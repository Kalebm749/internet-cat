import json
import random

import pytest

from cat import db
from cat.engine import Engine
from cat.llm import MockBackend


def make_engine(cfg, seed=1, backend=None):
    b = backend or MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    return Engine(cfg, b, rng=random.Random(seed)), b


def test_bootstrap_creates_state_and_objects(cfg):
    e, _ = make_engine(cfg)
    st = db.get_state(e.conn)
    assert st["area"] == cfg.character["start_area"]
    assert e.conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == len(cfg.world["objects"])


def test_one_tick_records_everything(cfg):
    e, _ = make_engine(cfg)
    info = e.tick()
    row = e.conn.execute("SELECT * FROM ticks WHERE id=?", (info["id"],)).fetchone()
    assert row["kind"] == "tick" and row["action"] and row["outcome"]
    assert set(json.loads(row["needs_after"])) == set(cfg.character["needs"])
    assert db.get_state(e.conn)["sim_time"] > row["sim_time"]


def test_1000_ticks_no_errors_and_invariants(cfg):
    e, _ = make_engine(cfg, seed=7)
    assert e.run(ticks=1000, interval=0) == 1000
    assert e.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1000
    st = db.get_state(e.conn)
    assert all(0 <= v <= 100 for v in st["needs"].values())
    assert -1 <= st["mood"]["valence"] <= 1 and 0 <= st["mood"]["arousal"] <= 1
    # every tick row has bounded needs and a positive duration
    for r in e.conn.execute("SELECT needs_after, duration FROM ticks"):
        assert all(0 <= v <= 100 for v in json.loads(r["needs_after"]).values())
        assert r["duration"] > 0


def test_deterministic_with_seed(cfg, tmp_path):
    def run(name):
        cfg.settings["db_path"] = str(tmp_path / name)
        e, _ = make_engine(cfg, seed=3)
        e.run(ticks=40, interval=0)
        return [tuple(r) for r in e.conn.execute("SELECT sim_time, action, needs_after FROM ticks")]
    assert run("a.db") == run("b.db")


def test_resumes_from_db(cfg):
    e, b = make_engine(cfg)
    e.run(ticks=5, interval=0)
    t = db.get_state(e.conn)["sim_time"]
    e2 = Engine(cfg, b, conn=e.conn, rng=random.Random(2))
    assert e2.state["sim_time"] == t
    e2.tick()
    assert e.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 6


def test_single_failure_is_retried_not_dozed(cfg):
    e, b = make_engine(cfg)
    b.fail_next = 1
    assert e.tick()["kind"] == "tick"


def test_backend_failure_becomes_doze_not_crash(cfg):
    e, b = make_engine(cfg)
    b.fail_next = 2          # llm_retries=1 => two attempts, both fail
    info = e.tick()
    assert info["kind"] == "doze" and info["outcome"] == "dozing"
    assert db.get_state(e.conn)["brain_online"] == 0
    assert e.tick()["kind"] == "tick"
    assert db.get_state(e.conn)["brain_online"] == 1


def test_crash_mid_tick_leaves_previous_state(cfg, monkeypatch):
    e, _ = make_engine(cfg)
    e.tick()
    before = db.get_state(e.conn)
    n_ticks = e.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(db, "save_state", boom)
    with pytest.raises(RuntimeError):
        e.tick()
    monkeypatch.undo()
    assert db.get_state(e.conn) == before
    assert e.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == n_ticks
    e.tick()  # engine still works after a reload


def test_run_survives_a_failing_tick(cfg, monkeypatch):
    e, _ = make_engine(cfg)
    orig = e._tick
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("transient")
        return orig()
    monkeypatch.setattr(e, "_tick", flaky)
    assert e.run(ticks=4, interval=0) == 4
    assert e.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 3


def test_effects_wired_in_engine(cfg):
    e, b = make_engine(cfg)
    b.handlers["narrator"] = lambda m, s: {
        "outcome": "She eats and wanders.", "gains": {"fun": "+"}, "effort": "low", "duration": 20,
        "changes": [{"op": "consume", "object_id": "ghost"},
                    {"op": "move_self", "area": "kitchen"}, {"op": "consume", "object_id": "bowl"}]}
    e.state["area"] = "kitchen"
    e.state["needs"]["food"] = 10.0
    info = e.tick()
    row = e.conn.execute("SELECT * FROM ticks WHERE id=?", (info["id"],)).fetchone()
    assert [c["op"] for c in json.loads(row["changes"])] == ["consume"]
    assert json.loads(row["rejected_changes"])[0]["reason"].startswith("no such object")
    assert db.get_state(e.conn)["needs"]["food"] > 10
    assert e.conn.execute("SELECT COUNT(*) FROM objects WHERE id='bowl'").fetchone()[0] == 0


def test_repeated_action_pays_less(cfg):
    e, b = make_engine(cfg)
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": "bat the red ball",
                                        "intent": "", "feeling": "neutral", "wants_sleep": False}
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok", "gains": {"fun": "++"}, "effort": "low",
                                           "duration": 5, "changes": []}
    e.state["needs"]["fun"] = 10.0
    e.tick()
    f1 = db.get_state(e.conn)["needs"]["fun"]
    e.tick()
    f2 = db.get_state(e.conn)["needs"]["fun"]
    assert f1 - 10 > 25 and (f2 - f1) < 20        # first ~+30, repeat ~+12 (x0.4)


def test_nothing_appears_or_vanishes_without_an_effect(cfg):
    """Audit: replay every tick's recorded changes; the result must equal the real objects table."""
    e, _ = make_engine(cfg, seed=11)
    start = {o["id"] for o in cfg.world["objects"]}
    e.run(ticks=1000, interval=0)
    ids = set(start)
    rejected_total = applied_total = 0
    for r in e.conn.execute("SELECT changes, rejected_changes FROM ticks ORDER BY id"):
        for c in json.loads(r["changes"]):
            applied_total += 1
            if c["op"] == "create":
                assert c["object_id"] not in ids
                ids.add(c["object_id"])
            elif c["op"] == "consume":
                ids.remove(c["object_id"])
        rejected_total += len(json.loads(r["rejected_changes"]))
    assert ids == {r[0] for r in e.conn.execute("SELECT id FROM objects")}
    assert applied_total > 100 and rejected_total > 20       # the mock exercised both paths
    valid_areas = set(cfg.world["areas"]) | {"inventory"}
    for o in e.conn.execute("SELECT area FROM objects"):
        assert o["area"] in valid_areas
    assert e.conn.execute("SELECT COUNT(*) FROM objects WHERE area='inventory'").fetchone()[0] \
        <= cfg.settings["inventory_max"]
    assert db.get_state(e.conn)["area"] in cfg.world["areas"]


def test_event_spawn_consumed_same_tick_is_still_audited(cfg):
    cfg.world["events"] = [{"text": "fish!", "chance": 1.0, "area": "kitchen",
                            "spawn": {"name": "a fish", "tags": ["edible"], "area": "kitchen"}}]
    e, b = make_engine(cfg)
    e.state["area"] = "kitchen"
    b.handlers["narrator"] = lambda m, s: {"outcome": "eats", "gains": {}, "effort": "low",
                                           "duration": 10, "changes": [{"op": "consume", "object_id": "fish"}]}
    info = e.tick()
    ops = [(c["op"], c["object_id"]) for c in json.loads(
        e.conn.execute("SELECT changes FROM ticks WHERE id=?", (info["id"],)).fetchone()[0])]
    assert ops == [("create", "fish"), ("consume", "fish")]

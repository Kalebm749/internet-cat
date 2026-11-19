import json
import random

import pytest

from cat import db
from cat.engine import Engine
from cat.llm import MockBackend


def make_engine(cfg, seed=1, backend=None):
    b = backend or MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
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
    assert -50 <= st["mood"]["value"] <= 50 and "arousal" not in st["mood"]
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
        "outcome": "She eats and wanders.", "gains": {"joy": "+"}, "effort": "low", "duration": 20,
        "changes": [{"op": "consume", "object_id": "ghost"},
                    {"op": "move_self", "area": "kitchen"}, {"op": "consume", "object_id": "bowl"}]}
    e.state["area"] = "kitchen"
    e.state["needs"]["sustenance"] = 10.0
    info = e.tick()
    row = e.conn.execute("SELECT * FROM ticks WHERE id=?", (info["id"],)).fetchone()
    assert [c["op"] for c in json.loads(row["changes"])] == ["consume"]
    assert json.loads(row["rejected_changes"])[0]["reason"].startswith("no such object")
    assert db.get_state(e.conn)["needs"]["sustenance"] > 10
    assert e.conn.execute("SELECT COUNT(*) FROM objects WHERE id='bowl'").fetchone()[0] == 0


def test_repeated_action_pays_less(cfg):
    e, b = make_engine(cfg)
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": "bat the red ball",
                                        "intent": "", "feeling": "neutral", "wants_sleep": False}
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok", "gains": {"wonder": "++"}, "effort": "low",
                                           "duration": 5, "changes": []}
    e.state["needs"]["wonder"] = 10.0
    e.tick()
    f1 = db.get_state(e.conn)["needs"]["wonder"]
    e.tick()
    f2 = db.get_state(e.conn)["needs"]["wonder"]
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


# ---------- config edited while a database exists ----------
def test_config_changes_are_reconciled_with_saved_state(cfg, caplog):
    e, b = make_engine(cfg)
    e.run(ticks=5, interval=0)
    before = db.get_state(e.conn)
    cfg.character["needs"]["thirst"] = {"start": 42, "decay_per_hour": 3, "meaning": "m",
                                         "expressions": {"empty": "e", "low": "l", "full": "f"}}
    cfg.character["tendencies"]["curiosity"] = 0.7
    del cfg.character["needs"]["peace"]
    b.need_ids = list(cfg.character["needs"])
    with caplog.at_level("WARNING", logger="cat.engine"):
        e2 = Engine(cfg, b, conn=e.conn, rng=random.Random(2))
    st = db.get_state(e2.conn)
    assert st["needs"]["thirst"] == 42 and "peace" not in st["needs"]
    assert st["needs"]["joy"] == before["needs"]["joy"]                 # untouched needs keep their value
    assert st["tendencies"]["curiosity"] == 0.7
    assert st["tendencies"]["boldness"] == before["tendencies"]["boldness"]
    assert "new need 'thirst'" in caplog.text and "dropped need 'peace'" in caplog.text
    assert e2.run(ticks=20, interval=0) == 20                           # and the loop works
    assert e2.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 25


def test_creature_in_a_removed_area_goes_home(cfg):
    e, b = make_engine(cfg)
    e.tick()
    e.conn.execute("UPDATE state SET area='the_moon'")
    e2 = Engine(cfg, b, conn=e.conn, rng=random.Random(2))
    assert db.get_state(e2.conn)["area"] == cfg.character["start_area"]
    assert e2.tick()["kind"] in ("tick", "asleep")


def test_unchanged_config_leaves_state_alone(cfg):
    e, b = make_engine(cfg)
    e.run(ticks=3, interval=0)
    st, ts = db.get_state(e.conn), e.conn.execute("SELECT updated_at FROM state").fetchone()[0]
    Engine(cfg, b, conn=e.conn, rng=random.Random(2))
    assert db.get_state(e.conn) == st

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


def test_backend_failure_becomes_doze_not_crash(cfg):
    e, b = make_engine(cfg)
    b.fail_next = 1
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

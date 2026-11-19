import pytest

from cat import db


def test_schema_and_wal(tmp_path):
    c = db.connect(tmp_path / "x.db")
    db.init_db(c)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"state", "objects", "ticks", "events", "memories", "preferences", "sleep_log",
            "meta"} <= tables


def test_meta_roundtrip(conn):
    assert db.get_meta(conn, "k") is None
    db.set_meta(conn, "k", 5)
    db.set_meta(conn, "k", 6)
    assert db.get_meta(conn, "k") == "6"


def test_state_roundtrip(conn):
    s = dict(sim_time="2026-11-18T07:00", area="a", needs={"food": 1}, mood={"w": "x"},
             energy=50.0, weather="clear", tendencies={"b": 0.5}, is_asleep=False, brain_online=True)
    db.save_state(conn, s, "now")
    got = db.get_state(conn)
    assert got["needs"] == {"food": 1} and got["is_asleep"] == 0
    db.save_state(conn, {**s, "area": "b"}, "later")
    assert db.get_state(conn)["area"] == "b"


def test_transaction_rolls_back(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            db.set_meta(conn, "a", 1)
            raise RuntimeError("crash mid-tick")
    assert db.get_meta(conn, "a") is None
    with db.transaction(conn):
        db.set_meta(conn, "a", 2)
    assert db.get_meta(conn, "a") == "2"

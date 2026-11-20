"""A life is never lost to an update: the database is backed up first, migrated, and the world brought in step with
world.yaml without touching anything the creature made, carried or lived. Also the world's food sources."""
import random
import shutil

import pytest

from cat import db
from cat.config import CONFIG_DIR, load_config
from cat.engine import Engine
from cat.llm import MockBackend


@pytest.fixture
def town(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    c = load_config(d)
    c.settings.update(db_path=str(tmp_path / "data" / "cat.db"))
    c.settings["director"]["enabled"] = False
    (tmp_path / "data").mkdir()
    return c


def make(cfg, seed=1):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    return Engine(cfg, b, rng=random.Random(seed))


def test_an_older_world_is_updated_without_losing_the_life(town):
    e = make(town)
    for _ in range(5):
        e.tick()
    with db.transaction(e.conn):            # an older world: junk, a director's thing, something carried, a drawing
        add = "INSERT INTO objects(id,name,area,tags,state,created_by,created_at) VALUES(?,?,?,?,?,?,'2026-11-18T07:00')"
        e.conn.execute(add, ("feather", "a white feather", "fountain", '["toy"]', "{}", "world"))
        e.conn.execute(add, ("top", "a battered wooden top", "inventory", '["toy"]', "{}", "world"))
        e.conn.execute(add, ("pebble", "a smooth pebble", "garden", '["movable"]', "{}", "director"))
        e.conn.execute(add, ("drawing_1", "a drawing of the fountain", "studio", '["drawing"]', '{"status": "pinned"}', "creature"))
        e.conn.execute("UPDATE objects SET name='the old stall' WHERE id='stall'")
        e.conn.execute("DELETE FROM objects WHERE id='nest'")
        db.set_meta(e.conn, "schema_version", 1)
    lived = e.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    e.conn.close()

    e2 = make(town, seed=2)                 # the update: a new version starts on the same database
    ids = set(e2.world.objects)
    assert "feather" not in ids and "pebble" not in ids                 # junk and the director's thing are gone
    assert {"top", "drawing_1", "nest"} <= ids                          # carried and made are kept; new is added
    assert e2.world.objects["stall"]["name"] == "the bread stall"       # the file decides the world's things
    assert e2.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == lived   # its life is all there
    assert db.schema_version(e2.conn) == db.SCHEMA_VERSION
    backups = list((town.db_path.parent / "backups").glob("cat-*-pre-update.db"))
    assert backups                                                      # and a copy was taken first
    import sqlite3
    old = sqlite3.connect(backups[0])
    assert old.execute("SELECT COUNT(*) FROM objects WHERE id='feather'").fetchone()[0] == 1


def test_no_backup_when_nothing_changed(town):
    e = make(town)
    e.tick()
    e.conn.close()
    make(town).conn.close()                 # the first restart may take one (fingerprint first recorded)
    before = len(list((town.db_path.parent / "backups").glob("*")))
    make(town).conn.close()
    assert len(list((town.db_path.parent / "backups").glob("*"))) == before


def test_sources_restock_whether_or_not_it_is_there(town):
    e = make(town)
    e.state["area"] = "garden"                                          # far from the market
    e.state["sim_time"] = "2026-11-18T05:40"
    e.tick()                                                            # before dawn: no bread
    assert not [o for o in e.world.here("market") if o["name"] == "a fresh loaf of bread"]
    e.state["sim_time"] = "2026-11-18T06:10"
    e.tick()
    loaves = [o for o in e.world.here("market") if o["name"] == "a fresh loaf of bread"]
    assert len(loaves) == 1 and loaves[0]["created_by"] == "source"
    e.state["sim_time"] = "2026-11-18T09:00"
    e.tick()                                                            # once a day, not every tick
    assert len([o for o in e.world.here("market") if o["name"] == "a fresh loaf of bread"]) == 1

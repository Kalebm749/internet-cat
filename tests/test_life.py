"""Spawning and reopening the life (docs/rewrite-contracts.md §2, §9)."""
import random

import pytest

from cat import content, life, store, tuning
from tests.rewrite_helpers import copy_pack


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "nit.db")


def test_spawn(conn):
    lf = life.open_life(conn, content.load(), tuning.load())
    assert lf.spawned and lf.location == "fountain" and lf.sim_time == "2026-11-21T07:00"
    assert (lf.bodystate.mood, lf.bodystate.energy, lf.bodystate.hunger) == (0.1, 0.8, 0.2)
    assert lf.character.version == 1 and lf.world.get("market.offerings") == 2
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == len(content.load().facts)


def test_reopen_carries_everything_forward(conn):
    c, t = content.load(), tuning.load()
    lf = life.open_life(conn, c, t)
    changes = lf.body.passage(lf.bodystate, 90, effort_per_min=0.001, cause="inspect")
    lf.world.apply([{"op": "append", "fact": "fountain.wishes", "value": {"text": "to know"}}], {}, random.Random(0))
    lf.sim_time, lf.location = "2026-11-21T08:30", "market"
    with store.transaction(conn):
        life.save(conn, lf)
        life.record_body(conn, lf, changes)
    back = life.open_life(conn, c, t)
    assert not back.spawned and back.location == "market" and back.sim_time == "2026-11-21T08:30"
    assert back.bodystate == lf.bodystate
    assert back.world.get("fountain.wishes") == [{"id": "1", "text": "to know"}]
    causes = {r["cause"] for r in conn.execute("SELECT cause FROM body_changes")}
    assert causes == {"time", "effort (inspect)"}          # mood starts at the baseline: no drift yet


def test_a_removed_location_moves_it_home(conn, tmp_path):
    pack = copy_pack(tmp_path)
    pack.write("locations/garden.yaml", 'id: garden\nname: the garden\ndescription: "x"\nexits: [fountain]\n')
    pack.replace("locations/fountain.yaml", "exits: [market", "exits: [market, garden")
    t = tuning.load()
    lf = life.open_life(conn, content.load(pack.root), t)
    lf.location = "garden"
    with store.transaction(conn):
        life.save(conn, lf)
    (pack.root / "locations/garden.yaml").unlink()
    pack.replace("locations/fountain.yaml", "exits: [market, garden", "exits: [market")
    back = life.open_life(conn, content.load(pack.root), t)
    assert back.location == "fountain"
    assert any("no longer exists" in n for n in back.notes)

"""World state (docs/rewrite-contracts.md §4.2): touched facts, regrowth, persistence and the content sync."""
import random

import pytest

from cat import content, store
from cat.ops import OpError
from cat.worldstate import WorldState
from tests.rewrite_helpers import copy_pack


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "nit.db")
    store.migrate(c)
    return c


def save(ws, conn, all=False):
    with store.transaction(conn):
        ws.save(conn, all=all)


def test_migrate_is_idempotent(tmp_path):
    c = store.connect(tmp_path / "x.db")
    assert store.migrate(c) == list(range(1, store.SCHEMA_VERSION + 1))
    assert store.migrate(c) == []
    assert store.schema_version(c) == store.SCHEMA_VERSION


def test_fresh_world_has_the_defaults_and_defaults_are_not_shared():
    c = content.load()
    a, b = WorldState.fresh(c), WorldState.fresh(c)
    assert a.get("market.offerings") == 2 and a.get("world.weather") == "clear"
    a.values["fountain.wishes"].append("x")
    assert b.get("fountain.wishes") == [] and c.facts["fountain.wishes"].default == []


def test_creature_changes_are_touched_world_changes_are_not():
    ws = WorldState.fresh(content.load())
    ws.apply([{"op": "add", "fact": "market.offerings", "value": -1}], {}, random.Random(0))
    ws.apply([{"op": "set", "fact": "world.weather", "value": "rain"}], {}, random.Random(0), by="world")
    assert ws.touched == {"market.offerings"}
    assert {"market.offerings", "world.weather"} <= ws.dirty


def test_failed_apply_leaves_state_alone():
    ws = WorldState.fresh(content.load())
    with pytest.raises(OpError):
        ws.apply([{"op": "add", "fact": "market.offerings", "value": -1},
                  {"op": "set", "fact": "world.weather", "value": "hail"}], {}, random.Random(0))
    assert ws.get("market.offerings") == 2 and not ws.touched


def test_regrow_at_hours_across_days_and_clamped():
    ws = WorldState.fresh(content.load())
    ws.values["market.offerings"] = 0
    assert ws.regrow("2026-11-21T07:00", "2026-11-21T12:00") == {"market.offerings": 1}   # 12:00 counts
    assert ws.regrow("2026-11-21T12:00", "2026-11-21T17:59") == {}                         # 12:00 already counted
    ws.values["market.offerings"] = 0
    ws.regrow("2026-11-21T19:00", "2026-11-22T13:00")                                      # 06:00 and 12:00 next day
    assert ws.get("market.offerings") == 2
    ws.regrow("2026-11-22T13:00", "2026-11-25T13:00")
    assert ws.get("market.offerings") == 3                                                 # max
    assert "market.offerings" not in ws.touched


def test_regrow_every_hours(tmp_path):
    pack = copy_pack(tmp_path)
    pack.replace("locations/market.yaml", "regrow: {amount: 1, at_hours: [6, 12, 18]}", "regrow: {amount: 1, every_hours: 4}")
    ws = WorldState.fresh(content.load(pack.root))
    ws.values["market.offerings"] = 0
    ws.regrow("2026-11-21T01:00", "2026-11-21T09:00")      # 04:00 and 08:00
    assert ws.get("market.offerings") == 2


def test_round_trip(conn):
    c = content.load()
    ws = WorldState.fresh(c)
    ws.apply([{"op": "append", "fact": "fountain.wishes", "value": {"text": "x"}}], {}, random.Random(0))
    save(ws, conn)
    back, rep = WorldState.load(conn, c)
    assert not rep.changed
    assert back.values == ws.values and back.touched == {"fountain.wishes"} and back.seq["fountain.wishes"] == 1


def test_world_changes_survive_a_restart(conn):
    """An untouched fact the world changed (weather) keeps its value when the content is unchanged."""
    c = content.load()
    ws = WorldState.fresh(c)
    ws.apply([{"op": "set", "fact": "world.weather", "value": "rain"}], {}, random.Random(0), by="world")
    save(ws, conn, all=True)
    back, _ = WorldState.load(conn, c)
    assert back.get("world.weather") == "rain"


def test_sync_with_changed_content(conn, tmp_path):
    pack = copy_pack(tmp_path)
    c1 = content.load(pack.root)
    ws = WorldState.fresh(c1)
    ws.apply([{"op": "add", "fact": "market.offerings", "value": -2}], {}, random.Random(0))   # touched
    save(ws, conn, all=True)

    # new fact, removed fact, a changed default on an untouched fact, a changed default on a touched fact
    pack.replace("locations/market.yaml", "  answered: {type: list, default: [], max_len: 200, visible: summary}",
                 "  bell: {type: bool, default: true}")
    pack.replace("locations/market.yaml", "offerings: {type: int, default: 2,", "offerings: {type: int, default: 1,")
    pack.replace("locations/fountain.yaml", "wishes: {type: list, default: [],", "wishes: {type: list, default: [{text: old}],")
    pack.replace("locations/market.yaml", "{op: move_where, from: market.mailbox, to: market.answered,",
                 "{op: remove_where, fact: market.mailbox,")
    pack.replace("locations/market.yaml", "\n           set: {status: answered, reply: \"{inputs.reply}\", answered_at: \"{now}\"}}", "}")
    c2 = content.load(pack.root)
    back, rep = WorldState.load(conn, c2)
    assert rep.added == ["market.bell"] and rep.removed == ["market.answered"]
    assert rep.updated == ["fountain.wishes"] and back.get("fountain.wishes") == [{"text": "old"}]
    assert back.get("market.offerings") == 0            # the creature's change wins over the new default
    save(back, conn)
    keys = {r["key"] for r in conn.execute("SELECT key FROM facts")}
    assert "market.answered" not in keys and "market.bell" in keys


def test_touched_value_that_no_longer_fits_is_reset(conn, tmp_path):
    pack = copy_pack(tmp_path)
    ws = WorldState.fresh(content.load(pack.root))
    ws.apply([{"op": "set", "fact": "world.weather", "value": "rain"}], {}, random.Random(0))
    save(ws, conn, all=True)
    pack.replace("world.yaml", "choices: [clear, cloudy, rain]", "choices: [clear, cloudy]")
    pack.replace("world.yaml", "weather_in: [cloudy], salience: 0.7, world: [{op: set, fact: world.weather, value: rain}]",
                 "weather_in: [cloudy], salience: 0.7")
    pack.replace("world.yaml", "weather_in: [rain], world: [{op: set, fact: world.weather, value: cloudy}]", "weather_in: [cloudy]")
    back, rep = WorldState.load(conn, content.load(pack.root))
    assert rep.reset == ["world.weather"] and back.get("world.weather") == "clear"


def test_visible_facts():
    ws = WorldState.fresh(content.load())
    ws.values["market.mailbox"] = [{"id": "1"}, {"id": "2"}]
    v = ws.visible("market")
    assert v["market.mailbox"] == {"count": 2} and v["market.offerings"] == 2 and v["world.weather"] == "clear"
    assert "fountain.wishes" not in v

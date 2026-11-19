import pytest

from cat import effects as fx
from cat.world import WorldState

NOW = "2026-11-18T12:00"


@pytest.fixture
def world(cfg, conn):
    return WorldState.seed(conn, cfg, NOW)


def run(cfg, world, changes, area="kitchen"):
    return fx.apply_changes(changes, world, area, cfg, NOW)


def reasons(r):
    return [x["reason"] for x in r.rejected]


# ---- move_self ----
def test_move_self_ok(cfg, world):
    r = run(cfg, world, [{"op": "move_self", "area": "living_room"}])
    assert r.area == "living_room" and not r.rejected and r.applied[0]["label"] == "the living room"


@pytest.mark.parametrize("target,msg", [
    ("garden", "not connected"), ("mars", "unknown area"), ("kitchen", "already"), (None, "missing")])
def test_move_self_rejected(cfg, world, target, msg):
    r = run(cfg, world, [{"op": "move_self", "area": target}])
    assert r.area == "kitchen" and msg in reasons(r)[0]


def test_changes_evaluated_in_order(cfg, world):
    r = run(cfg, world, [{"op": "move_self", "area": "living_room"},
                         {"op": "move_self", "area": "hallway"},
                         {"op": "take", "object_id": "bowl"}])  # bowl is in the kitchen, not here
    assert r.area == "hallway" and len(r.applied) == 2 and "not here" in reasons(r)[0]


# ---- take / drop ----
def test_take_and_drop(cfg, world):
    r = run(cfg, world, [{"op": "take", "object_id": "bowl"}])
    assert world.objects["bowl"]["area"] == "inventory" and not r.rejected
    r = run(cfg, world, [{"op": "drop", "object_id": "bowl"}], area="living_room")
    assert world.objects["bowl"]["area"] == "living_room" and not r.rejected


@pytest.mark.parametrize("change,msg,area", [
    ({"op": "take", "object_id": "sofa"}, "not here", "kitchen"),
    ({"op": "take", "object_id": "ghost"}, "no such object", "kitchen"),
    ({"op": "take", "object_id": "sofa"}, "not movable", "living_room"),
    ({"op": "drop", "object_id": "bowl"}, "not in the inventory", "kitchen"),
])
def test_take_drop_rejections(cfg, world, change, msg, area):
    assert msg in reasons(run(cfg, world, [change], area=area))[0]


def test_inventory_full(cfg, world):
    cfg.settings["inventory_max"] = 1
    r = run(cfg, world, [{"op": "take", "object_id": "bowl"}], "kitchen")
    world.objects["ball"]["area"] = "kitchen"
    r = run(cfg, world, [{"op": "take", "object_id": "ball"}], "kitchen")
    assert "inventory is full" in reasons(r)


# ---- move_object ----
def test_move_object_ok_and_adjacent(cfg, world):
    r = run(cfg, world, [{"op": "move_object", "object_id": "bowl", "area": "living_room"}])
    assert not r.rejected and world.objects["bowl"]["area"] == "living_room"


@pytest.mark.parametrize("change,msg", [
    ({"op": "move_object", "object_id": "bowl", "area": "garden"}, "neither here nor adjacent"),
    ({"op": "move_object", "object_id": "sofa", "area": "kitchen"}, "not within reach"),
    ({"op": "move_object", "object_id": "bowl", "area": "kitchen"}, "already there"),
    ({"op": "move_object", "object_id": "bowl", "area": "nowhere"}, "unknown area"),
])
def test_move_object_rejections(cfg, world, change, msg):
    assert msg in reasons(run(cfg, world, [change]))[0]


def test_move_object_not_movable(cfg, world):
    r = run(cfg, world, [{"op": "move_object", "object_id": "sofa", "area": "kitchen"}], "living_room")
    assert "not movable" in reasons(r)[0]


# ---- consume ----
def test_consume_edible_deletes_and_flags(cfg, world, conn):
    r = run(cfg, world, [{"op": "consume", "object_id": "bowl"}])
    assert r.consumed and "bowl" not in world.objects
    world.sync(conn)


def test_consume_inventory_ok(cfg, world):
    world.objects["bowl"]["area"] = "inventory"
    assert run(cfg, world, [{"op": "consume", "object_id": "bowl"}], "garden").consumed


@pytest.mark.parametrize("area,oid,msg", [
    ("living_room", "sofa", "not edible"), ("garden", "bowl", "not within reach")])
def test_consume_rejections(cfg, world, area, oid, msg):
    r = run(cfg, world, [{"op": "consume", "object_id": oid}], area)
    assert msg in reasons(r)[0] and not r.consumed and oid in world.objects


# ---- set_state ----
def test_set_state(cfg, world):
    r = run(cfg, world, [{"op": "set_state", "object_id": "window", "key": "open", "value": True}],
            "living_room")
    assert world.objects["window"]["state"]["open"] == "true" and not r.rejected


@pytest.mark.parametrize("change,msg", [
    ({"op": "set_state", "object_id": "window", "key": "open", "value": "x"}, "not within reach"),
    ({"op": "set_state", "object_id": "bowl", "key": "", "value": "x"}, "'key'"),
    ({"op": "set_state", "object_id": "bowl", "key": "k", "value": ["a"]}, "invalid 'value'"),
    ({"op": "set_state", "object_id": "bowl", "key": "k", "value": "x" * 99}, "too long"),
])
def test_set_state_rejections(cfg, world, change, msg):
    assert msg in reasons(run(cfg, world, [change]))[0]


# ---- create ----
def test_create_filters_tags_and_ids(cfg, world):
    r = run(cfg, world, [{"op": "create", "name": "a fort", "tags": ["soft", "bogus"]}])
    o = world.objects["fort"]
    assert o["tags"] == ["soft"] and o["created_by"] == "creature" and o["area"] == "kitchen"
    assert r.applied[0]["dropped_tags"] == ["bogus"]


@pytest.mark.parametrize("change,msg", [
    ({"op": "create", "name": "x", "area": "garden"}, "current area"),
    ({"op": "create", "name": ""}, "'name'"),
    ({"op": "create", "name": "n" * 99}, "too long"),
    ({"op": "create", "name": "x", "tags": "soft"}, "invalid 'tags'"),
])
def test_create_rejections(cfg, world, change, msg):
    assert msg in reasons(run(cfg, world, [change]))[0]


def test_create_rate_limit_and_area_cap(cfg, world):
    r = run(cfg, world, [{"op": "create", "name": "a"}, {"op": "create", "name": "b"}])
    assert len(r.applied) == 1 and "create limit" in reasons(r)[0]
    cfg.settings["max_objects_per_area"] = len(world.here("kitchen"))
    assert "full" in reasons(run(cfg, world, [{"op": "create", "name": "c"}]))[0]


# ---- general ----
def test_unknown_op_and_garbage(cfg, world):
    r = run(cfg, world, [{"op": "teleport"}, {"nope": 1}, {"op": None}])
    assert len(r.rejected) == 3 and not r.applied
    assert r.rejected[0]["change"] == {"op": "teleport"}


def test_too_many_changes(cfg, world):
    cfg.settings["max_changes_per_tick"] = 2
    ch = [{"op": "set_state", "object_id": "bowl", "key": f"k{i}", "value": "1"} for i in range(4)]
    r = run(cfg, world, ch)
    assert len(r.applied) == 2 and len(r.rejected) == 2


def test_name_fallback_and_ambiguity(cfg, world):
    r = run(cfg, world, [{"op": "consume", "object_id": "A bowl of fish"}])
    assert r.consumed
    world.add_object("a ball", ["movable"], "kitchen", "creature", NOW)
    world.add_object("a ball", ["movable"], "kitchen", "creature", NOW)
    assert "ambiguous" in reasons(run(cfg, world, [{"op": "take", "object_id": "a ball"}]))[0]


def test_rejections_are_logged(cfg, world, caplog):
    with caplog.at_level("WARNING", logger="cat.effects"):
        run(cfg, world, [{"op": "consume", "object_id": "ghost"}])
    assert "rejected change" in caplog.text

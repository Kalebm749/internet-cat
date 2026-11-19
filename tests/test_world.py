import random

from cat import clock
from cat.world import WorldState

NOW = "2026-11-18T12:00"


def make(cfg, conn):
    return WorldState.seed(conn, cfg, NOW)


def test_seed_sync_load_roundtrip(cfg, conn):
    w = make(cfg, conn)
    w.sync(conn)
    w2 = WorldState.load(conn, cfg, "clear")
    assert set(w2.objects) == set(w.objects)
    assert w2.objects["bowl"]["tags"] == ["edible", "movable"]


def test_add_remove_unique_ids(cfg, conn):
    w = make(cfg, conn)
    a = w.add_object("a red ball", ["toy"], "garden", "creature", NOW)
    assert a["id"] == "red_ball"
    assert w.add_object("a red ball", ["toy"], "garden", "creature", NOW)["id"] == "red_ball_2"
    w.sync(conn)
    w.remove_object("red_ball")
    w.sync(conn)
    ids = {r[0] for r in conn.execute("SELECT id FROM objects")}
    assert "red_ball" not in ids and "red_ball_2" in ids


def test_events_respect_filters(cfg, conn):
    w = make(cfg, conn)
    cfg.world["events"] = [
        {"text": "bird", "chance": 1.0, "area": "living_room", "hours": [8, 18]}]
    rng = random.Random(0)
    noon = clock.parse("2026-11-18T12:00")
    assert [e["text"] for e in w.roll_events(rng, noon, "living_room", NOW)] == ["bird"]
    assert w.roll_events(rng, noon, "kitchen", NOW) == []                       # wrong area
    assert w.roll_events(rng, clock.parse("2026-11-18T20:00"), "living_room", NOW) == []  # hours


def test_weather_events_and_spawn_cap(cfg, conn):
    w = make(cfg, conn)
    cfg.world["events"] = [
        {"text": "rain", "chance": 1.0, "set_weather": "rain"},
        {"text": "stops", "chance": 1.0, "only_weather": "rain", "set_weather": "clear"},
        {"text": "fish", "chance": 1.0, "area": "kitchen",
         "spawn": {"name": "a fish", "tags": ["edible"], "area": "kitchen"}}]
    cfg.settings["events_max_per_tick"] = 5
    cfg.settings["max_objects_per_area"] = 2
    noon = clock.parse(NOW)
    f = w.roll_events(random.Random(1), noon, "kitchen", NOW)
    assert w.weather == "clear" or w.weather == "rain"
    assert [e["text"] for e in f][0] == "rain"
    assert any(e["spawned"] == "fish" for e in f)
    for _ in range(3):
        w.roll_events(random.Random(1), noon, "kitchen", NOW)
    assert len(w.here("kitchen")) <= 2


def test_events_per_tick_capped(cfg, conn):
    w = make(cfg, conn)
    cfg.world["events"] = [{"text": str(i), "chance": 1.0} for i in range(5)]
    cfg.settings["events_max_per_tick"] = 2
    assert len(w.roll_events(random.Random(0), clock.parse(NOW), "kitchen", NOW)) == 2


def test_objects_keep_their_config_order_through_the_database(cfg, conn):
    """The menu is built from this order, so it must not depend on hash randomisation."""
    from cat import db
    from cat.world import WorldState
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    with db.transaction(conn):
        w.sync(conn)
    expect = [o["id"] for o in cfg.world["objects"]]
    assert [o["id"] for o in WorldState.load(conn, cfg, "clear").objects.values()] == expect
    assert [r[0] for r in conn.execute("SELECT id FROM objects ORDER BY rowid")] == expect


def test_the_menu_is_identical_under_different_hash_seeds(tmp_path):
    import os
    import subprocess
    import sys
    script = tmp_path / "menu.py"
    script.write_text(
        "import sys; sys.path.insert(0, '.')\n"
        "from cat.config import load_config\nfrom cat import db, moves\nfrom cat.world import WorldState\n"
        "cfg = load_config(); c = db.connect(':memory:'); db.init_db(c)\n"
        "w = WorldState.seed(c, cfg, '2026-11-18T19:40')\n"
        "with db.transaction(c):\n    w.sync(c)\n"
        "w = WorldState.load(c, cfg, 'clear')\n"
        "print(','.join(m.key for m in moves.offered(cfg, area_id='living_room', here=w.here('living_room'), "
        "inventory=[], projects=[], can_sleep=False)))\n")
    outs = {subprocess.run([sys.executable, str(script)], capture_output=True, text=True, cwd=os.getcwd(),
                           env={**os.environ, "PYTHONHASHSEED": str(s)}).stdout for s in (1, 2, 3, 4)}
    assert len(outs) == 1 and "go:kitchen" in outs.pop()

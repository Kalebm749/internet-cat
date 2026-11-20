"""Drawing: real things it makes in the stationer's back room, over several sittings, from what it has known.
Runs on the default town-square world and moves (the test fixtures' house world has no paper)."""
import json
import random
import shutil

import pytest
from fastapi.testclient import TestClient

from cat import art, drawings as DW
from cat.config import CONFIG_DIR, load_config
from cat.engine import Engine
from cat.llm import MockBackend
from tests.helpers import pick


@pytest.fixture
def town(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    c = load_config(d)
    c.settings["db_path"] = str(tmp_path / "cat.db")
    c.settings["anxiety"]["enabled"] = False
    c.settings["activity"]["enabled"] = False
    c.settings["director"]["enabled"] = False
    c.settings["moves"].update(shuffle=False, max_offered=500)      # every move on the menu, in file order
    c.settings["drives"]["enabled"] = False
    c.world["events"] = []
    return c


def engine(cfg, script, journal=None):
    """`script` is a list of move labels (or substrings), one per decision."""
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    steps = iter(script)
    seen = []
    b.handlers["brain"] = lambda m, s: (seen.append(m[-1]["content"]), pick(m, next(steps)))[1]
    if journal:
        b.handlers["journal"] = journal
    e = Engine(cfg, b, rng=random.Random(1))
    e.world.events = None
    return e, b, seen


def drawing_row(e):
    return DW.all_rows(e.conn)[0]


def test_it_can_only_draw_what_it_has_known(town):
    e, b, seen = engine(town, ["look closely at a scatter of old coins in the basin", "go to the stationer's back room",
                               "start a drawing of a scatter of old coins in the basin"])
    e.state["area"] = "studio"
    assert DW.subjects(e.conn, town, e.world) == []                       # nothing known yet: nothing to draw
    assert not any("start a drawing" in m.label for m in e._menu())
    e.state["area"] = "fountain"
    e.tick()                                                              # looked closely at the coins
    e.tick()                                                              # went to the back room
    keys = {s["key"] for s in DW.subjects(e.conn, town, e.world)}
    assert {"place:fountain", "thing:coins"} <= keys and "thing:paper" not in keys
    e.tick()
    d = drawing_row(e)
    assert d["subject_key"] == "thing:coins" and d["subject_tags"] == ["shiny", "wet"]
    assert len(d["sittings"]) == 1 and {"minutes", "mood", "anxiety", "energy", "tick_id"} <= set(d["sittings"][0])
    o = e.world.objects[d["object_id"]]
    assert o["tags"] == ["drawing"] and o["created_by"] == "creature" and o["area"] == "studio"
    assert o["state"] == {"status": "in progress", "sittings": "1"}


def _studio_with_a_drawing(town, script, journal=None):
    e, b, seen = engine(town, ["go to the stationer's back room", "start a drawing of the fountain"] + script, journal)
    e.state["area"] = "fountain"
    e.tick()
    e.tick()
    return e, b, seen


def test_an_ongoing_drawing_finished_titled_and_pinned(town):
    title = lambda m, s: {"entry": "Done.", "title": "Stillwater"} if "finished" in m[-1]["content"] else {"entry": "ok"}
    e, b, seen = _studio_with_a_drawing(town, ["add to a drawing of the fountain", "call a drawing of the fountain finished",
                                               "pin a drawing of the fountain on the board"], journal=title)
    e.tick()
    assert len(drawing_row(e)["sittings"]) == 2                           # a second sitting on the same drawing
    e.tick()
    d = drawing_row(e)
    assert d["status"] == "finished" and d["title"] == "Stillwater"       # its own name for it, from its journal
    e.tick()
    d = drawing_row(e)
    assert d["status"] == "pinned" and e.world.objects[d["object_id"]]["state"]["status"] == "pinned"
    from cat.web.app import create_app
    c = TestClient(create_app(town))
    st = c.get("/api/state").json()
    assert st["art"]["pinned"][0]["title"] == "Stillwater" and st["art"]["pinned"][0]["meta"].startswith("2 sittings")
    svg = c.get(st["art"]["pinned"][0]["img"])
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml") and "<path" in svg.text
    assert "Stillwater" in c.get("/biography").text
    assert any("pinned a drawing of the fountain on the board" in w["text"]
               for en in st["diary"] if en["type"] == "tick" for w in en["world_changes"])


def test_throwing_a_drawing_away(town):
    e, b, seen = _studio_with_a_drawing(town, ["throw away a drawing of the fountain"])
    oid = drawing_row(e)["object_id"]
    e.tick()
    assert oid not in e.world.objects and drawing_row(e)["status"] == "discarded"
    assert not e.conn.execute("SELECT 1 FROM objects WHERE id=?", (oid,)).fetchone()


def test_it_can_only_throw_away_its_own_things(town):
    from cat.effects import apply_changes
    e, b, seen = engine(town, [])
    e.state["area"] = "studio"
    fx = apply_changes([{"op": "discard", "object_id": "paper"}], e.world, "studio", town, e.state["sim_time"])
    assert fx.rejected and "not made by the creature" in fx.rejected[0]["reason"] and "paper" in e.world.objects


def test_an_interrupted_sitting_is_recorded_as_shorter(town):
    town.settings["activity"]["enabled"] = True
    town.moves["verbs"]["start_drawing"]["minutes"] = [60, 60]
    e, b, seen = _studio_with_a_drawing(town, [])
    assert e.state["activity"] and e.state["activity"]["drawing_object"]
    e.queue_injected_event({"text": "A door bangs somewhere."})           # the world breaks in
    b.handlers["brain"] = lambda m, s: pick(m, "stay still and do nothing")
    e.tick()
    s = drawing_row(e)["sittings"][0]
    assert s["interrupted"] is True and s["minutes"] == 20                # one 20-minute step of 60


def test_blocked_when_the_table_or_the_board_is_full(town):
    town.settings["drawing"].update(max_in_progress=1)
    e, b, seen = _studio_with_a_drawing(town, [])
    assert "draw_new" in DW.blocked(town, e.world)
    assert not any(m.verb == "start_drawing" for m in e._menu())
    assert any(m.verb == "draw_more" for m in e._menu())


def test_the_picture_comes_from_its_history_and_nothing_else(town):
    base = {"id": 7, "subject_kind": "thing", "subject_tags": ["wet"], "subject_name": "the puddle", "title": None}
    calm = {**base, "sittings": [{"minutes": 40, "mood": 30, "anxiety": 0, "energy": 20}]}
    tense = {**base, "sittings": [{"minutes": 40, "mood": -30, "anxiety": 90, "energy": 20}]}
    short = {**base, "sittings": [{"minutes": 5, "mood": 30, "anxiety": 0, "energy": 20}]}
    a, a2 = art.render_svg(town, calm), art.render_svg(town, calm)
    assert a == a2                                                        # the same history, the same picture
    assert a != art.render_svg(town, tense)                               # how it was changes how it looks
    assert art.render_svg(town, short).count("<path") < a.count("<path")  # a short sitting leaves fewer marks
    assert art.motif(calm) == "ripples" and art.motif({"subject_kind": "place"}) == "horizon"
    assert art.motif({"subject_kind": "happening"}) == "burst"


def test_a_drawing_project_is_worked_by_drawing_and_its_pieces_are_drawings(town):
    e, b, seen = _studio_with_a_drawing(town, ["add to a drawing of the fountain", "call a drawing of the fountain finished"])
    with e.conn:
        e.conn.execute("INSERT INTO projects(title,activity,why,status,progress,note,formed_day,updated_at,medium) "
                       "VALUES('the fountain series','drawing','it keeps drawing it','active',0,'','2026-11-18','',"
                       "'drawing')")
    assert not any(m.verb == "work_on" for m in e._menu())                # its work is drawing itself
    e.tick()
    p = dict(e.conn.execute("SELECT * FROM projects").fetchone())
    assert p["progress"] > 0 and p["ticks_worked"] == 1
    e.tick()
    a = dict(e.conn.execute("SELECT * FROM artifacts").fetchone())
    assert a["kind"] == "drawing" and a["drawing_id"] == drawing_row(e)["id"]
    assert not any(t == "create" for t, _ in b.calls)                    # no LLM wrote the piece


def test_an_older_world_gets_the_new_back_room(town):
    e, b, seen = engine(town, [])
    with e.conn:
        e.conn.execute("DELETE FROM objects WHERE area='studio'")         # a world seeded before the studio existed
    e.conn.close()
    e2 = Engine(town, b, rng=random.Random(2))
    assert {"paper", "worktable", "corkboard"} <= set(e2.world.objects)
    assert json.loads(e2.conn.execute("SELECT tags FROM objects WHERE id='paper'").fetchone()[0]) == ["paper"]


def test_the_drawing_tool_keeps_only_valid_marks_with_what_it_has(town):
    charcoal = DW.colours_here(town, [{"tags": ["paper"]}])
    assert "blue" not in charcoal and "black" in charcoal                 # no paints here: charcoal only
    raw = {"marks": [{"shape": "circle", "x": 140, "y": -5, "size": 30, "colour": "black"},
                     {"shape": "wave", "x": 50, "y": 50, "size": 40, "colour": "blue"},        # a colour it lacks
                     {"shape": "explode", "x": 1, "y": 1, "size": 1, "colour": "black"}]       # not a shape
           + [{"shape": "dots", "x": 10, "y": 10, "size": 10, "colour": "grey"}] * 20, "going_for": "the glint"}
    out = DW.parse_marks(town, raw, 5, charcoal)
    assert len(out["marks"]) == 5 and out["marks"][0]["x"] == 100 and out["marks"][0]["y"] == 0
    assert all(m["colour"] in charcoal for m in out["marks"]) and out["going_for"] == "the glint"
    assert out["marks"][0]["layer"] == "middle"                           # the default distance
    pts = DW.parse_marks(town, {"marks": [{"shape": "outline", "x": 0, "y": 0, "size": 9, "colour": "black",
                                           "points": [[10, 10], [200, 50], [50, 90], "bad"]}]}, 5, charcoal)
    assert pts["marks"][0]["points"] == [[10, 10], [100, 50], [50, 90]]     # a freehand contour, clamped
    with pytest.raises(Exception):
        DW.parse_marks(town, {"marks": [{"shape": "line", "x": 1, "y": 1, "size": 1, "colour": "blue"}]}, 5, charcoal)


def test_a_sitting_places_its_own_marks_from_what_is_in_the_room(town):
    seen = []
    e, b, _ = engine(town, ["go to the stationer's back room", "start a drawing of the fountain"])
    b.handlers["sketch"] = lambda m, s: (seen.append(m[-1]["content"]), {
        "marks": [{"shape": "wash", "x": 50, "y": 60, "size": 80, "colour": "teal", "about": "the water"}],
        "going_for": "how still it is"})[1]
    e.state["area"] = "fountain"
    e.tick()
    e.tick()
    s = drawing_row(e)["sittings"][0]
    assert s["marks"][0]["colour"] == "teal" and s["going_for"] == "how still it is"   # the watercolours are here
    assert "The sheet is blank." in seen[0] and "teal" in seen[0] and "What you know of it:" in seen[0]
    assert 'fill="#2f8a83"' in art.render_svg(town, drawing_row(e))

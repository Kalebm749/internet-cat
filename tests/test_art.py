"""Nit's drawing system (docs/rewrite-contracts.md §4.10): the simulated materials, the operation checks, Gwen,
versions that are never overwritten, and the website."""
import io
import json
import random

import numpy as np
import pytest
from PIL import Image

from cat import artifacts, content, knowledge, life, store, tuning
from cat.art import operations, render
from cat.capabilities import CapContext, Registry
from cat.capabilities.draw import picked_up
from cat.decision import Decision
from cat.execute import Executor, load_texts
from cat.llm import MockBackend
from cat.scheduler import Scheduler

SUPPLIES = content.load().facts["art_room.supplies"].default
LIMITS = {"max_points": 120, "max_per_pass": 400, "max_total": 600, "max_points_per_pass": 20000}


def draw(ops, seed=3):
    good, probs = operations.clean(ops, SUPPLIES, LIMITS)
    assert not probs, probs
    png, empty = render.render(good, SUPPLIES, seed)
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), np.float32) / 255, empty


def line(tool, pressure=0.8, y=256, straight=False, **kw):
    pts = [[60, y], [450, y]] if straight else [[60, y], [200, y - 6], [330, y + 6], [450, y]]
    return {"op": "stroke", "tool": tool, "points": pts, "pressure": pressure, **kw}


def band(img, y=256, half=14):
    return img[y - half:y + half, 120:400]


def ink_amount(img):
    """How far each pixel is from the paper (0 = untouched)."""
    return np.clip(0.94 - img.mean(axis=2), 0, 1)


# ---------------------------------------------------------------- materials


def test_the_three_materials_look_different():
    blank, _ = draw([])
    pencil, _ = draw([line("pencil_2b", straight=True)])
    marker, _ = draw([line("marker_blue", straight=True)])
    crayon, _ = draw([line("crayon_blue", straight=True)])

    def touched(img):
        return np.abs(img - blank).sum(axis=2) > 0.12

    p, m, c = (band(touched(x)[..., None])[..., 0] for x in (pencil, marker, crayon))
    assert p.sum() < m.sum() * 0.6                               # pencil lines are thin
    sat = lambda img, mask: float((img.max(axis=2) - img.min(axis=2))[mask].mean())
    assert sat(band(pencil), p) < 0.08 < sat(band(marker), m)     # graphite is grey, marker is colour
    # a marker's colour is even; wax leaves gaps and grain inside the stroke
    core = lambda img: band(img, half=2).mean(axis=2)
    assert core(crayon).std() > 2 * core(marker).std()
    assert (band(touched(crayon)[..., None])[..., 0] == 0).sum() > 0


def test_pencil_pressure_and_hardness():
    light, _ = draw([line("pencil_hb", 0.2)])
    firm, _ = draw([line("pencil_hb", 0.9)])
    soft, _ = draw([line("pencil_4b", 0.9)])
    d = lambda img: float(ink_amount(band(img)).max())
    assert d(light) < d(firm) < d(soft)
    once, _ = draw([line("pencil_hb", 0.5)])
    thrice, _ = draw([line("pencil_hb", 0.5)] * 3)
    assert ink_amount(band(thrice)).sum() > 1.5 * ink_amount(band(once)).sum()     # strokes build up


def test_crayon_pressure_fills_the_grain():
    light, _ = draw([line("crayon_red", 0.15, width=14)])
    firm, _ = draw([line("crayon_red", 1.0, width=14)])
    cover = lambda img: float((ink_amount(band(img, half=4)) > 0.05).mean())
    assert cover(light) < 0.7 * cover(firm)


def test_marker_overlaps_darken():
    one, _ = draw([line("marker_yellow")])
    two, _ = draw([line("marker_yellow"), line("marker_yellow")])
    assert ink_amount(band(two, half=2)).mean() > ink_amount(band(one, half=2)).mean() * 1.15


def test_the_eraser_lifts_pencil_more_than_marker():
    rub = {"op": "erase", "tool": "eraser", "points": [[100, 256], [420, 256]], "pressure": 1.0, "width": 30}
    pencil, _ = draw([line("pencil_2b")])
    pencil_rubbed, _ = draw([line("pencil_2b"), rub])
    marker, _ = draw([line("marker_black")])
    marker_rubbed, _ = draw([line("marker_black"), rub])
    lifted = lambda a, b: 1 - ink_amount(band(b)).sum() / ink_amount(band(a)).sum()
    assert lifted(pencil, pencil_rubbed) > 0.6
    assert lifted(marker, marker_rubbed) < 0.3
    assert ink_amount(band(pencil_rubbed)).sum() > 0             # a faint ghost stays


def test_fills_stay_inside_the_lines_and_paper_is_textured():
    img, empty = draw([{"op": "shape", "tool": "crayon_black", "shape": "ellipse", "cx": 256, "cy": 256, "rx": 90,
                        "ry": 90, "pressure": 0.9},
                       {"op": "fill", "tool": "marker_red", "x": 256, "y": 256, "pressure": 0.8}])
    assert empty == []
    inside, outside = img[236:276, 236:276], img[20:60, 20:60]
    assert inside[..., 0].mean() - inside[..., 2].mean() > 0.3   # red inside
    assert abs(outside[..., 0].mean() - outside[..., 2].mean()) < 0.1
    blank, _ = draw([])
    assert 0.003 < blank.mean(axis=2).std() < 0.03                 # visible but restrained grain
    assert blank[..., 0].mean() > blank[..., 2].mean()             # warm white


def test_replay_is_deterministic_and_needs_no_gpu():
    ops = [line("pencil_hb"), line("crayon_green", 0.5, y=300),
           {"op": "fill", "tool": "crayon_yellow", "x": 256, "y": 100, "pressure": 0.6}]
    good, _ = operations.clean(ops, SUPPLIES, LIMITS)
    a, _ = render.render(good, SUPPLIES, 11)
    b, _ = render.render(good, SUPPLIES, 11)
    c, _ = render.render(good, SUPPLIES, 12)
    assert a == b and a != c
    assert Image.open(io.BytesIO(a)).size == (512, 512)


# ---------------------------------------------------------------- checking operations


@pytest.mark.parametrize("op, problem", [
    ({"op": "paint", "tool": "pencil_hb"}, "op must be one of"),
    ({"op": "stroke", "tool": "spray_can", "points": [[0, 0], [9, 9]]}, "not one of the supplies"),
    ({"op": "stroke", "tool": "pencil_hb", "points": [[0, 0], [900, 9]]}, "outside 0..512"),
    ({"op": "stroke", "tool": "pencil_hb", "points": [[0, 0]]}, "points must be a list of 2"),
    ({"op": "stroke", "tool": "pencil_hb", "points": [[0, 0], [9, 9]], "code": "import os"}, "unknown fields"),
    ({"op": "erase", "tool": "pencil_hb", "points": [[0, 0], [9, 9]]}, "erase needs an eraser"),
    ({"op": "stroke", "tool": "eraser", "points": [[0, 0], [9, 9]]}, "cannot draw"),
    ({"op": "stroke", "tool": "marker_red", "points": [[0, 0], [9, 9]], "width": 400}, "width for marker"),
    ({"op": "shape", "tool": "pencil_hb", "shape": "rect", "x": 500, "y": 10, "w": 100, "h": 10}, "stay on the paper"),
    ({"op": "fill", "tool": "crayon_red", "x": "middle", "y": 3}, "outside 0..512"),
])
def test_malformed_operations_are_rejected(op, problem):
    good, probs = operations.clean([op], SUPPLIES, LIMITS)
    assert good == [] and problem in probs[0], probs


def test_operation_limits():
    ops = [line("pencil_hb")] * 10
    good, probs = operations.clean(ops, SUPPLIES, {**LIMITS, "max_per_pass": 4})
    assert len(good) == 4 and len(probs) == 6
    good, _ = operations.clean(ops, SUPPLIES, {**LIMITS, "max_total": 5}, already=3)
    assert len(good) == 2
    assert operations.clean("draw a cat", SUPPLIES, LIMITS) == ([], ["operations must be a list"])


def test_garbage_from_a_model_never_breaks_the_renderer():
    rng = random.Random(5)
    junk = [None, "x", 3, [], {}, {"op": "fill"}, {"op": "stroke", "tool": "pencil_hb", "points": "1,2"}]
    for _ in range(300):
        op = {"op": rng.choice(["stroke", "shape", "fill", "erase", "nope"]),
              "tool": rng.choice([s["id"] for s in SUPPLIES] + ["?"]),
              "points": [[rng.uniform(-50, 600), rng.uniform(-50, 600)] for _ in range(rng.randint(0, 6))],
              "pressure": rng.choice([rng.uniform(-1, 2), "hard", None]),
              "shape": rng.choice(["line", "rect", "ellipse", "polygon", "star"]),
              "x": rng.uniform(-10, 520), "y": rng.uniform(-10, 520), "w": rng.uniform(-5, 300), "h": rng.uniform(0, 300),
              "cx": rng.uniform(0, 512), "cy": rng.uniform(0, 512), "rx": rng.uniform(-3, 300), "ry": rng.uniform(0, 300)}
        junk.append({k: v for k, v in op.items() if rng.random() < 0.8 or k in ("op", "tool")})
    good, probs = operations.clean(junk, SUPPLIES, {**LIMITS, "max_per_pass": 400})
    png, _ = render.render(good, SUPPLIES, 1)
    assert Image.open(io.BytesIO(png)).size == (512, 512) and probs


# ---------------------------------------------------------------- Nit's choices


def test_nit_s_words_choose_the_supplies():
    ids = lambda words: sorted(s["id"] for s in picked_up(SUPPLIES, words))
    assert ids("the red and blue crayons") == ["crayon_blue", "crayon_red"]
    assert ids("a 2B pencil for the outline, then crayons") == sorted(
        ["pencil_2b"] + [s["id"] for s in SUPPLIES if s["kind"] == "crayon"])
    assert ids("just the black marker and the eraser") == ["eraser", "marker_black"]
    assert len(ids("whatever feels right")) == len(SUPPLIES)   # names nothing: everything within reach


# ---------------------------------------------------------------- Gwen, versions, persistence


class Room:
    def __init__(self, tmp_path):
        self.t = tuning.load()
        self.conn = store.connect(tmp_path / "nit.db")
        self.life = life.open_life(self.conn, content.load(), self.t)
        self.life.content.events.clear()
        self.life.location = "art_room"
        self.backend = MockBackend(seed=1)
        self.ex = Executor(self.conn, self.life, self.t, load_texts(), Registry(self.life.content),
                           Scheduler(self.t["decisions"]), random.Random(0), self.backend)
        with store.transaction(self.conn):
            knowledge.arrive(self.conn, self.life.content, "art_room", self.life.sim_time)

    def do(self, action, **inputs):
        d = Decision(kind="full", action=action, inputs=inputs, thought="")
        cap = self.ex.prepare(d)
        with store.transaction(self.conn):
            res = self.ex.start(d, None, cap)
            self.ex.advance(40)
            life.save(self.conn, self.life)
        return res

    def works(self):
        return self.life.world.get("art_room.artworks")


@pytest.fixture
def room(tmp_path):
    return Room(tmp_path)


def test_gwen_makes_a_first_drawing_from_nit_s_intention(room):
    seen = []

    def gwen(msgs, schema):
        seen.append((msgs, schema))
        return {"title": "Sun over water", "description": "A yellow circle above a blue line.", "operations": [
            {"op": "shape", "tool": "crayon_yellow", "shape": "ellipse", "cx": 256, "cy": 180, "rx": 70, "ry": 70,
             "pressure": 0.8},
            {"op": "fill", "tool": "crayon_yellow", "x": 256, "y": 180, "pressure": 0.7},
            {"op": "stroke", "tool": "marker_blue", "points": [[40, 380], [470, 380]], "pressure": 0.9},   # not picked up
            {"op": "stroke", "tool": "crayon_blue", "points": [[40, 380], [470, 380]], "pressure": 0.9}]}
    room.backend.handlers["capability"] = gwen
    res = room.do("make_drawing", intention="the sun I remember over the fountain", thoughts="warm and quiet",
                  materials="the yellow and blue crayons", effort="careful")
    assert res["status"] == "started"
    msgs, schema = seen[0]
    assert "the sun I remember over the fountain" in msgs[1]["content"]
    assert sorted(schema["properties"]["operations"]["items"]["properties"]["tool"]["enum"]) == ["crayon_blue", "crayon_yellow"]
    piece = room.works()[-1]
    assert piece["title"] == "Sun over water" and piece["format"] == "png" and piece["status"] == "unfinished"
    stored = artifacts.load(room.conn, piece["id"])
    assert stored["materials"] == ["a yellow crayon", "a blue crayon"]       # what was really used
    assert len(stored["meta"]["ops"]) == 3 and "not one of the supplies" in stored["meta"]["dropped"][0]
    assert Image.open(io.BytesIO(stored["data"])).format == "PNG"
    cap = json.loads(room.conn.execute("SELECT capability FROM activities ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert "data" not in cap["artifact"] and "meta" not in cap["artifact"]   # the activity keeps a reference


def test_a_new_pass_is_a_new_version_and_the_old_one_stays(room):
    room.do("make_drawing", intention="a round thing", thoughts="", materials="pencils", effort="quick")
    first = room.works()[-1]
    first_row = artifacts.load(room.conn, first["id"])
    seen = []

    def gwen(msgs, schema):
        seen.append(msgs[1]["content"])
        return {"title": "A round thing, shaded", "description": "The circle, now hatched.", "operations": [
            {"op": "fill", "tool": "pencil_4b", "x": 256, "y": 256, "pressure": 0.6, "cross": True}]}
    room.backend.handlers["capability"] = gwen
    room.do("work_on_drawing", artwork=first["id"], intention="shade it", thoughts="", materials="the 4B pencil",
            effort="quick")
    second = room.works()[-1]
    assert second["revision_of"] == first["id"] and second["root_id"] == first["id"]
    assert [w["status"] for w in room.works() if w["id"] == first["id"]] == ["superseded"]
    assert "operations already on the paper" in seen[0]
    new_row = artifacts.load(room.conn, second["id"])
    assert new_row["meta"]["ops"][:len(first_row["meta"]["ops"])] == first_row["meta"]["ops"]
    assert len(new_row["meta"]["ops"]) == len(first_row["meta"]["ops"]) + 1
    assert artifacts.load(room.conn, first["id"])["data"] == first_row["data"]          # never overwritten
    # the stored history replays to exactly the stored image
    m = new_row["meta"]
    png, _ = render.render(m["ops"], m["supplies"], m["seed"], m["size"])
    assert png == new_row["data"]
    assert [v["id"] for v in artifacts.versions(room.conn, first["id"])] == [first["id"], second["id"]]


def test_nit_can_look_back_at_how_a_drawing_developed(room):
    room.do("make_drawing", intention="a tree", thoughts="", materials="green crayon", effort="quick")
    first = room.works()[-1]
    room.do("work_on_drawing", artwork=first["id"], intention="add a trunk", thoughts="", materials="brown crayon",
            effort="quick")
    second = room.works()[-1]
    room.ex.perception.clear()
    room.do("study_drawing", artwork=second["id"])
    seen = " ".join(room.ex.perception)
    assert "Version 1" in seen and "Version 2" in seen and "green crayon" in seen and "brown crayon" in seen


def test_nit_sees_the_png_itself_and_the_vision_model_is_unloaded_after(room):
    room.do("make_drawing", intention="a tree", thoughts="", materials="green crayon", effort="quick")
    got = []
    room.backend.handlers["look_at_drawing"] = lambda msgs, schema: (got.append(msgs), {"seen": "A green blob on a stick."})[1]
    room.ex.perception.clear()
    room.do("study_drawing", artwork=room.works()[-1]["id"])
    seen = " ".join(room.ex.perception)
    assert "you see: A green blob on a stick." in seen and "Version 1" in seen
    assert got[0][1]["images"] and ("look_at_drawing", "qwen2.5vl:3b") in room.backend.calls


def test_without_eyes_nit_still_has_the_descriptions(room):
    room.do("make_drawing", intention="a tree", thoughts="", materials="green crayon", effort="quick")
    room.backend.fail_next = 1
    room.ex.perception.clear()
    room.do("study_drawing", artwork=room.works()[-1]["id"])
    seen = " ".join(room.ex.perception)
    assert "Version 1" in seen and "you see:" not in seen


def test_ollama_gets_keep_alive_zero_for_the_vision_model():
    from cat.llm import OllamaBackend
    sent = []
    b = OllamaBackend.__new__(OllamaBackend)
    b.s = {"llm_temperature": {}, "llm_num_ctx": 4096}
    b._post = lambda path, payload: (sent.append(payload), {"message": {"content": '{"seen": "x"}'}})[1]
    b._notify_call = lambda e: None
    assert b.chat_json_with_options("look_at_drawing", "qwen2.5vl:3b", [{"role": "user", "content": "hi"}],
                                    {}, {"keep_alive": 0}) == {"seen": "x"}
    assert sent[0]["keep_alive"] == 0 and "keep_alive" not in sent[0]["options"]


def test_no_drawing_is_made_without_nit_asking(room):
    room.do("make_drawing", intention="a stone", thoughts="", materials="pencil", effort="quick")
    n = len(room.works())
    with store.transaction(room.conn):
        room.ex.advance(600)                        # time passes; nothing revisits it on its own
    assert len(room.works()) == n and room.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


def test_unusable_gwen_output_fails_cleanly(room):
    room.backend.handlers["capability"] = lambda m, s: {"title": "x", "description": "y",
                                                       "operations": [{"op": "stroke", "tool": "laser"}]}
    cap = room.ex.prepare(Decision(kind="full", action="make_drawing",
                                   inputs={"intention": "a", "thoughts": "", "materials": "", "effort": "quick"}))
    assert cap is not None and not cap.ok                      # the registry turns the failure into "it didn't work"
    res = room.do("make_drawing", intention="a", thoughts="", materials="", effort="quick")
    assert res["status"] == "rejected" and room.works() == []
    assert room.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


# ---------------------------------------------------------------- the website


def test_the_website_shows_png_and_old_svg_artwork(room, cfg):
    from fastapi.testclient import TestClient
    from cat.web.app import create_app
    room.do("make_drawing", intention="a boat", thoughts="", materials="markers", effort="quick")
    first = room.works()[-1]
    room.do("work_on_drawing", artwork=first["id"], intention="add waves", thoughts="", materials="blue marker",
            effort="quick")
    second = room.works()[-1]
    old = {"id": "old1", "type": "drawing", "title": "An old one", "created_at": "2026-11-20T09:00",
           "description": "From before.", "status": "completed",
           "svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><circle cx="9" cy="9" r="4"/></svg>'}
    room.life.world.append_raw("art_room.artworks", old)
    with store.transaction(room.conn):
        life.save(room.conn, room.life)
    cfg.settings["nit"]["db_path"] = str(room.conn.execute("PRAGMA database_list").fetchone()[2])
    c = TestClient(create_app(cfg))
    page = c.get("/artwork").text
    assert f'/artwork/image/{second["id"]}' in page and "/artwork/image/old1" in page
    img = c.get(f"/artwork/image/{second['id']}")
    assert img.headers["content-type"] == "image/png" and img.content == artifacts.load(room.conn, second["id"])["data"]
    assert c.get(f"/artwork/image/{second['id']}.png").status_code == 200
    svg = c.get("/artwork/image/old1.svg")
    assert svg.headers["content-type"].startswith("image/svg+xml") and "sandbox" in svg.headers["content-security-policy"]
    detail = c.get(f"/artwork/{second['id']}").text
    assert "How it developed" in detail and "Version 2" in detail and first["id"] in detail
    assert c.get("/artwork/old1").status_code == 200 and c.get("/artwork/nope").status_code == 404


def test_more_passes_on_one_drawing_are_allowed_with_diminishing_returns(room):
    room.do("make_drawing", intention="a round thing", thoughts="", materials="pencils", effort="quick")
    fresh = []
    for k in range(3):
        current = room.works()[-1]
        res = room.do("work_on_drawing", artwork=current["id"], intention=f"refine it {k}", thoughts="",
                      materials="pencils", effort="quick")
        assert res["status"] == "started"                          # continuing is always allowed
        row = room.conn.execute("SELECT freshness, repeat_parts FROM activities WHERE id = ?",
                                (res["activity_id"],)).fetchone()
        assert "restart" not in json.loads(row["repeat_parts"])     # never treated as doing it again at once
        fresh.append(row["freshness"])
    assert fresh == sorted(fresh, reverse=True) and fresh[-1] < fresh[0] and fresh[0] > 0.6
    assert len([w for w in room.works() if w["status"] == "superseded"]) == 3

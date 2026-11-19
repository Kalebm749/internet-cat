"""Projects on the website and the dev dashboard."""
import random
import re

import pytest
from fastapi.testclient import TestClient

from cat import db
from cat.devdash import data
from cat.devdash.app import create_app as create_dev
from cat.engine import Engine
from cat.llm import MockBackend
from cat.web.app import create_app as create_web


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)


def make(cfg, project=None):
    cfg.world["events"] = []
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": "draw", "intent": "",
                                        "feeling": "neutral", "wants_sleep": False}
    b.handlers["narrator"] = lambda m, s: {"outcome": "It draws.", "gains": {}, "effort": "low", "duration": 6,
                                           "changes": [], **({"project": project} if project else {})}
    e = Engine(cfg, b, rng=random.Random(1))
    e.world.events = None
    return e


def add(e, title, status="active", progress=10.0, why="likes it", note=""):
    e.conn.execute("INSERT INTO projects(title,activity,why,status,progress,note,formed_day,updated_at) "
                   "VALUES(?,?,?,?,?,?,'2026-11-18','t')", (title, title, why, status, progress, note))


def test_empty_states(cfg):
    e = make(cfg)
    e.tick()
    s = TestClient(create_web(cfg)).get("/api/state").json()["projects"]
    assert s["rows"] == [] and s["empty"] and s["artifacts"] == [] and s["artifacts_empty"]
    html = TestClient(create_web(cfg)).get("/").text
    assert 'id="projects-card"' in html and "no projects yet." in html and "nothing made yet." in html


def test_projects_show_stage_words_and_status_never_the_number(cfg):
    e = make(cfg)
    e.tick()
    add(e, "drawing the old tree", progress=60.0, note="the trunk is done")
    add(e, "learning a song", status="paused", progress=10.0)
    add(e, "a finished thing", status="done", progress=100.0)
    add(e, "given up", status="dropped")
    s = TestClient(create_web(cfg)).get("/api/state").json()["projects"]
    rows = {r["title"]: r for r in s["rows"]}
    assert set(rows) == {"drawing the old tree", "learning a song", "a finished thing"}        # dropped: hidden
    assert rows["drawing the old tree"]["stage"] == "well along" and rows["drawing the old tree"]["status_label"] == "working on it"
    assert rows["learning a song"]["status_label"] == "set aside"
    assert rows["a finished thing"]["stage"] == "finished"
    assert [r["status"] for r in s["rows"]] == ["active", "paused", "done"]                       # active first
    assert all("progress" not in r for r in s["rows"])
    html = TestClient(create_web(cfg)).get("/").text
    assert "working on it · well along" in html and "the trunk is done" in html


def test_artifacts_are_listed_newest_first_and_capped(cfg):
    e = make(cfg)
    e.tick()
    add(e, "writing")
    for i in range(cfg.settings["projects"]["artifacts_shown"] + 3):
        e.conn.execute("INSERT INTO artifacts(project_id,tick_id,kind,title,text,created_sim_time) "
                       "VALUES(1,1,'passage',?,?,'2026-11-18T09:00')", (f"piece {i}", f"text {i}"))
    s = TestClient(create_web(cfg)).get("/api/state").json()["projects"]
    assert len(s["artifacts"]) == cfg.settings["projects"]["artifacts_shown"]
    assert s["artifacts"][0]["title"] == f"piece {cfg.settings['projects']['artifacts_shown'] + 2}"
    assert s["artifacts"][0]["project"] == "writing" and s["artifacts"][0]["day_text"] == "Day 1"
    html = TestClient(create_web(cfg)).get("/").text
    assert "af-text" in html and "text 8" in html and "text 0" not in html


def test_a_made_piece_shows_in_the_diary_entry_and_the_panel(cfg):
    e = make(cfg, project={"id": 1, "advance": "+", "artifact": {"kind": "sketch", "title": "Roots",
                                                               "text": "Deep roots."}})
    add(e, "drawing the old tree", progress=0.0)
    e.tick()
    c = TestClient(create_web(cfg))
    entry = c.get("/api/state").json()["diary"][0]
    assert {"op": "make", "label": "Roots", "text": "made “Roots”"} in entry["world_changes"]
    assert "Deep roots." in c.get("/").text


def test_an_older_database_without_the_tables_still_renders(cfg):
    e = make(cfg)
    e.tick()
    e.conn.execute("DROP TABLE artifacts")
    e.conn.execute("DROP TABLE projects")
    r = TestClient(create_web(cfg)).get("/api/state")
    assert r.status_code == 200 and r.json()["projects"]["empty"]


def test_the_live_markup_templates_exist(cfg):
    html = TestClient(create_web(cfg)).get("/").text
    assert 'id="tpl-project"' in html and 'id="tpl-artifact"' in html


# ---------------- dev dashboard ----------------
def test_the_projects_page_and_its_json_twin(cfg):
    e = make(cfg, project={"id": 1, "advance": "++", "note": "trunk done",
                           "artifact": {"kind": "sketch", "title": "Roots", "text": "Deep roots."}})
    add(e, "drawing the old tree", progress=0.0)
    e.tick()
    c = TestClient(create_dev(cfg))
    html = c.get("/projects").text
    assert "drawing the old tree" in html and "Roots" in html and "trunk done" in html and "exempt from the repetition" in html
    j = c.get("/api/dev/projects").json()
    assert j["projects"][0]["progress"] == 16 and j["projects"][0]["artifact_count"] == 1
    assert j["artifacts"][0]["title"] == "Roots" and j["settings"]["taper"] == 0.5
    assert any(href == "/projects" for _, _, href in __import__("cat.devdash.app", fromlist=["NAV"]).NAV)


def test_tick_detail_explains_the_project_report(cfg):
    e = make(cfg, project={"id": 1, "advance": "++", "note": "trunk done"})
    add(e, "drawing the old tree", progress=0.0)
    tid = e.tick()["id"]
    d = data.tick_detail(cfg, tid)
    assert d["project"]["ok"] and d["project"]["progress_after"] == 16 and d["project"]["repetition_exempt"]
    html = TestClient(create_dev(cfg)).get(f"/ticks/{tid}").text
    assert 'id="project"' in html and "Project work" in html and "exempt" in html
    assert re.search(r"0\.0 → <b>16\.0</b>|0 → <b>16", html) or "16" in html


def test_a_rejected_report_is_shown_on_the_tick_page(cfg):
    e = make(cfg, project={"id": 9, "advance": "++"})
    tid = e.tick()["id"]
    html = TestClient(create_dev(cfg)).get(f"/ticks/{tid}").text
    assert "Rejected: no active project with id 9" in html


def test_ticks_without_project_reports_have_no_section(cfg):
    e = make(cfg)
    tid = e.tick()["id"]
    assert data.tick_detail(cfg, tid)["project"] is None
    assert 'id="project"' not in TestClient(create_dev(cfg)).get(f"/ticks/{tid}").text

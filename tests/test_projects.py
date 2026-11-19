"""Long-term projects: formed in sleep, idle upkeep, validated advances, saved artifacts."""
import json
import random

import pytest

from cat import db, projects as PJ, sleep
from cat.engine import Engine
from cat.llm import MockBackend


def row(i, title, status="active", progress=0.0, worked=0, idle=0, why="it likes it"):
    return {"id": i, "title": title, "status": status, "progress": progress, "worked_today": worked,
            "idle_nights": idle, "why": why, "note": ""}


# ---------- forming a project in sleep ----------
def test_no_or_garbage_proposal_changes_nothing(cfg):
    for raw in (None, [], {}, "x", ["str"], [{"op": "dance"}]):
        assert PJ.decide_ops(cfg, [], raw, "d")["action"] in ("none", "rejected")


def test_start_a_project(cfg):
    out = PJ.decide_ops(cfg, [], [{"op": "start", "title": "  drawing   the   old tree ",
                                   "activity": "drawing", "why": "it keeps coming back"}], "2026-11-18")
    assert out == {"action": "start", "title": "drawing the old tree", "activity": "drawing",
                   "why": "it keeps coming back", "formed_day": "2026-11-18"}


def test_start_is_limited_by_max_active_and_duplicates_and_empty_titles(cfg):
    start = [{"op": "start", "title": "writing"}]
    assert PJ.decide_ops(cfg, [row(1, "drawing")], start, "d")["action"] == "rejected"      # max_active is 1
    cfg.settings["projects"]["max_active"] = 2
    assert PJ.decide_ops(cfg, [row(1, "drawing")], start, "d")["action"] == "start"
    assert "already a project" in PJ.decide_ops(cfg, [row(1, "Writing")], start, "d")["reason"]
    assert PJ.decide_ops(cfg, [], [{"op": "start", "title": "  "}], "d")["reason"] == "empty title"


def test_pause_resume_drop_need_a_real_project_in_the_right_state(cfg):
    ex = [row(1, "a"), row(2, "b", status="paused")]
    assert PJ.decide_ops(cfg, ex, [{"op": "pause", "id": 1}], "d")["action"] == "pause"
    assert PJ.decide_ops(cfg, ex, [{"op": "pause", "id": 2}], "d")["reason"] == "not active"
    assert PJ.decide_ops(cfg, ex, [{"op": "resume", "id": 1}], "d")["reason"] == "not paused"
    assert PJ.decide_ops(cfg, ex, [{"op": "resume", "id": 2}], "d")["reason"] == "max_active reached"
    assert PJ.decide_ops(cfg, [row(2, "b", status="paused")], [{"op": "resume", "id": 2}], "d")["action"] == "resume"
    assert PJ.decide_ops(cfg, ex, [{"op": "drop", "id": 2}], "d")["action"] == "drop"
    assert "no open project" in PJ.decide_ops(cfg, ex, [{"op": "drop", "id": 99}], "d")["reason"]


def test_only_the_first_proposal_counts(cfg):
    out = PJ.decide_ops(cfg, [], [{"op": "start", "title": "one"}, {"op": "start", "title": "two"}], "d")
    assert out["title"] == "one"


def test_idle_upkeep_counts_idle_nights_and_pauses(cfg):
    cfg.settings["projects"]["pause_after_idle_nights"] = 3
    up = PJ.idle_upkeep(cfg, [row(1, "a", worked=1, idle=2), row(2, "b", idle=1), row(3, "c", idle=2),
                              row(4, "d", status="paused", idle=0)])
    assert [(u["id"], u["idle_nights"], u["pause"]) for u in up] == [(1, 0, False), (2, 2, False), (3, 3, True)]
    cfg.settings["projects"]["pause_after_idle_nights"] = 0
    assert not any(u["pause"] for u in PJ.idle_upkeep(cfg, [row(3, "c", idle=50)]))


def test_stage_words_never_numbers(cfg):
    assert [PJ.stage_word(p, cfg) for p in (0, 24, 25, 60, 90, 100)] == \
        ["just begun", "just begun", "underway", "well along", "nearly done", "nearly done"]


# ---------- the sleep cycle stores it ----------
def make(cfg, seed=1):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    return Engine(cfg, b, rng=random.Random(seed)), b


def sleep_once(e):
    e.state["sim_time"] = "2026-11-18T23:00"
    e.state["energy"] = -45.0
    e.world.events = None
    e.tick()


def test_sleep_starts_a_project_and_traces_the_proposal(cfg):
    cfg.world["events"] = []
    e, b = make(cfg)
    e.tick()
    b.handlers["projects"] = lambda m, s: {"projects": [{"op": "start", "title": "drawing the old tree",
                                                         "activity": "drawing", "why": "it keeps returning"}]}
    sleep_once(e)
    p = PJ.active(e.conn)
    assert [(x["title"], x["status"], x["progress"], x["formed_day"]) for x in p] == \
        [("drawing the old tree", "active", 0.0, "2026-11-18")]
    t = e.conn.execute("SELECT * FROM sleep_traces WHERE step='projects'").fetchone()
    assert json.loads(t["proposed"])[0]["op"] == "start"
    assert json.loads(t["clamped"])["op"]["action"] == "start"


def test_a_rejected_proposal_is_logged_and_changes_nothing(cfg):
    cfg.world["events"] = []
    e, b = make(cfg)
    e.tick()
    b.handlers["projects"] = lambda m, s: {"projects": [{"op": "drop", "id": 42}]}
    sleep_once(e)
    assert PJ.open_projects(e.conn) == []
    t = e.conn.execute("SELECT clamped FROM sleep_traces WHERE step='projects'").fetchone()
    assert json.loads(t["clamped"])["op"]["action"] == "rejected"


def test_an_untouched_project_is_paused_after_enough_nights(cfg):
    cfg.world["events"] = []
    cfg.settings["projects"]["pause_after_idle_nights"] = 2
    e, b = make(cfg)
    e.tick()
    e.conn.execute("INSERT INTO projects(title,activity,why,status,progress,note,formed_day,updated_at) "
                   "VALUES('x','x','y','active',10,'', '2026-11-17','t')")
    b.handlers["projects"] = lambda m, s: {"projects": []}
    sleep_once(e)
    assert PJ.get(e.conn, 1)["status"] == "active" and PJ.get(e.conn, 1)["idle_nights"] == 1
    e.state["is_asleep"] = False
    e.conn.execute("UPDATE meta SET value='' WHERE key='sleep_started'")
    sleep_once(e)
    assert PJ.get(e.conn, 1)["status"] == "paused"


def test_the_sleep_prompt_asks_for_one_change_and_a_real_repeated_enjoyed_activity(cfg):
    msgs = sleep.projects_messages(cfg, "A day.", ["a log"], ["drawing"], [row(3, "writing", progress=30)])
    text = msgs[-1]["content"]
    assert "ONE" in text and "[3] writing (active, underway)" in text and "- drawing" in text
    assert "researching, drawing, writing, making music" in text


def test_project_settings_are_validated(tmp_path):
    from tests.test_config import _problems
    assert "projects.max_active: must be an integer >= 1" in _problems(
        tmp_path, "settings.yaml", lambda s: s["projects"].update(max_active=0))
    assert "projects.taper: must be a number 0..1" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["projects"].update(taper=2))
    assert "bucket '+++' is not in gain_values" in _problems(
        tmp_path / "c", "settings.yaml", lambda s: s["projects"]["progress_values"].update({"+++": 5}))
    assert "completion_gains: 'fun' is not a need" in _problems(
        tmp_path / "d", "settings.yaml", lambda s: s["projects"]["completion_gains"].update(fun=5))
    assert "needs a bound above 100" in _problems(
        tmp_path / "e", "settings.yaml", lambda s: s["projects"].update(stages={25: "a", 50: "b"}))

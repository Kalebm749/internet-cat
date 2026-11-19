"""Project work in the tick: the narrator's report, tapering progress, exemption from repetition,
artifacts, completion, and what the brain and narrator are told."""
import json
import random
import re

import pytest

from cat import brain, narrator, projects as PJ
from cat.engine import Engine
from cat.llm import MockBackend
from cat.world import WorldState


def active_project(**kw):
    base = {"id": 1, "title": "drawing the old tree", "status": "active", "progress": 0.0, "note": "",
            "why": "it keeps returning to it", "last_worked": None}
    base.update(kw)
    return base


# ---------- pure: validating and sizing an advance ----------
def test_advance_sizes_come_from_the_bucket_and_taper_near_the_end(cfg):
    pv = cfg.settings["projects"]["progress_values"]
    a0 = PJ.plan_advance(cfg, [active_project()], {"id": 1, "advance": "+"})
    assert a0["ok"] and a0["gained"] == pv["+"] and a0["progress_after"] == pv["+"]
    a50 = PJ.plan_advance(cfg, [active_project(progress=50)], {"id": 1, "advance": "++"})
    assert a50["gained"] == pytest.approx(pv["++"] * (1 - 0.5 * 0.5))                  # taper 0.5 at 50%
    a90 = PJ.plan_advance(cfg, [active_project(progress=90)], {"id": 1, "advance": "++"})
    assert a90["gained"] < a50["gained"]                                              # slower still


def test_zero_advance_changes_no_progress_but_can_update_the_note(cfg):
    a = PJ.plan_advance(cfg, [active_project(progress=20, note="old")], {"id": 1, "advance": "0", "note": "new bit"})
    assert a["ok"] and not a["advanced"] and a["progress_after"] == 20 and a["note"] == "new bit"


def test_milestones_and_completion_fire_once(cfg):
    a = PJ.plan_advance(cfg, [active_project(progress=20)], {"id": 1, "advance": "++"})      # 20 -> ~35: crosses 25
    assert a["milestones"] == [25] and not a["completed"]
    done = PJ.plan_advance(cfg, [active_project(progress=99)], {"id": 1, "advance": "++"})
    assert done["progress_after"] == 100 and done["completed"] and 100 in done["milestones"]
    again = PJ.plan_advance(cfg, [active_project(progress=100)], {"id": 1, "advance": "++"})
    assert not again["completed"]


def test_reports_about_unknown_or_inactive_projects_are_rejected(cfg):
    assert PJ.plan_advance(cfg, [active_project()], None) is None
    assert PJ.plan_advance(cfg, [active_project()], {}) is None
    for rid in (99, "x", None, True):
        r = PJ.plan_advance(cfg, [active_project()], {"id": rid, "advance": "++"})
        assert r["ok"] is False and "no active project" in r["reason"]
    paused = PJ.plan_advance(cfg, [active_project(status="paused")], {"id": 1, "advance": "++"})
    assert paused["ok"] is False


def test_an_unknown_bucket_is_no_progress(cfg):
    a = PJ.plan_advance(cfg, [active_project()], {"id": 1, "advance": "+++"})
    assert a["ok"] and not a["advanced"] and a["gained"] == 0


def test_artifacts_need_real_progress_and_text_and_are_trimmed(cfg):
    art = {"kind": "sketch", "title": "Roots", "text": "  deep   roots  " + "x" * 5000}
    a = PJ.plan_advance(cfg, [active_project()], {"id": 1, "advance": "+", "artifact": art})
    assert a["artifact"]["title"] == "Roots" and a["artifact"]["text"].startswith("deep roots")
    assert len(a["artifact"]["text"]) == cfg.settings["projects"]["artifact_max_length"]
    assert PJ.plan_advance(cfg, [active_project()], {"id": 1, "advance": "0", "artifact": art})["artifact"] is None
    assert PJ.plan_advance(cfg, [active_project()], {"id": 1, "advance": "+", "artifact": {"text": " "}})["artifact"] is None


# ---------- what the LLMs are told ----------
def ctx(cfg, conn, **kw):
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    base = dict(sim_time="2026-11-18T19:40", area_id="living_room", needs={k: 60 for k in cfg.character["needs"]},
                energy=10.0, mood={"description": "calm", "word": "calm"}, tendencies={}, weather="clear",
                objects_here=w.here("living_room"), inventory=[])
    base.update(kw)
    return brain.BrainContext(**base)


def test_the_brain_is_told_what_it_is_in_the_middle_of(cfg, conn):
    pr = [{"title": "drawing the old tree", "why": "it keeps returning to it", "stage": "underway",
           "note": "the trunk is done", "idle": "", "untouched": False}]
    p = brain.situation_prompt(cfg, ctx(cfg, conn, projects=pr))
    assert "## What you're in the middle of" in p
    assert "A long-term project of yours: drawing the old tree (it keeps returning to it). It is underway. " \
           "Where you got to: the trunk is done" in p
    assert "last worked" not in p


def test_the_brain_is_told_when_a_project_was_left_or_not_started(cfg, conn):
    idle = [{"title": "t", "why": "", "stage": "just begun", "note": "", "idle": "about 2 days ago", "untouched": False}]
    assert "You last worked on it about 2 days ago." in brain.situation_prompt(cfg, ctx(cfg, conn, projects=idle))
    new = [{"title": "t", "why": "", "stage": "just begun", "note": "", "idle": "", "untouched": True}]
    assert "You have not started on it yet." in brain.situation_prompt(cfg, ctx(cfg, conn, projects=new))


def test_no_project_section_without_projects_and_never_a_number(cfg, conn):
    assert "in the middle of" not in brain.situation_prompt(cfg, ctx(cfg, conn))
    pr = [{"title": "t", "why": "w", "stage": "well along", "note": "n", "idle": "", "untouched": False}]
    section = brain.situation_prompt(cfg, ctx(cfg, conn, projects=pr)).split("## What you're in the middle of")[1].split("## Around you")[0]
    assert not re.search(r"\d", section)


def test_the_narrator_sees_active_projects_with_ids_and_stage_words(cfg, conn):
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    view = [{"id": 7, "title": "drawing the old tree", "why": "likes it", "stage": "underway", "note": "trunk done"}]
    text = narrator.describe_world(cfg, "2026-11-18T19:40", "garden", "clear", w.here("garden"), [],
                                   {k: 60 for k in cfg.character["needs"]}, view)
    assert "Projects it is working on" in text
    assert "- [7] drawing the old tree (likes it). It is underway. Where it stands: trunk done" in text
    assert not re.search(r"progress|\d+%", text.split("Projects it is working on")[1])
    assert "Projects it is working on" not in narrator.describe_world(
        cfg, "2026-11-18T19:40", "garden", "clear", w.here("garden"), [], {k: 60 for k in cfg.character["needs"]}, [])


def test_the_narrator_prompt_explains_the_project_field(cfg):
    s = narrator.system_prompt(cfg)
    assert "- project: OPTIONAL" in s and "artifact" in s and "{id, advance, note, artifact}" in s


def test_the_brain_output_schema_is_unchanged_by_projects():
    assert set(brain.BRAIN_SCHEMA["properties"]) == {"reaction", "thought", "action", "intent", "feeling", "wants_sleep"}


# ---------- in the engine ----------
def make(cfg, gains=None, project=None, action="draw the old tree"):
    cfg.world["events"] = []
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": action, "intent": "",
                                        "feeling": "neutral", "wants_sleep": False}
    out = {"outcome": "It draws.", "gains": gains or {}, "effort": "low", "duration": 6, "changes": []}
    b.handlers["narrator"] = lambda m, s: {**out, **({"project": project} if project else {})}
    e = Engine(cfg, b, rng=random.Random(1))
    e.world.events = None
    return e, b


def add_project(e, **kw):
    vals = dict(title="drawing the old tree", activity="drawing", why="likes it", status="active", progress=0.0,
                note="", formed_day="2026-11-18")
    vals.update(kw)
    e.conn.execute("INSERT INTO projects(title,activity,why,status,progress,note,formed_day,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,'t')", (vals["title"], vals["activity"], vals["why"], vals["status"],
                                                 vals["progress"], vals["note"], vals["formed_day"]))


def test_a_report_advances_the_project_and_keeps_a_note_for_next_time(cfg):
    e, b = make(cfg, project={"id": 1, "advance": "++", "note": "the trunk is done"})
    add_project(e)
    e.tick()
    p = PJ.get(e.conn, 1)
    assert p["progress"] == cfg.settings["projects"]["progress_values"]["++"] and p["note"] == "the trunk is done"
    assert p["last_worked"] and p["ticks_worked"] == 1 and p["worked_today"] == 1
    seen = []
    b.handlers["brain"] = lambda m, s: (seen.append(m[-1]["content"]) or
                                        {"reaction": "", "thought": "", "action": "keep drawing", "intent": "",
                                         "feeling": "neutral", "wants_sleep": False})
    e.tick()
    assert "Where you got to: the trunk is done" in seen[0] and "just begun" in seen[0]


def test_the_report_is_traced_with_the_exemption(cfg):
    e, b = make(cfg, project={"id": 1, "advance": "+"})
    add_project(e)
    info = e.tick()
    t = json.loads(e.conn.execute("SELECT project FROM tick_traces WHERE tick_id=?", (info["id"],)).fetchone()[0])
    assert t["ok"] and t["repetition_exempt"] is True and t["progress_after"] == 8


def test_rejected_reports_change_nothing_and_are_traced(cfg):
    e, b = make(cfg, project={"id": 99, "advance": "++"})
    add_project(e)
    info = e.tick()
    assert PJ.get(e.conn, 1)["progress"] == 0
    t = json.loads(e.conn.execute("SELECT project FROM tick_traces WHERE tick_id=?", (info["id"],)).fetchone()[0])
    assert t["ok"] is False and "no active project" in t["reason"]


def test_a_paused_project_cannot_be_advanced(cfg):
    e, b = make(cfg, project={"id": 1, "advance": "++"})
    add_project(e, status="paused")
    e.tick()
    assert PJ.get(e.conn, 1)["progress"] == 0


def test_project_work_is_exempt_from_the_repetition_penalty(cfg, tmp_path):
    def wonder_gains(project):
        cfg.settings["db_path"] = str(tmp_path / f"{bool(project)}.db")
        e, b = make(cfg, gains={"wonder": "++"}, project=project)
        add_project(e)
        e.state["needs"]["wonder"] = 5.0
        e.tick()
        first = e.state["needs"]["wonder"]
        e.tick()
        return first, e.state["needs"]["wonder"] - first
    first_plain, second_plain = wonder_gains(None)
    first_proj, second_proj = wonder_gains({"id": 1, "advance": "+"})
    assert second_plain < first_plain - 5 and second_plain == pytest.approx(11.6, abs=0.6)   # x0.4 on the repeat
    assert second_proj == pytest.approx(29.5, abs=0.6)                                       # exempt: full gain again


def test_a_report_with_no_progress_does_not_exempt_the_action(cfg, tmp_path):
    e, b = make(cfg, gains={"wonder": "++"}, project={"id": 1, "advance": "0"})
    add_project(e)
    e.state["needs"]["wonder"] = 5.0
    e.tick()
    first = e.state["needs"]["wonder"]
    e.tick()
    assert e.state["needs"]["wonder"] - first < 15


def test_an_artifact_is_saved_and_linked_to_the_project_and_tick(cfg):
    art = {"kind": "sketch", "title": "Roots", "text": "Deep roots under the old tree."}
    e, b = make(cfg, project={"id": 1, "advance": "+", "artifact": art})
    add_project(e)
    info = e.tick()
    a = e.conn.execute("SELECT * FROM artifacts").fetchone()
    assert (a["project_id"], a["tick_id"], a["kind"], a["title"], a["text"]) == \
        (1, info["id"], "sketch", "Roots", "Deep roots under the old tree.")
    assert a["created_sim_time"]


def test_finishing_a_project_completes_it_pays_the_gains_and_makes_a_memory(cfg):
    e, b = make(cfg, project={"id": 1, "advance": "++", "note": "all done"})
    add_project(e, progress=99.0)
    e.state["needs"]["joy"] = 30.0
    before = e.state["needs"]["joy"]
    e.tick()
    p = PJ.get(e.conn, 1)
    assert p["status"] == "done" and p["progress"] == 100
    gain = cfg.settings["projects"]["completion_gains"]["joy"]
    decay = cfg.character["needs"]["joy"]["decay_per_hour"] * 6 / 60
    assert e.state["needs"]["joy"] == pytest.approx(before - decay + gain)
    assert len(e.memory.all_short()) == 1                              # a finished project is a notable moment
    assert PJ.active(e.conn) == []                                   # the brain stops being told about it


def test_a_milestone_makes_a_short_term_memory(cfg):
    e, b = make(cfg, project={"id": 1, "advance": "++"})
    add_project(e, progress=20.0)
    e.tick()
    assert len(e.memory.all_short()) == 1


def test_working_on_a_project_does_not_make_a_place_stale(cfg):
    cfg.settings["stale_after_ticks"] = 2
    e, b = make(cfg, project={"id": 1, "advance": "+"})
    add_project(e)
    for _ in range(5):
        e.tick()
    assert e._ticks_in_area() == 0 and not e._stale_here()
    e2, b2 = make(cfg)                                                # no project: the same place goes stale
    for _ in range(3):
        e2.tick()
    assert e2._stale_here()


def test_the_narrator_cannot_set_progress_directly(cfg):
    e, b = make(cfg, project={"id": 1, "advance": "++", "progress": 100, "status": "done"})
    add_project(e)
    e.tick()
    p = PJ.get(e.conn, 1)
    assert p["status"] == "active" and p["progress"] == cfg.settings["projects"]["progress_values"]["++"]

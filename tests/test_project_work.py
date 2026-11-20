"""Project work in the tick: tapering progress, exemption from repetition, artifacts, completion, and what
the brain and the journal are told."""
import json
import random
import re

import pytest

from cat import brain, projects as PJ
from cat.engine import Engine
from cat.llm import MockBackend
from cat.world import WorldState
from tests.helpers import pick


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


def test_the_brain_output_schema_has_no_project_field():
    assert set(brain.BRAIN_SCHEMA["properties"]) == {"reaction", "thought", "move", "why", "feeling"}


# ---------- in the engine: work_on is a move, and code decides the progress ----------
WORK = "work on drawing the old tree"


def make(cfg, label=WORK, why="the trunk is done"):
    cfg.world["events"] = []
    cfg.settings["director"]["enabled"] = False
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.handlers["brain"] = lambda m, s: pick(m, label, why=why)
    b.handlers["journal"] = lambda m, s: {"entry": "I drew."}
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


def test_working_on_a_project_advances_it_by_the_configured_step_and_keeps_the_why_as_the_note(cfg):
    e, b = make(cfg)
    add_project(e)
    e.tick()
    p = PJ.get(e.conn, 1)
    step = cfg.settings["projects"]["progress_values"][cfg.settings["projects"]["work_bucket"]]
    assert p["progress"] == step and p["note"] == "the trunk is done"
    assert p["last_worked"] and p["ticks_worked"] == 1 and p["worked_today"] == 1
    seen = []
    b.handlers["brain"] = lambda m, s: (seen.append(m[-1]["content"]), pick(m, WORK, why="now the leaves"))[1]
    e.tick()
    assert "Where you got to: the trunk is done" in seen[0] and "just begun" in seen[0]
    assert "- [m" in seen[0] and "work on drawing the old tree" in seen[0]               # still on the menu


def test_the_step_tapers_as_the_project_nears_the_end(cfg):
    e, b = make(cfg)
    add_project(e, progress=50.0)
    e.tick()
    step = cfg.settings["projects"]["progress_values"][cfg.settings["projects"]["work_bucket"]]
    assert PJ.get(e.conn, 1)["progress"] == pytest.approx(50 + step * (1 - cfg.settings["projects"]["taper"] * 0.5))


def test_the_report_is_traced_with_the_exemption(cfg):
    e, b = make(cfg)
    add_project(e)
    info = e.tick()
    t = json.loads(e.conn.execute("SELECT project FROM tick_traces WHERE tick_id=?", (info["id"],)).fetchone()[0])
    assert t["ok"] and t["repetition_exempt"] is True and t["progress_after"] == 8


def test_a_paused_or_finished_project_is_not_on_the_menu(cfg):
    e, b = make(cfg, label="sit quietly")
    add_project(e, status="paused")
    add_project(e, title="a finished thing", status="done", progress=100.0)
    assert not any(m.verb == "work_on" for m in e._menu())
    e.conn.execute("UPDATE projects SET status='active' WHERE id=1")
    assert [m.label for m in e._menu() if m.verb == "work_on"] == ["work on drawing the old tree"]


def test_project_work_is_exempt_from_the_repetition_penalty(cfg, tmp_path):
    def wonder_gains(label):
        cfg.settings["db_path"] = str(tmp_path / f"{abs(hash(label))}.db")
        e, b = make(cfg, label=label)
        add_project(e)
        cfg.moves["verbs"]["look_out"]["effects"] = {"wonder": "++"}
        cfg.moves["verbs"]["work_on"]["effects"] = {"wonder": "++"}
        cfg.moves["verbs"]["look_out"]["minutes"] = cfg.moves["verbs"]["work_on"]["minutes"] = [20, 20]   # full gains
        e.state["needs"]["wonder"] = 5.0
        e.tick()
        first = e.state["needs"]["wonder"]
        e.tick()
        return first, e.state["needs"]["wonder"] - first
    f_plain, s_plain = wonder_gains("look out and watch the world for a while")
    f_proj, s_proj = wonder_gains(WORK)
    assert s_plain < (f_plain - 5) * 0.6                   # the repeat pays x0.4 (about 30 -> 12)
    assert s_proj > (f_proj - 5) * 0.8                     # project work: about the full gain again


def test_finishing_a_project_completes_it_pays_the_gains_and_makes_a_memory(cfg):
    e, b = make(cfg)
    add_project(e, progress=99.0)
    e.state["needs"]["joy"] = 30.0
    before = e.state["needs"]["joy"]
    e.tick()
    p = PJ.get(e.conn, 1)
    minutes = e.conn.execute("SELECT duration FROM ticks").fetchone()[0]
    assert p["status"] == "done" and p["progress"] == 100
    gain = cfg.settings["projects"]["completion_gains"]["joy"]
    decay = cfg.character["needs"]["joy"]["decay_per_hour"] * minutes / 60
    assert e.state["needs"]["joy"] == pytest.approx(before - decay + gain)
    assert len(e.memory.all_short()) == 1                              # a finished project is a notable moment
    assert PJ.active(e.conn) == []                                     # and it leaves the menu


def test_a_milestone_makes_a_short_term_memory(cfg):
    e, b = make(cfg)
    add_project(e, progress=20.0)
    e.tick()
    assert len(e.memory.all_short()) == 1


def test_working_on_a_project_does_not_make_a_place_stale(cfg):
    cfg.settings["stale_after_ticks"] = 2
    e, b = make(cfg)
    add_project(e)
    for _ in range(5):
        e.tick()
    assert e._ticks_in_area() == 0 and not e._stale_here()
    cfg.settings["db_path"] = cfg.settings["db_path"] + "2"
    e2, b2 = make(cfg, label="sit quietly and do nothing for a while")      # no project: the same place goes stale
    for _ in range(3):
        e2.tick()
    assert e2._stale_here()


# ---------- the piece it makes, in its own voice ----------
def creating(cfg, text="Deep roots under the old tree.", **kw):
    e, b = make(cfg, **kw)
    calls = []
    b.handlers["create"] = lambda m, s: (calls.append(m), {"kind": "sketch", "title": "Roots", "text": text})[1]
    return e, b, calls


def test_a_milestone_asks_the_entity_itself_to_make_something(cfg):
    e, b, calls = creating(cfg)
    add_project(e, progress=20.0, note="the trunk is done")
    info = e.tick()
    assert len(calls) == 1
    system, user = calls[0][0]["content"], calls[0][1]["content"]
    assert system == brain.system_prompt(cfg) and cfg.character["name"] in system        # its voice and identity, not the narrator's
    assert "drawing the old tree" in user and "likes it" in user and "You have just reached a milestone in it." in user
    a = e.conn.execute("SELECT * FROM artifacts").fetchone()
    assert (a["project_id"], a["tick_id"], a["kind"], a["title"], a["text"]) == (1, info["id"], "sketch", "Roots", "Deep roots under the old tree.")
    assert e.conn.execute("SELECT kind FROM llm_calls WHERE kind='create'").fetchone()


def test_finishing_asks_for_a_final_piece_and_says_so(cfg):
    e, b, calls = creating(cfg)
    add_project(e, progress=99.0)
    e.tick()
    assert "You have just finished it." in calls[0][1]["content"] and "It is finished." in calls[0][1]["content"]
    assert e.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


def test_an_ordinary_step_makes_no_piece_and_no_call(cfg):
    e, b, calls = creating(cfg)
    add_project(e, progress=0.0)                          # 0 -> 8: no milestone
    e.tick()
    assert calls == [] and e.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_a_failed_creation_is_just_no_piece_and_the_progress_still_counts(cfg):
    from cat.llm import BackendError
    e, b, calls = creating(cfg)
    def down(m, s): raise BackendError("down")
    b.handlers["create"] = down
    add_project(e, progress=20.0)
    info = e.tick()
    assert info["kind"] == "tick" and PJ.get(e.conn, 1)["progress"] > 25
    assert e.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_artifacts_can_be_turned_off(cfg):
    cfg.settings["projects"]["artifacts_enabled"] = False
    e, b, calls = creating(cfg)
    add_project(e, progress=20.0)
    e.tick()
    assert calls == [] and e.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_the_piece_is_cleaned_and_a_blank_one_is_dropped(cfg):
    e, b, calls = creating(cfg, text="  words   " + "x" * 5000)
    add_project(e, progress=20.0)
    e.tick()
    t = e.conn.execute("SELECT text FROM artifacts").fetchone()[0]
    assert t.startswith("words x") and len(t) == cfg.settings["projects"]["artifact_max_length"]
    cfg.settings["db_path"] = cfg.settings["db_path"] + "2"
    e2, b2, calls2 = creating(cfg, text="   ")
    add_project(e2, progress=20.0)
    e2.tick()
    assert e2.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_the_creation_prompt_is_editable_yaml(cfg):
    cfg.prompts["brain"]["milestone_reached"] = "A big step!"
    e, b, calls = creating(cfg)
    add_project(e, progress=20.0)
    e.tick()
    assert "A big step!" in calls[0][1]["content"]


def test_the_journal_never_writes_the_piece(cfg):
    e, b, calls = creating(cfg)
    b.handlers["journal"] = lambda m, s: {"entry": "x", "artifact": {"kind": "k", "title": "t", "text": "journal wrote this"}}
    add_project(e, progress=0.0)
    e.tick()
    assert e.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0

"""Activities: a move that takes time runs without LLM calls until it ends or something breaks in."""
import json
import random

import pytest
from fastapi.testclient import TestClient

from cat import clock, db
from cat.engine import Engine
from cat.llm import BackendError, MockBackend
from tests.helpers import pick

REST = "rest on the sofa"


def make(cfg, label=REST, minutes=60, step=20, events=None, seed=1):
    cfg.settings["activity"].update(enabled=True, step_minutes=step)
    cfg.settings["director"]["enabled"] = False
    cfg.world["events"] = events or []
    cfg.moves["verbs"]["rest_on"]["minutes"] = [minutes, minutes]
    cfg.moves["verbs"]["rest_on"]["effects"] = {"peace": "++"}
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    seen = []
    b.handlers["brain"] = lambda m, s: (seen.append(m[-1]["content"]), pick(m, label, why="it is cosy"))[1]
    b.handlers["narrator"] = lambda m, s: {"outcome": "She settles into the cushions."}
    e = Engine(cfg, b, rng=random.Random(seed))
    e.world.events = None
    return e, b, seen


def kinds(e):
    return [r[0] for r in e.conn.execute("SELECT kind FROM ticks ORDER BY id")]


def brain_calls(b):
    return sum(1 for t, _ in b.calls if t == "brain")


# ---------- the shape of a longer move ----------
def test_a_long_move_is_one_decision_then_carry_on_ticks_with_no_llm_calls(cfg):
    e, b, seen = make(cfg)
    for _ in range(4):
        e.tick()
    assert kinds(e) == ["tick", "carry", "carry", "tick"]
    assert brain_calls(b) == 2 and len(seen) == 2                          # decisions only
    assert sum(1 for t, _ in b.calls if t == "narrator") == 2
    assert [r[0] for r in e.conn.execute("SELECT duration FROM ticks ORDER BY id")] == [20, 20, 20, 20]
    first = e.conn.execute("SELECT * FROM ticks WHERE id=1").fetchone()
    assert first["activity_minutes"] == 60 and first["move_key"] == "rest_on:sofa" and first["action"] == REST


def test_the_clock_covers_the_whole_move(cfg):
    e, b, _ = make(cfg)
    start = e.state["sim_time"]
    for _ in range(3):
        e.tick()
    assert e.state["sim_time"] == clock.advance(start, 60)
    assert e.state["activity"] is None                                      # it has ended


def test_gains_and_effort_are_paid_once_at_the_start_and_carry_ticks_only_decay(cfg):
    e, b, _ = make(cfg)
    e.state["needs"]["peace"] = 20.0
    e.tick()
    after_decision = dict(e.state["needs"])
    energy = e.state["energy"]
    assert after_decision["peace"] > 40                                      # the ++ arrived with the decision
    e.tick()
    for k, spec in cfg.character["needs"].items():                           # then it is just time passing
        assert e.state["needs"][k] == pytest.approx(after_decision[k] - spec["decay_per_hour"] * 20 / 60)
    assert e.state["energy"] == energy                                       # no effort, no sleep: unchanged


def test_carry_ticks_write_need_math_that_adds_up(cfg):
    e, b, _ = make(cfg)
    e.tick()
    e.tick()
    rows = e.conn.execute("SELECT * FROM need_math WHERE tick_id=2").fetchall()
    assert len(rows) == len(cfg.character["needs"])
    for r in rows:
        assert r["gain_raw"] == 0 and r["after"] == pytest.approx(r["before"] - r["decay"] - (r["event_drain"] or 0)
                                                                   - (r["dislike_drain"] or 0) + r["clamp_adjust"], abs=2e-3)


def test_a_move_no_longer_than_a_step_is_one_tick(cfg):
    e, b, _ = make(cfg, minutes=20)
    e.tick()
    e.tick()
    assert kinds(e) == ["tick", "tick"] and e.state["activity"] is None


def test_turned_off_a_move_takes_its_whole_length_in_one_tick(cfg):
    e, b, _ = make(cfg)
    cfg.settings["activity"]["enabled"] = False
    start = e.state["sim_time"]
    e.tick()
    assert kinds(e) == ["tick"] and e.state["sim_time"] == clock.advance(start, 60) and e.state["activity"] is None


def test_going_to_sleep_is_never_an_activity(cfg):
    e, b, _ = make(cfg, label="go to sleep")
    cfg.moves["verbs"]["sleep"]["minutes"] = [5, 5]
    e.state["energy"] = -30.0
    e.tick()
    assert e.state["activity"] is None and e.state["is_asleep"]


def test_the_trace_follows_the_activity(cfg):
    e, b, _ = make(cfg)
    for _ in range(3):
        e.tick()
    states = [json.loads(r[0])["state"] for r in e.conn.execute("SELECT activity FROM tick_traces ORDER BY tick_id")]
    assert states == ["started", "carried", "finished"]


# ---------- interrupts ----------
def test_an_event_interrupts_it_and_the_brain_is_told_what_was_broken_off(cfg):
    e, b, seen = make(cfg, events=[{"text": "A loud noise.", "chance": 0.0}])
    e.tick()                                                                   # decision; activity running
    cfg.world["events"] = [{"text": "A loud noise.", "chance": 1.0}]
    e.tick()                                                                   # the event breaks in: a new decision
    assert kinds(e) == ["tick", "tick"] and len(seen) == 2
    assert "You were partway through: rest on the sofa, when something happened." in seen[1]
    assert "The world: A loud noise." in seen[1]
    state = json.loads(e.conn.execute("SELECT activity FROM tick_traces WHERE tick_id=2").fetchone()[0])
    assert state["state"] == "started"                                          # the new move began on that tick


def test_events_can_be_set_not_to_interrupt(cfg):
    cfg.settings["activity"]["interrupt_on_events"] = False
    e, b, seen = make(cfg)
    e.tick()
    cfg.world["events"] = [{"text": "A loud noise.", "chance": 1.0}]
    e.tick()
    assert kinds(e) == ["tick", "carry"] and e.state["activity"] is not None
    assert e.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1       # it still happened


def test_a_need_newly_dropping_to_empty_interrupts(cfg):
    e, b, seen = make(cfg)
    e.state["needs"]["curiosity"] = 17.0                       # low; decay will take it below 15 while it rests
    e.tick()
    e.state["needs"]["curiosity"] = 14.0                       # now empty, and it was not when the move began
    e.tick()
    assert kinds(e) == ["tick", "tick"]
    assert "when you realised something was badly lacking." in seen[1]


def test_a_need_that_was_already_empty_does_not_keep_interrupting(cfg):
    e, b, seen = make(cfg)
    e.state["needs"]["curiosity"] = 5.0
    e.tick()
    e.tick()
    assert kinds(e) == ["tick", "carry"]


def test_empty_need_interrupts_can_be_turned_off(cfg):
    cfg.settings["activity"]["interrupt_on_empty_need"] = False
    e, b, seen = make(cfg)
    e.state["needs"]["curiosity"] = 17.0
    e.tick()
    e.state["needs"]["curiosity"] = 14.0
    e.tick()
    assert kinds(e) == ["tick", "carry"]


def test_the_director_does_not_run_during_carry_on_ticks(cfg):
    e, b, _ = make(cfg)
    cfg.settings["director"].update(enabled=True, every_ticks=1, stale_every_ticks=1)
    calls = []
    b.handlers["director"] = lambda m, s: (calls.append(1), {"happening": {}})[1]
    for _ in range(3):
        e.tick()
    assert kinds(e)[:3] == ["tick", "carry", "carry"] and len(calls) <= 1          # at most the decision tick's own run


def test_sleeping_ends_the_activity(cfg):
    e, b, _ = make(cfg)
    e.tick()
    assert e.state["activity"] is not None
    e.state["sim_time"] = "2026-11-18T23:30"
    e.state["energy"] = -45.0
    info = e.tick()
    assert info["kind"] == "asleep" and e.state["activity"] is None


# ---------- survives, and what it does not touch ----------
def test_an_activity_survives_a_restart(cfg):
    e, b, seen = make(cfg)
    e.tick()
    e2 = Engine(cfg, b, rng=random.Random(2))
    assert e2.state["activity"]["label"] == REST and e2.state["activity"]["remaining"] == 40
    before = brain_calls(b)
    info = e2.tick()
    assert info["kind"] == "carry" and brain_calls(b) == before


def test_while_it_runs_a_dead_brain_changes_nothing_and_when_it_ends_the_tick_dozes(cfg):
    e, b, _ = make(cfg)
    e.tick()
    b.online = False
    assert e.tick()["kind"] == "carry" and e.tick()["kind"] == "carry"         # no brain needed to carry on
    assert e.tick()["kind"] == "doze"                                           # now it must decide, and cannot


def test_carry_ticks_do_not_count_as_places_or_decisions(cfg):
    cfg.settings["stale_after_ticks"] = 2
    cfg.settings["director"].update(every_ticks=50)
    e, b, _ = make(cfg)
    for _ in range(3):
        e.tick()
    assert e._ticks_in_area() == 1                                              # one decision in this place
    assert int(db.get_meta(e.conn, "director_gap", "0")) == 1                   # and one toward the director
    cfg.settings["db_path"] = cfg.settings["db_path"] + "2"
    e2, b2, _ = make(cfg)
    for _ in range(3):
        e2.tick()                                                               # same move again (decision, carry, carry)
    assert e2._repetition_multiplier("rest_on:sofa", "living_room") == cfg.settings["repetition_penalty"]["multiplier"]


# ---------- the diary and pacing ----------
def test_the_diary_has_one_entry_for_the_whole_move_with_its_length(cfg, monkeypatch):
    from cat.web.app import create_app
    e, b, _ = make(cfg)
    for _ in range(3):
        e.tick()
    entries = TestClient(create_app(cfg)).get("/api/state").json()["diary"]
    assert len(entries) == 1 and entries[0]["span"] == "about 60 min" and entries[0]["action"] == REST
    html = TestClient(create_app(cfg)).get("/").text
    assert "about 60 min" in html


def test_a_short_move_has_no_span(cfg):
    from cat.web.app import create_app
    e, b, _ = make(cfg, minutes=20)
    e.tick()
    assert TestClient(create_app(cfg)).get("/api/state").json()["diary"][0]["span"] == ""


def test_carry_ticks_wait_less_than_decisions(cfg):
    cfg.settings["tick_interval_seconds"] = 90
    cfg.settings["activity"]["carry_interval_seconds"] = 10
    cfg.settings["dev_commands_enabled"] = False
    e, b, _ = make(cfg)
    waits = []
    e.run(ticks=4, interval=None, sleep=waits.append)
    assert [k for k in kinds(e)] == ["tick", "carry", "carry", "tick"]
    assert waits[:3] == [10, 10, 90]                                            # after tick, carry, carry


def test_the_dashboard_renders_a_carry_tick(cfg, monkeypatch):
    from cat.devdash.app import create_app
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)
    e, b, _ = make(cfg)
    e.tick()
    e.tick()
    c = TestClient(create_app(cfg))
    assert c.get("/ticks/2").status_code == 200 and c.get("/ticks").status_code == 200
    assert c.get("/api/dev/ticks/2").status_code == 200


# ---------- config ----------
def test_activity_settings_are_validated(tmp_path):
    from tests.test_config import _problems
    assert "activity.step_minutes: must be an integer >= 1" in _problems(
        tmp_path, "settings.yaml", lambda s: s["activity"].update(step_minutes=0))
    assert "activity.enabled: must be true or false" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["activity"].update(enabled="yes"))
    assert "activity.carry_interval_seconds: must be a number >= 0" in _problems(
        tmp_path / "c", "settings.yaml", lambda s: s["activity"].update(carry_interval_seconds=-1))


def test_an_older_database_without_the_column_still_loads(cfg):
    e, b, _ = make(cfg)
    e.tick()
    st = db.get_state(e.conn)
    assert "activity" in st and st["activity"]["remaining"] == 40


# ---------- reasons and telemetry ----------
def test_the_diary_shows_why_and_how_long_it_was_carried_on(cfg):
    from cat.web.app import create_app
    e, b, _ = make(cfg)
    for _ in range(3):
        e.tick()
    en = TestClient(create_app(cfg)).get("/api/state").json()["diary"][0]
    assert en["why_text"] == "because: it is cosy"
    assert en["carried"] == 2 and "2 more steps" in en["carried_text"]
    html = TestClient(create_app(cfg)).get("/").text
    assert "because: it is cosy" in html and "no new decision" in html


def test_the_dashboard_counts_decisions_against_carried_on_ticks(cfg):
    from cat.devdash import data
    e, b, _ = make(cfg)
    for _ in range(3):
        e.tick()
    s = data.stats(db.connect(cfg.db_path, readonly=True), cfg, hours=24)
    assert s["decisions"] == 1 and s["carried"] == 2
    assert abs(s["carried_share"] - 2 / 3) < 1e-9
    assert s["avg_tokens_per_call"] is not None and s["per_day"] and s["per_day"][0]["calls"] >= 1

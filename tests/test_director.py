"""The world director: when it runs, what it may do, what it never sees, and that failure costs nothing."""
import json
import random

import pytest

from cat import db, director as DR
from cat.engine import Engine
from cat.llm import BackendError, MockBackend


# ---------- pure: cadence ----------
def test_due_on_the_interval_and_sooner_when_stale(cfg):
    d = cfg.settings["director"]
    assert DR.due(cfg, d["every_ticks"] - 1, False) == (False, "not due")
    assert DR.due(cfg, d["every_ticks"], False) == (True, "interval")
    assert DR.due(cfg, d["stale_every_ticks"], True) == (True, "stale")
    assert DR.due(cfg, d["stale_every_ticks"] - 1, True) == (False, "not due")
    cfg.settings["director"]["enabled"] = False
    assert DR.due(cfg, 99, True) == (False, "disabled")


# ---------- pure: validating a proposal ----------
def val(cfg, raw, recent=(), here=0):
    return DR.validate(cfg, raw, list(recent), here)


def test_nothing_happens_is_the_normal_answer(cfg):
    for raw in (None, {}, "x", {"text": "  "}, {"mood": 1}, []):
        assert val(cfg, raw)["action"] == "none"


def test_a_clean_happening_passes_through(cfg):
    out = val(cfg, {"text": "  A bell   rings. ", "mood": 0.2, "weather": "rain",
                    "spawn": {"name": "a pebble", "tags": ["movable", "glittery"]}, "drains": {"peace": 3}})
    assert out["action"] == "happened" and out["text"] == "A bell rings." and out["mood"] == 0.2
    assert out["weather"] == "rain" and out["spawn"] == {"name": "a pebble", "tags": ["movable"]}   # tag filtered
    assert out["drains"] == {"peace": 3.0} and out["notes"] == []


def test_repeats_are_rejected_case_insensitively(cfg):
    out = val(cfg, {"text": "A bell rings."}, recent=["a BELL rings."])
    assert out["action"] == "rejected" and "repeats" in out["reason"]


def test_text_is_trimmed_to_the_limit(cfg):
    n = cfg.settings["director"]["text_max_length"]
    assert len(val(cfg, {"text": "x" * (n + 50)})["text"]) == n


def test_mood_is_clamped_and_garbage_mood_is_zero(cfg):
    cap = cfg.settings["director"]["max_mood"]
    assert val(cfg, {"text": "a", "mood": 9})["mood"] == cap
    assert val(cfg, {"text": "a", "mood": -9})["mood"] == -cap
    for bad in ("x", True, float("nan"), None):
        assert val(cfg, {"text": "a", "mood": bad})["mood"] == 0.0


def test_unknown_weather_is_ignored_not_fatal(cfg):
    out = val(cfg, {"text": "a", "weather": "volcano"})
    assert out["action"] == "happened" and out["weather"] is None and any("volcano" in n for n in out["notes"])


def test_spawn_respects_the_area_cap(cfg):
    out = val(cfg, {"text": "a", "spawn": {"name": "x"}}, here=cfg.settings["max_objects_per_area"])
    assert out["spawn"] is None and any("full" in n for n in out["notes"])
    assert val(cfg, {"text": "a", "spawn": {"name": "  "}})["spawn"] is None


def test_drains_are_capped_and_validated(cfg):
    d = cfg.settings["director"]
    out = val(cfg, {"text": "a", "drains": {"peace": 999, "joy": 2, "wonder": 3, "nope": 5, "curiosity": -1}})
    assert out["drains"]["peace"] == d["max_drain_points"]
    assert len(out["drains"]) <= d["max_drain_needs"] and "nope" not in out["drains"] and "curiosity" not in out["drains"]
    assert any("unknown need 'nope'" in n for n in out["notes"]) and any("limited" in n for n in out["notes"])


# ---------- what the director is told ----------
def msgs(cfg, **kw):
    base = dict(area_id="living_room", sim_time="2026-11-18T19:40", weather="rain", objects=["the sofa"],
                recent_events=["A bird lands."], recent_actions=["nap"], stale=False)
    base.update(kw)
    return DR.messages(cfg, **base)


def test_the_director_never_sees_needs_or_mood(cfg):
    m = msgs(cfg)
    user = m[1]["content"]
    assert "the living room" in user and "7:40 pm" in user and "Weather: rain" in user
    assert "- A bird lands." in user and "- nap" in user and "the sofa" in user
    for word in ("mood", "feel", "energy", "sustenance level", "low", "empty"):
        assert word not in user.lower(), word
    assert "never see how the entity feels or what it needs" in m[0]["content"]
    assert "do not rescue it, reward it or punish it" in m[0]["content"]


def test_the_stale_note_appears_only_when_stale(cfg):
    assert "for a while" not in msgs(cfg)[1]["content"]
    assert "for a while" in msgs(cfg, stale=True)[1]["content"]


def test_the_director_prompt_is_editable_yaml(cfg):
    cfg.prompts["director"]["none_events"] = "(a blank slate)"
    assert "(a blank slate)" in msgs(cfg, recent_events=[])[1]["content"]


# ---------- in the engine ----------
def make(cfg, answer=None, seed=1):
    cfg.world["events"] = []                         # only the director can make happenings
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": "sit", "intent": "",
                                        "feeling": "neutral", "wants_sleep": False}
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok", "gains": {}, "effort": "low", "duration": 6,
                                           "changes": []}
    calls = []
    if answer is not None:
        b.handlers["director"] = lambda m, s: (calls.append(m) or {"happening": answer})
    e = Engine(cfg, b, rng=random.Random(seed))
    e.world.events = None
    return e, b, calls


def test_it_runs_on_the_interval_not_every_tick(cfg):
    cfg.settings["director"]["every_ticks"] = 4
    e, b, calls = make(cfg, {"text": "A bell rings."})
    cfg.settings["stale_after_ticks"] = 0                # stale off: interval only
    for _ in range(9):
        e.tick()
    assert len(calls) == 2                               # ticks 5 and 9
    assert e.conn.execute("SELECT COUNT(*) FROM events WHERE text='A bell rings.'").fetchone()[0] >= 1


def test_it_runs_sooner_when_the_place_goes_stale(cfg):
    cfg.settings["director"].update(every_ticks=10, stale_every_ticks=2)
    cfg.settings["stale_after_ticks"] = 2
    e, b, calls = make(cfg, {"text": "A bell rings."})
    for _ in range(6):
        e.tick()
    assert len(calls) >= 1 and "for a while" in calls[0][1]["content"]
    t = [json.loads(r[0]) for r in e.conn.execute("SELECT director FROM tick_traces WHERE director IS NOT NULL")]
    assert t[0]["trigger"] == "stale"


def test_disabled_means_only_hand_written_events(cfg):
    cfg.settings["director"]["enabled"] = False
    e, b, calls = make(cfg, {"text": "A bell rings."})
    for _ in range(12):
        e.tick()
    assert calls == [] and e.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_its_happening_is_perceived_nudges_mood_and_is_recorded(cfg):
    cfg.settings["director"]["every_ticks"] = 1
    e, b, calls = make(cfg, {"text": "A bell rings twice.", "mood": 0.4})
    seen = []
    b.handlers["brain"] = lambda m, s: (seen.append(m[-1]["content"]) or
                                        {"reaction": "", "thought": "", "action": "sit", "intent": "",
                                         "feeling": "neutral", "wants_sleep": False})
    e.tick()
    e.tick()
    assert any("The world: A bell rings twice." in s for s in seen)           # the brain perceives it
    assert e.conn.execute("SELECT area FROM events WHERE text='A bell rings twice.'").fetchone()
    assert e.state["mood"]["event_nudge"] != 0                                 # it nudged mood


def test_its_spawn_follows_the_same_rules_as_events(cfg):
    cfg.settings["director"]["every_ticks"] = 1
    e, b, calls = make(cfg, {"text": "A pebble rolls in.", "spawn": {"name": "a smooth pebble", "tags": ["movable", "x"]}})
    e.tick()
    e.tick()
    o = e.conn.execute("SELECT * FROM objects WHERE name='a smooth pebble'").fetchone()
    assert o and o["created_by"] == "director" and json.loads(o["tags"]) == ["movable"]


def test_its_weather_change_only_takes_known_names(cfg):
    cfg.settings["director"]["every_ticks"] = 1
    e, b, _ = make(cfg, {"text": "The sky opens.", "weather": "rain"})
    e.tick()
    e.tick()
    assert e.world.weather == "rain"
    e2, b2, _ = make(cfg, {"text": "The sky does something odd.", "weather": "volcano"}, seed=2)
    cfg.settings["db_path"] = cfg.settings["db_path"] + "2"
    e2.tick()
    assert e2.world.weather != "volcano"


def test_its_drains_apply_like_event_drains_and_are_traced(cfg):
    cfg.settings["director"]["every_ticks"] = 1
    e, b, _ = make(cfg, {"text": "A harsh clatter.", "drains": {"peace": 4}})
    e.tick()
    before = dict(e.state["needs"])
    e.tick()
    decay = cfg.character["needs"]["peace"]["decay_per_hour"] * 6 / 60
    assert e.state["needs"]["peace"] == pytest.approx(before["peace"] - decay - 4)
    r = e.conn.execute("SELECT event_drain FROM need_math WHERE need='peace' ORDER BY id DESC LIMIT 1").fetchone()
    assert r["event_drain"] == 4


def test_it_counts_toward_the_events_cap(cfg):
    cfg.settings["director"]["every_ticks"] = 1
    cfg.settings["events_max_per_tick"] = 1
    e, b, calls = make(cfg, {"text": "A bell rings."})
    cfg.world["events"] = [{"text": "Always.", "chance": 1.0}]
    e.tick()
    e.tick()
    assert calls == []                                   # the hand-written event used the one slot


def test_a_failed_call_costs_nothing_and_does_not_doze_the_tick(cfg):
    cfg.settings["director"]["every_ticks"] = 1
    e, b, _ = make(cfg, {"text": "x"})
    def boom(m, s): raise BackendError("down")
    b.handlers["director"] = boom
    e.tick()
    info = e.tick()
    assert info["kind"] == "tick"                        # the brain and narrator still ran
    t = [json.loads(r[0]) for r in e.conn.execute("SELECT director FROM tick_traces WHERE director IS NOT NULL")]
    assert t[0]["action"] == "failed" and "down" in t[0]["reason"]


def test_a_rejected_repeat_is_traced_and_changes_nothing(cfg):
    cfg.settings["director"]["every_ticks"] = 1
    e, b, _ = make(cfg, {"text": "A bell rings."})
    e.tick()
    e.tick()                                             # first run records the bell
    e.tick()                                             # second run proposes it again
    actions = [json.loads(r[0])["action"] for r in e.conn.execute(
        "SELECT director FROM tick_traces WHERE director IS NOT NULL ORDER BY tick_id")]
    assert actions[0] == "happened" and "rejected" in actions
    assert e.conn.execute("SELECT COUNT(*) FROM events WHERE text='A bell rings.'").fetchone()[0] == 1


def test_it_does_not_run_while_asleep_or_with_the_brain_offline(cfg):
    cfg.settings["director"]["every_ticks"] = 1
    e, b, calls = make(cfg, {"text": "A bell rings."})
    e.state["is_asleep"] = True
    e.tick()
    assert calls == []
    e2, b2, calls2 = make(cfg, {"text": "A bell rings."}, seed=3)
    cfg.settings["db_path"] = cfg.settings["db_path"] + "3"
    b2.online = False
    e2.tick()
    assert calls2 == []


def test_the_director_call_is_recorded_as_its_own_kind(cfg):
    cfg.settings["director"]["every_ticks"] = 1
    e, b, _ = make(cfg, {"text": "A bell rings."})
    e.tick()
    e.tick()
    kinds = {r[0] for r in e.conn.execute("SELECT kind FROM llm_calls")}
    assert "director" in kinds


def test_the_director_settings_are_validated(tmp_path):
    from tests.test_config import _problems
    assert "director.every_ticks: must be an integer >= 1" in _problems(
        tmp_path, "settings.yaml", lambda s: s["director"].update(every_ticks=0))
    assert "stale_every_ticks: must not exceed every_ticks" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["director"].update(every_ticks=3, stale_every_ticks=5))
    assert "director.max_mood: must be a number 0..1" in _problems(
        tmp_path / "c", "settings.yaml", lambda s: s["director"].update(max_mood=3))
    assert "director.enabled: must be true or false" in _problems(
        tmp_path / "d", "settings.yaml", lambda s: s["director"].update(enabled="yes"))


# ---------- dev dashboard ----------
def test_tick_page_explains_the_director(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from cat.devdash import data
    from cat.devdash.app import create_app
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)
    cfg.settings["director"]["every_ticks"] = 1
    e, b, _ = make(cfg, {"text": "A bell rings.", "mood": 0.9, "weather": "volcano",
                         "spawn": {"name": "a pebble", "tags": ["movable"]}})
    e.tick()
    tid = e.tick()["id"]
    d = data.tick_detail(cfg, tid)["director"]
    assert d["action"] == "happened" and d["trigger"] == "interval" and d["mood"] == cfg.settings["director"]["max_mood"]
    html = TestClient(create_app(cfg)).get(f"/ticks/{tid}").text
    assert 'id="director"' in html and "A bell rings." in html and "never sees needs or mood" in html
    assert "ignored or clamped" in html and "volcano" in html


def test_ticks_without_a_director_run_have_no_section(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from cat.devdash.app import create_app
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)
    e, b, _ = make(cfg, {"text": "x"})
    tid = e.tick()["id"]
    assert 'id="director"' not in TestClient(create_app(cfg)).get(f"/ticks/{tid}").text

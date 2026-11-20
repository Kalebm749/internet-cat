import json
import random

import pytest

from cat import clock, db, sleep
from cat.engine import Engine
from cat.llm import BackendError, MockBackend
from tests.helpers import pick


def make(cfg, seed=1):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    return Engine(cfg, b, rng=random.Random(seed)), b


def quiet_brain(b, wants_sleep=False, action="sit"):
    b.handlers["brain"] = lambda m, s: pick(m, "go to sleep" if wants_sleep else None, feeling="neutral")
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok" if action == "sit" else action}     # shows in the day log


def set_time(e, t, energy=None, **needs):
    """Set the clock, optionally energy (-50..50) and need levels."""
    e.state["sim_time"] = t
    e.state["needs"].update({k: float(v) for k, v in needs.items()})
    if energy is not None:
        e.state["energy"] = float(energy)
    e.world.events = None


# ---------- pure: tendencies ----------
def test_tendency_clamped_to_max_step_and_range(cfg):
    step = cfg.settings["sleep"]["tendency_max_step"]
    cur = {"boldness": 0.5, "sociability": 0.99, "tidiness": 0.01}
    new, ch = sleep.clamp_tendencies(cfg, cur, {"boldness": 100, "sociability": 1, "tidiness": -1})
    assert new["boldness"] == pytest.approx(0.5 + step)
    assert new["sociability"] == 1.0
    assert "tidiness" not in new and ch["tidiness"]["dropped"] is True      # faded to nothing: forgotten
    assert ch["boldness"] == {"from": 0.5, "to": 0.55}


@pytest.mark.parametrize("bad", [None, [], "x", {"boldness": "up"}, {"boldness": float("nan")},
                                 {"boldness": True}])
def test_tendency_garbage_ignored(cfg, bad):
    cur = {"boldness": 0.5, "sociability": 0.4, "tidiness": 0.6}
    new, ch = sleep.clamp_tendencies(cfg, cur, bad)
    assert new == cur and ch == {}


def test_tendency_scaling_is_proportional(cfg):
    new, _ = sleep.clamp_tendencies(cfg, {"boldness": 0.5, "sociability": 0.5, "tidiness": 0.5},
                                    {"boldness": 0.5})
    assert new["boldness"] == pytest.approx(0.525)


# ---------- pure: preferences ----------
def test_new_preference_starts_weak_and_repeats_strengthen(cfg):
    first = sleep.merge_preferences(cfg, {}, [{"subject": "the old oak", "valence": 0.8, "reason": "view"}])
    assert first[0]["valence"] == pytest.approx(0.4) and first[0]["previous"] is None
    existing = {"the old oak": {"subject": "the old oak", "valence": 0.4, "reason": "view"}}
    again = sleep.merge_preferences(cfg, existing, [{"subject": "The Old Oak", "valence": 0.8, "reason": ""}])
    assert again[0]["valence"] == pytest.approx(0.6) and again[0]["subject"] == "the old oak"
    assert again[0]["reason"] == "view"


def test_preferences_limits_and_garbage(cfg):
    props = [{"subject": f"s{i}", "valence": 0.1 * i, "reason": ""} for i in range(1, 9)]
    props += [{"subject": "", "valence": 1}, {"subject": "x", "valence": "high"}, "junk",
              {"subject": "s8", "valence": -1}]
    out = sleep.merge_preferences(cfg, {}, props)
    assert len(out) == cfg.settings["sleep"]["max_preferences_per_night"]
    assert all(-1 <= o["valence"] <= 1 for o in out)
    assert sleep.merge_preferences(cfg, {}, None) == []


def test_parse_consolidation(cfg):
    s, m = sleep.parse_consolidation(cfg, {"summary": "ok", "memories": [
        {"text": "a", "importance": "high"}, {"text": "", "importance": "low"},
        {"text": "b", "importance": "weird"}, 7]})
    iv = cfg.settings["sleep"]["importance_values"]
    assert s == "ok" and m == [("a", iv["high"]), ("b", iv["medium"])]
    with pytest.raises(BackendError):
        sleep.parse_consolidation(cfg, {"summary": "", "memories": []})
    assert len(sleep.parse_consolidation(cfg, {"summary": "x", "memories": [
        {"text": str(i), "importance": "low"} for i in range(20)]})[1]) == cfg.settings["sleep"]["max_new_memories"]


# ---------- engine: falling asleep ----------
def test_exhausted_at_night_falls_asleep_without_brain(cfg):
    e, b = make(cfg)
    set_time(e, "2026-11-18T23:00", energy=-45)
    info = e.tick()
    assert info["kind"] == "asleep" and e.state["is_asleep"]
    assert not any(t == "brain" for t, _ in b.calls)
    assert db.get_meta(e.conn, "sleep_started") == "2026-11-18T23:00"


def test_exhausted_by_day_does_not_force_sleep(cfg):
    e, b = make(cfg)
    set_time(e, "2026-11-18T12:00", energy=-45)
    assert e.tick()["kind"] == "tick"


def test_brain_can_choose_sleep_when_tired_or_night(cfg):
    e, b = make(cfg)
    quiet_brain(b, wants_sleep=True)
    set_time(e, "2026-11-18T14:00", energy=-30)          # tired by day
    e.tick()
    assert e.state["is_asleep"]
    e2, b2 = make(cfg, seed=2)
    quiet_brain(b2, wants_sleep=True)
    set_time(e2, "2026-11-18T12:00", energy=50)       # not tired, daytime: ignored
    e2.tick()
    assert not e2.state["is_asleep"]
    e3, b3 = make(cfg, seed=3)
    quiet_brain(b3, wants_sleep=True)
    set_time(e3, "2026-11-18T22:00", energy=50)       # night: honoured
    e3.tick()
    assert e3.state["is_asleep"]


# ---------- engine: asleep + wake ----------
def run_until(e, pred, limit=40):
    for _ in range(limit):
        info = e.tick()
        if pred(info):
            return info
    raise AssertionError("condition not reached")


def test_sleep_restores_rest_and_wakes_in_the_morning(cfg):
    e, b = make(cfg)
    set_time(e, "2026-11-18T22:00", energy=-45)
    e.tick()
    assert e.state["is_asleep"]
    seen = []
    info = run_until(e, lambda i: (seen.append(i), i["outcome"].startswith("wakes"))[1])
    st = db.get_state(e.conn)
    assert not st["is_asleep"] and st["energy"] >= cfg.settings["sleep"]["wake_energy_at_least"]
    t = clock.parse(st["sim_time"])
    assert t.hour >= cfg.settings["sleep"]["wake_not_before_hour"]
    assert all(i["kind"] == "asleep" for i in seen)
    assert e.tick()["kind"] == "tick"                         # awake again, back to normal


def test_does_not_wake_before_morning_even_if_rested(cfg):
    e, b = make(cfg)
    set_time(e, "2026-11-18T21:30", energy=0)
    e.state["is_asleep"] = True
    db.set_meta(e.conn, "sleep_started", e.state["sim_time"])
    for _ in range(3):
        e.tick()
        assert e.state["energy"] <= 50
    assert clock.parse(e.state["sim_time"]).hour < 6 or clock.parse(e.state["sim_time"]).day == 19
    assert e.state["is_asleep"] or clock.parse(e.state["sim_time"]).hour >= 6


def test_max_sleep_hours_cap(cfg):
    cfg.settings["sleep"]["restore_per_sim_hour"] = 0          # never gets rested
    e, b = make(cfg)
    set_time(e, "2026-11-18T22:00", energy=-45)
    e.tick()
    info = run_until(e, lambda i: i["outcome"].startswith("wakes"), limit=20)
    assert not e.state["is_asleep"]


def test_sleep_decay_is_slower(cfg):
    e, b = make(cfg)
    set_time(e, "2026-11-18T22:00", energy=-45)
    f0 = e.state["needs"]["joy"]
    e.tick()
    drop = f0 - e.state["needs"]["joy"]
    full = cfg.character["needs"]["joy"]["decay_per_hour"] * cfg.settings["sleep"]["sim_minutes_per_tick"] / 60
    assert drop == pytest.approx(full * cfg.settings["sleep"]["decay_multiplier"])


# ---------- engine: the sleep cycle ----------
def seed_day(e, b):
    """A day with a few notable moments."""
    b.handlers["brain"] = lambda m, s: pick(m, feeling="positive")
    b.handlers["narrator"] = lambda m, s: {"outcome": "It was beautiful.", "gains": {"joy": "++"},
                                           "effort": "low", "duration": 20, "changes": []}
    for _ in range(4):
        e.tick()


def test_full_cycle_consolidates_drifts_and_logs(cfg):
    e, b = make(cfg)
    seed_day(e, b)
    assert len(e.memory.all_short()) == 4
    b.handlers.pop("brain"); b.handlers.pop("narrator")
    b.handlers["consolidate"] = lambda m, s: {"summary": "Sunsets all day.", "memories": [
        {"text": "I watched the sunset and it was beautiful.", "importance": "high"}]}
    b.handlers["tendencies"] = lambda m, s: {"changes": {"boldness": 1, "sociability": -100}}
    b.handlers["preferences"] = lambda m, s: {"preferences": [
        {"subject": "sunsets", "valence": 1.0, "reason": "so pretty"}]}
    before = dict(e.state["tendencies"])
    set_time(e, "2026-11-18T23:00", energy=-45)
    e.tick()                                                   # onset => cycle runs now
    st = db.get_state(e.conn)
    step = cfg.settings["sleep"]["tendency_max_step"]
    assert st["tendencies"]["boldness"] == pytest.approx(before["boldness"] + step)
    assert st["tendencies"]["sociability"] == pytest.approx(before["sociability"] - step)
    assert st["tendencies"]["tidiness"] == before["tidiness"]
    assert e.memory.all_short() == []
    longs = e.conn.execute("SELECT * FROM memories WHERE kind='long'").fetchall()
    assert [r["text"] for r in longs] == ["I watched the sunset and it was beautiful."]
    assert longs[0]["importance"] == cfg.settings["sleep"]["importance_values"]["high"]
    assert longs[0]["embedding"] is not None
    pref = e.conn.execute("SELECT * FROM preferences").fetchone()
    assert pref["subject"] == "sunsets" and pref["valence"] == pytest.approx(0.5)
    log = e.conn.execute("SELECT * FROM sleep_log").fetchone()
    assert log["summary"] == "Sunsets all day." and log["sim_date"] == "2026-11-18"
    assert json.loads(log["tendency_changes"])["boldness"]["to"] == pytest.approx(before["boldness"] + step)
    assert db.get_meta(e.conn, "sleep_pending") == "0"
    assert e.memory.recall(None, st["sim_time"])[0]["text"].startswith("I watched")


def test_core_traits_never_change(cfg):
    e, b = make(cfg)
    traits = list(cfg.character["core_traits"])
    b.handlers["tendencies"] = lambda m, s: {"changes": {"curious": 1, "core_traits": 1, "boldness": 1}}
    set_time(e, "2026-11-18T23:00", energy=-45)
    e.tick()
    assert set(e.state["tendencies"]) == set(cfg.character["tendencies"]) | {"curious"}   # one new a night
    assert cfg.character["core_traits"] == traits


def test_offline_sleep_postpones_and_recovers_without_losing_memories(cfg):
    e, b = make(cfg)
    seed_day(e, b)
    shorts = len(e.memory.all_short())
    b.handlers.pop("brain"); b.handlers.pop("narrator")
    set_time(e, "2026-11-18T22:00", energy=-45)
    b.online = False
    e.tick(); e.tick()
    assert e.state["is_asleep"] and db.get_meta(e.conn, "sleep_pending") == "1"
    assert len(e.memory.all_short()) == shorts                 # kept for later
    assert e.conn.execute("SELECT COUNT(*) FROM sleep_log").fetchone()[0] == 0
    b.online = True
    e.tick()                                                   # retry on the next sleeping tick
    assert db.get_meta(e.conn, "sleep_pending") == "0"
    assert e.conn.execute("SELECT COUNT(*) FROM sleep_log").fetchone()[0] == 1
    assert e.memory.all_short() == []


def test_failed_llm_call_mid_cycle_writes_nothing(cfg):
    e, b = make(cfg)
    seed_day(e, b)
    b.handlers.pop("brain"); b.handlers.pop("narrator")
    shorts = len(e.memory.all_short())
    tend = dict(e.state["tendencies"])

    def boom(m, s):
        raise BackendError("ollama fell over")
    b.handlers["preferences"] = boom                           # 3rd call fails
    set_time(e, "2026-11-18T23:00", energy=-45)
    e.tick()
    assert db.get_meta(e.conn, "sleep_pending") == "1"
    assert len(e.memory.all_short()) == shorts and e.state["tendencies"] == tend
    assert e.conn.execute("SELECT COUNT(*) FROM memories WHERE kind='long'").fetchone()[0] == 0
    del b.handlers["preferences"]
    e.tick()
    assert db.get_meta(e.conn, "sleep_pending") == "0"


def test_second_night_only_sees_the_new_day(cfg):
    e, b = make(cfg)
    seen = []
    b.handlers["consolidate"] = lambda m, s: (seen.append(m[-1]["content"]), {
        "summary": "x", "memories": []})[1]
    set_time(e, "2026-11-18T12:00", energy=50)
    quiet_brain(b, action="first-day-thing")
    e.tick()
    b.handlers["brain"] = lambda m, s: pick(m, "go to sleep", feeling="neutral")
    set_time(e, "2026-11-18T22:00", energy=50)
    e.tick()                                                   # wants_sleep at night -> cycle 1
    assert "first-day-thing" in seen[0]
    # wake, second day
    run_until(e, lambda i: i["outcome"].startswith("wakes"))
    quiet_brain(b, action="second-day-thing")
    e.tick()
    set_time(e, "2026-11-19T23:00", energy=-45)
    e.tick()
    assert "second-day-thing" in seen[1] and "first-day-thing" not in seen[1]


def test_preference_forgetting_and_cap(cfg, conn):
    from cat.memory import Memory
    cfg.settings["sleep"]["max_preferences_total"] = 2
    m = Memory(conn, cfg, MockBackend())
    for i, v in enumerate([0.9, 0.05, -0.7, 0.3]):
        conn.execute("INSERT INTO preferences(subject,valence,reason,updated_at) VALUES(?,?,?,?)",
                     (f"p{i}", v, "", "t"))
    plan = sleep.SleepPlan(summary="s")
    sleep.commit(conn, cfg, m, plan, "2026-11-18T23:00", "now")
    left = {r["subject"] for r in conn.execute("SELECT subject FROM preferences")}
    assert left == {"p0", "p2"}                                # weak dropped, then capped by |valence|


def test_1000_mock_ticks_with_sleep(cfg):
    e, b = make(cfg, seed=9)
    assert e.run(ticks=1000, interval=0) == 1000
    kinds = dict(e.conn.execute("SELECT kind, COUNT(*) FROM ticks GROUP BY kind").fetchall())
    assert kinds.get("asleep", 0) > 20 and kinds["tick"] > 100
    n_sleeps = e.conn.execute("SELECT COUNT(*) FROM sleep_log").fetchone()[0]
    assert n_sleeps >= 2
    st = db.get_state(e.conn)
    for t, v in st["tendencies"].items():
        assert 0 <= v <= 1
    # every night's recorded change respects the max step
    step = cfg.settings["sleep"]["tendency_max_step"]
    for r in e.conn.execute("SELECT tendency_changes FROM sleep_log"):
        for ch in json.loads(r[0]).values():
            assert abs(ch["to"] - ch["from"]) <= step + 1e-9
    assert all(-50 <= r[0] <= 50 for r in e.conn.execute("SELECT energy_after FROM ticks"))


# ---------- self-image ----------
def row(i, text, conf, day="2026-11-10"):
    return {"id": i, "text": text, "confidence": conf, "formed_day": day}


def test_self_image_none_or_garbage_changes_nothing(cfg):
    for raw in (None, [], {}, "x", [{"text": "  "}], ["str"]):
        assert sleep.decide_self_image(cfg, [], raw, "2026-11-18")["action"] in ("none", "rejected")


def test_self_image_adds_one_line_with_text_confidence_and_day(cfg):
    out = sleep.decide_self_image(cfg, [], [{"text": " I   seem small. ", "confidence": 0.6},
                                           {"text": "second is ignored", "confidence": 0.9}], "2026-11-18")
    assert out["action"] == "added" and out["text"] == "I seem small."
    assert out["confidence"] == 0.6 and out["formed_day"] == "2026-11-18" and out["dropped"] == []


def test_self_image_confidence_clamped_and_weak_or_bad_rejected(cfg):
    assert sleep.decide_self_image(cfg, [], [{"text": "a", "confidence": 7}], "d")["confidence"] == 1.0
    low = sleep.decide_self_image(cfg, [], [{"text": "a", "confidence": 0.01}], "d")
    assert low["action"] == "rejected" and "below" in low["reason"]
    assert sleep.decide_self_image(cfg, [], [{"text": "a", "confidence": "high"}], "d")["action"] == "rejected"
    assert sleep.decide_self_image(cfg, [], [{"text": "a", "confidence": True}], "d")["action"] == "rejected"


def test_self_image_duplicate_rejected(cfg):
    out = sleep.decide_self_image(cfg, [row(1, "I seem small.", 0.5)],
                                  [{"text": "i seem SMALL.", "confidence": 0.9}], "d")
    assert out["action"] == "rejected" and "already" in out["reason"]


def test_self_image_text_is_truncated(cfg):
    n = cfg.settings["sleep"]["max_self_image_length"]
    out = sleep.decide_self_image(cfg, [], [{"text": "x" * (n + 50), "confidence": 0.5}], "d")
    assert len(out["text"]) == n


def test_later_line_can_revise_or_contradict_an_earlier_one(cfg):
    ex = [row(1, "I am small.", 0.5), row(2, "I like noise.", 0.4)]
    out = sleep.decide_self_image(cfg, ex, [{"text": "I am not small after all.", "confidence": 0.7,
                                             "replaces": 1}], "2026-11-18")
    assert out["action"] == "revised" and out["replaced"]["text"] == "I am small."
    unknown = sleep.decide_self_image(cfg, ex, [{"text": "New.", "confidence": 0.7, "replaces": 99}], "d")
    assert unknown["action"] == "added" and unknown["replaced"] is None


def test_self_image_cap_forgets_the_least_confident(cfg):
    cap = cfg.settings["sleep"]["max_self_image_lines"]
    ex = [row(i, f"line {i}", 0.3 + i / 100) for i in range(1, cap + 1)]
    out = sleep.decide_self_image(cfg, ex, [{"text": "stronger", "confidence": 0.9}], "d")
    assert out["action"] == "added" and [d["id"] for d in out["dropped"]] == [1]
    weak = sleep.decide_self_image(cfg, ex, [{"text": "weaker", "confidence": 0.25}], "d")
    assert weak["action"] == "dropped_at_cap" and weak["dropped"] == []


def test_sleep_stores_the_line_and_the_brain_sees_it(cfg):
    e, b = make(cfg)
    seed_day(e, b)
    b.handlers.pop("brain"); b.handlers.pop("narrator")
    b.handlers["self_image"] = lambda m, s: {"self_image": [{"text": "I tire when I climb.",
                                                             "confidence": 0.6, "replaces": 0}]}
    set_time(e, "2026-11-18T23:00", energy=-45)
    e.tick()
    r = e.conn.execute("SELECT * FROM self_image").fetchall()
    assert [(x["text"], x["confidence"], x["formed_day"]) for x in r] == [("I tire when I climb.", 0.6, "2026-11-18")]
    assert e._self_image_lines() == ["I tire when I climb."]
    assert "I tire when I climb." in __import__("cat.brain", fromlist=["x"]).situation_prompt(
        cfg, e._brain_context([]))


def test_sleep_revises_a_line_and_respects_the_cap(cfg):
    e, b = make(cfg)
    seed_day(e, b)
    b.handlers.pop("brain"); b.handlers.pop("narrator")
    e.conn.execute("INSERT INTO self_image(text,confidence,formed_day,updated_at) VALUES('I am small.',0.5,'2026-11-10','x')")
    old_id = e.conn.execute("SELECT id FROM self_image").fetchone()["id"]
    b.handlers["self_image"] = lambda m, s: {"self_image": [{"text": "I may not be small.", "confidence": 0.7,
                                                             "replaces": old_id}]}
    set_time(e, "2026-11-18T23:00", energy=-45)
    e.tick()
    assert [x["text"] for x in e.conn.execute("SELECT text FROM self_image")] == ["I may not be small."]


def test_sleep_trace_logs_every_proposal_and_outcome(cfg):
    import json as _j
    e, b = make(cfg)
    seed_day(e, b)
    b.handlers.pop("brain"); b.handlers.pop("narrator")
    b.handlers["self_image"] = lambda m, s: {"self_image": [{"text": "a", "confidence": 0.01}]}   # too weak
    set_time(e, "2026-11-18T23:00", energy=-45)
    e.tick()
    t = e.conn.execute("SELECT * FROM sleep_traces WHERE step='self_image'").fetchone()
    assert _j.loads(t["proposed"])[0]["text"] == "a"
    assert _j.loads(t["clamped"])["action"] == "rejected"
    assert e.conn.execute("SELECT COUNT(*) FROM self_image").fetchone()[0] == 0


def test_self_image_failure_postpones_the_whole_night(cfg):
    e, b = make(cfg)
    seed_day(e, b)
    b.handlers.pop("brain"); b.handlers.pop("narrator")
    def boom(m, s): raise BackendError("down")
    b.handlers["self_image"] = boom
    shorts = len(e.memory.all_short())
    set_time(e, "2026-11-18T23:00", energy=-45)
    e.tick()
    assert len(e.memory.all_short()) == shorts and e.conn.execute("SELECT COUNT(*) FROM sleep_log").fetchone()[0] == 0


def test_sleep_prompt_asks_for_at_most_one_tentative_line_without_inventing_a_form(cfg):
    msgs = sleep.self_image_messages(cfg, "A day.", ["a memory"], ["a log"], [row(3, "I am small.", 0.5)])
    text = msgs[-1]["content"]
    assert "at most ONE" in text and "[3] I am small." in text and "Do not invent a form" in text
    assert "species" not in msgs[0]["content"]


def test_sleep_settings_for_self_image_are_required(tmp_path):
    from tests.test_config import _problems
    msg = _problems(tmp_path, "settings.yaml", lambda s: s["sleep"].pop("max_self_image_lines"))
    assert "sleep: missing required field 'max_self_image_lines'" in msg


# ---------- open-ended, learned tendencies ----------
def test_a_new_tendency_can_be_learned_and_starts_weak(cfg):
    new, ch = sleep.clamp_tendencies(cfg, {}, {"drawn to quiet corners": 1, "another one": 1})
    sl = cfg.settings["sleep"]
    assert list(new) == ["drawn to quiet corners"]            # at most max_new_tendencies_per_night a night
    assert abs(new["drawn to quiet corners"] - (sl["tendency_start"] + sl["tendency_max_step"])) < 1e-9
    assert ch["drawn to quiet corners"]["new"] is True


def test_a_negative_nudge_cannot_invent_a_tendency_and_names_are_cleaned(cfg):
    new, ch = sleep.clamp_tendencies(cfg, {}, {"avoids crowds": -1, "   ": 1, "x" * 200: 1, "ok": "high"})
    assert len(new) == 1 and len(next(iter(new))) == cfg.settings["sleep"]["tendency_name_max_length"]


def test_a_new_name_matches_an_existing_one_ignoring_case(cfg):
    new, ch = sleep.clamp_tendencies(cfg, {"Restless": 0.5}, {"restless": 1})
    assert new == {"Restless": 0.55} and "new" not in ch["Restless"]


def test_the_number_of_tendencies_is_capped_and_faded_ones_are_forgotten(cfg):
    cfg.settings["sleep"]["max_tendencies"] = 2
    new, _ = sleep.clamp_tendencies(cfg, {"a": 0.5, "b": 0.5}, {"c": 1})
    assert set(new) == {"a", "b"}
    cfg.settings["sleep"]["tendency_forget_below"] = 0.1
    new, ch = sleep.clamp_tendencies(cfg, {"a": 0.12}, {"a": -1})
    assert new == {} and ch["a"]["dropped"] is True


def test_the_default_character_predefines_no_tendencies_and_a_learned_one_reaches_the_prompt():
    from cat.config import load_config
    from cat import brain
    c = load_config(None)
    assert c.character["tendencies"] == {}
    assert brain.tendency_phrase(c, "drawn to quiet corners", 0.9) == "very drawn to quiet corners"
    assert brain.tendency_phrase(c, "drawn to quiet corners", 0.1) == "a little drawn to quiet corners"

import json
import random

import pytest

from cat import clock, db, sleep
from cat.engine import Engine
from cat.llm import BackendError, MockBackend


def make(cfg, seed=1):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    return Engine(cfg, b, rng=random.Random(seed)), b


def quiet_brain(b, wants_sleep=False, action="sit"):
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": action,
                                        "intent": "", "feeling": "neutral", "wants_sleep": wants_sleep}
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok", "gains": {}, "effort": "low",
                                           "duration": 10, "changes": []}


def set_time(e, t, **needs):
    e.state["sim_time"] = t
    e.state["needs"].update({k: float(v) for k, v in needs.items()})
    from cat import character
    e.state["energy"] = character.energy(e.state["needs"], e.cfg)
    e.world.events = None


# ---------- pure: tendencies ----------
def test_tendency_clamped_to_max_step_and_range(cfg):
    step = cfg.settings["sleep"]["tendency_max_step"]
    cur = {"boldness": 0.5, "sociability": 0.99, "tidiness": 0.01}
    new, ch = sleep.clamp_tendencies(cfg, cur, {"boldness": 100, "sociability": 1, "tidiness": -1})
    assert new["boldness"] == pytest.approx(0.5 + step)
    assert new["sociability"] == 1.0 and new["tidiness"] == 0.0
    assert ch["boldness"] == {"from": 0.5, "to": 0.55}


@pytest.mark.parametrize("bad", [None, [], "x", {"boldness": "up"}, {"boldness": float("nan")},
                                 {"boldness": True}, {"unknown": 1}])
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
    set_time(e, "2026-11-18T23:00", rest=5, food=5)
    info = e.tick()
    assert info["kind"] == "asleep" and e.state["is_asleep"]
    assert not any(t == "brain" for t, _ in b.calls)
    assert db.get_meta(e.conn, "sleep_started") == "2026-11-18T23:00"


def test_exhausted_by_day_does_not_force_sleep(cfg):
    e, b = make(cfg)
    set_time(e, "2026-11-18T12:00", rest=5, food=5)
    assert e.tick()["kind"] == "tick"


def test_brain_can_choose_sleep_when_tired_or_night(cfg):
    e, b = make(cfg)
    quiet_brain(b, wants_sleep=True)
    set_time(e, "2026-11-18T14:00", rest=20, food=20)          # tired by day
    e.tick()
    assert e.state["is_asleep"]
    e2, b2 = make(cfg, seed=2)
    quiet_brain(b2, wants_sleep=True)
    set_time(e2, "2026-11-18T12:00", rest=100, food=100)       # not tired, daytime: ignored
    e2.tick()
    assert not e2.state["is_asleep"]
    e3, b3 = make(cfg, seed=3)
    quiet_brain(b3, wants_sleep=True)
    set_time(e3, "2026-11-18T22:00", rest=100, food=100)       # night: honoured
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
    set_time(e, "2026-11-18T22:00", rest=5, food=60)
    e.tick()
    assert e.state["is_asleep"]
    seen = []
    info = run_until(e, lambda i: (seen.append(i), i["outcome"].startswith("wakes"))[1])
    st = db.get_state(e.conn)
    assert not st["is_asleep"] and st["needs"]["rest"] >= cfg.settings["sleep"]["wake_rest_at_least"]
    t = clock.parse(st["sim_time"])
    assert t.hour >= cfg.settings["sleep"]["wake_not_before_hour"]
    assert all(i["kind"] == "asleep" for i in seen)
    assert e.tick()["kind"] == "tick"                         # awake again, back to normal


def test_does_not_wake_before_morning_even_if_rested(cfg):
    e, b = make(cfg)
    set_time(e, "2026-11-18T21:30", rest=50, food=60)
    e.state["is_asleep"] = True
    db.set_meta(e.conn, "sleep_started", e.state["sim_time"])
    for _ in range(3):
        e.tick()
        assert e.state["needs"]["rest"] <= 100
    assert clock.parse(e.state["sim_time"]).hour < 6 or clock.parse(e.state["sim_time"]).day == 19
    assert e.state["is_asleep"] or clock.parse(e.state["sim_time"]).hour >= 6


def test_max_sleep_hours_cap(cfg):
    cfg.settings["sleep"]["restore_per_sim_hour"] = 0          # never gets rested
    e, b = make(cfg)
    set_time(e, "2026-11-18T22:00", rest=5, food=60)
    e.tick()
    info = run_until(e, lambda i: i["outcome"].startswith("wakes"), limit=20)
    assert not e.state["is_asleep"]


def test_sleep_decay_is_slower(cfg):
    e, b = make(cfg)
    set_time(e, "2026-11-18T22:00", rest=5, fun=80, food=80)
    f0 = e.state["needs"]["fun"]
    e.tick()
    drop = f0 - e.state["needs"]["fun"]
    full = cfg.character["needs"]["fun"]["decay"] * cfg.settings["sleep"]["sim_minutes_per_tick"] / 60
    assert drop == pytest.approx(full * cfg.settings["sleep"]["decay_multiplier"])


# ---------- engine: the sleep cycle ----------
def seed_day(e, b):
    """A day with a few notable moments."""
    b.handlers["brain"] = lambda m, s: {"reaction": "wow!", "thought": "", "action": "watch the sunset",
                                        "intent": "", "feeling": "positive", "wants_sleep": False}
    b.handlers["narrator"] = lambda m, s: {"outcome": "It was beautiful.", "gains": {"fun": "++"},
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
    set_time(e, "2026-11-18T23:00", rest=5, food=60)
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
    set_time(e, "2026-11-18T23:00", rest=5, food=60)
    e.tick()
    assert set(e.state["tendencies"]) == set(cfg.character["tendencies"])
    assert cfg.character["core_traits"] == traits


def test_offline_sleep_postpones_and_recovers_without_losing_memories(cfg):
    e, b = make(cfg)
    seed_day(e, b)
    shorts = len(e.memory.all_short())
    b.handlers.pop("brain"); b.handlers.pop("narrator")
    set_time(e, "2026-11-18T22:00", rest=5, food=60)
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
    set_time(e, "2026-11-18T23:00", rest=5, food=60)
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
    set_time(e, "2026-11-18T12:00", rest=100, food=100)
    quiet_brain(b, action="first-day-thing")
    e.tick()
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": "second-day-thing",
                                        "intent": "", "feeling": "neutral", "wants_sleep": True}
    set_time(e, "2026-11-18T22:00", rest=100, food=100)
    e.tick()                                                   # wants_sleep at night -> cycle 1
    assert "first-day-thing" in seen[0]
    # wake, second day
    run_until(e, lambda i: i["outcome"].startswith("wakes"))
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": "second-day-thing",
                                        "intent": "", "feeling": "neutral", "wants_sleep": False}
    e.tick()
    set_time(e, "2026-11-19T23:00", rest=5, food=5)
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
    assert all(0 <= r[0] <= 100 for r in e.conn.execute("SELECT json_extract(needs_after,'$.rest') FROM ticks"))

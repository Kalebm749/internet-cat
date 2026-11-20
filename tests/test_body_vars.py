"""Mood, energy and hunger (docs/rewrite-contracts.md §2.2, §2.3)."""
import copy

import pytest

from cat import content, tuning
from cat.body import Body, BodyState


@pytest.fixture(scope="module")
def t():
    return tuning.load()["body"]


@pytest.fixture(scope="module")
def body(t):
    return Body(t, content.load().world)


def by_cause(changes):
    return {(c.var, c.cause): c.delta for c in changes}


def test_awake_day_without_effort(body, t):
    st = BodyState(mood=0.1, energy=0.8, hunger=0.2)
    ch = by_cause(body.passage(st, 16 * 60))
    assert st.hunger == pytest.approx(0.2 + 16 * t["hunger_per_hour"])
    assert st.energy == pytest.approx(0.8 - 16 * t["awake_energy_per_hour"])
    assert ch[("hunger", "time")] > 0 and ch[("energy", "time")] < 0
    assert ch[("mood", "hunger")] < 0          # hunger passed the threshold late in the day
    assert st.mood < 0.1


def test_effort_spends_energy_and_makes_hungry(body, t):
    st = BodyState(0.1, 0.8, 0.2)
    ch = by_cause(body.passage(st, 60, effort_per_min=0.002, cause="inspect"))
    assert ch[("energy", "effort (inspect)")] == pytest.approx(-0.12)
    assert ch[("hunger", "effort (inspect)")] == pytest.approx(0.12 * t["hunger_per_effort"])


def test_effort_cannot_spend_energy_it_does_not_have(body):
    st = BodyState(0.1, 0.01, 0.2)
    body.passage(st, 60, effort_per_min=0.01)
    assert st.energy == 0.0


def test_resting_restores_through_per_minute(body):
    st = BodyState(0.1, 0.5, 0.2)
    ch = by_cause(body.passage(st, 20, per_minute={"energy": 0.002}, cause="sit_and_reflect"))
    assert ch[("energy", "sit_and_reflect")] == pytest.approx(0.04)


def test_sleep_restores_less_when_hungry(body, t):
    fed, hungry = BodyState(0.1, 0.2, 0.0), BodyState(0.1, 0.2, 0.9)
    body.passage(fed, 8 * 60, asleep=True)
    body.passage(hungry, 8 * 60, asleep=True)
    assert fed.energy > hungry.energy > 0.2
    assert fed.hunger == pytest.approx(8 * t["hunger_per_hour"] * t["hunger_sleep_factor"])


def test_mood_drifts_to_baseline_without_overshooting(body, t):
    low, high = BodyState(-0.5, 0.5, 0.0), BodyState(0.12, 0.5, 0.0)
    body.passage(low, 60)
    body.passage(high, 600)
    assert low.mood == pytest.approx(-0.5 + t["mood_drift_per_hour"])
    assert high.mood == pytest.approx(t["mood_baseline"])


def test_step_size_does_not_change_the_result(body):
    a, b = BodyState(0.1, 0.8, 0.5), BodyState(0.1, 0.8, 0.5)
    body.passage(a, 60, effort_per_min=0.001)
    for _ in range(6):
        body.passage(b, 10, effort_per_min=0.001)
    assert (a.mood, a.energy, a.hunger) == pytest.approx((b.mood, b.energy, b.hunger))


def test_apply_clamps_and_reports_the_real_delta(body):
    st = BodyState(0.9, 0.5, 0.2)
    ch = body.apply(st, {"mood": 0.5, "hunger": -0.45}, "eat_offering")
    assert st.mood == 1.0 and st.hunger == 0.0
    assert by_cause(ch) == {("mood", "eat_offering"): pytest.approx(0.1), ("hunger", "eat_offering"): pytest.approx(-0.2)}


def test_body_words_and_derived_conditions(body):
    d = body.describe(BodyState(mood=0.6, energy=0.9, hunger=0.7))
    assert d.words == ["Your stomach is a small, insistent knot.", "You feel light and quick."]
    assert d.derived == "buoyant"
    assert body.describe(BodyState(0.0, 0.8, 0.0), repetition=0.7).derived == "restless"
    assert body.describe(BodyState(0.0, 0.8, 0.0), repetition=0.0).derived is None
    assert body.describe(BodyState(-0.5, 0.1, 0.0)).derived == "drained"


def test_a_plain_day_balances(body, t):
    """Tuning guard: half the waking day in low-effort activities, two meals, eight hours of sleep.
    Over a week hunger never reaches the worst band, and energy runs low by bedtime and recovers by morning."""
    effort = content.load().world["effort"]["low"]
    st = Body.start(t)
    for day in range(7):
        evening = None
        for hour in range(16):
            body.passage(st, 60, effort_per_min=effort if hour % 2 else 0.0)
            if hour in (4, 10):                          # 11:00 and 17:00
                body.apply(st, {"hunger": -0.45, "energy": 0.05, "mood": 0.04}, "eat_offering")
            assert st.hunger < 0.85, (day, hour, st)
        evening = copy.copy(st)
        body.passage(st, 8 * 60, asleep=True)
        assert 0.1 < evening.energy < 0.5, (day, evening)
        assert st.energy > 0.7, (day, st)


def test_tuning_validation_lists_every_problem(tmp_path):
    good = tuning.load()
    assert tuning.validate(good) == []
    bad = copy.deepcopy(good)
    bad["body"]["hunger_per_hour"] = 2
    del bad["body"]["step_minutes"]
    bad["body"]["start"]["mood"] = 5
    bad["speed"] = 1
    probs = tuning.validate(bad)
    assert "unknown key 'speed'" in probs
    assert "body: missing 'step_minutes'" in probs
    assert any(p.startswith("body.hunger_per_hour") for p in probs)
    assert any(p.startswith("body.start.mood") for p in probs)

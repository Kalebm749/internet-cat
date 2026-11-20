"""When decisions happen (docs/rewrite-contracts.md §3.1, §3.2), driven by a fake clock."""
import pytest

from cat import clock, tuning
from cat.scheduler import Scheduler

T0 = "2026-11-21T08:00"


@pytest.fixture
def s():
    return Scheduler(tuning.load()["decisions"])        # interval 15 s, check-in 30 sim min, salience 0.4


def run(s, due, real, sim):
    s.started(due, real, sim)
    s.finished()


def test_first_decision_is_immediate_then_every_interval_while_idle(s):
    d = s.due(0, T0, None)
    assert d.kind == "full" and d.triggers[0].kind == "interval"
    run(s, d, 0, T0)
    assert s.due(14.9, T0, None) is None
    assert s.due(15, T0, None).kind == "full"


def test_no_decisions_during_an_activity_except_check_ins(s):
    run(s, s.due(0, T0, None), 0, T0)
    act = {"started": T0}
    calls = []
    real, sim = 0.0, T0
    for _ in range(120):                                # 120 x 2.5 s real = 5 real min = 120 sim min at 24x
        real += 2.5
        sim = clock.advance(sim, 1)
        d = s.due(real, sim, act)
        if d:
            calls.append((d.kind, sim))
            run(s, d, real, sim)
    assert [k for k, _ in calls] == ["check_in"] * 4
    assert [t[-5:] for _, t in calls] == ["08:30", "09:00", "09:30", "10:00"]


def test_an_unfinished_decision_is_never_interrupted(s):
    d = s.due(0, T0, None)
    s.started(d, 0, T0)                                  # the model is still thinking ...
    assert s.due(60, T0, None) is None                   # ... long past the interval
    assert s.note("event", "a glint", salience=0.9)
    assert s.due(60, T0, None) is None
    s.finished()
    nxt = s.due(61, T0, None)
    assert nxt.kind == "full" and [t.kind for t in nxt.triggers] == ["event"]


def test_events_interrupt_activities_only_when_salient(s):
    run(s, s.due(0, T0, None), 0, T0)
    act = {"started": T0}
    assert not s.note("event", "spray", salience=0.3)
    assert s.due(5, clock.advance(T0, 2), act) is None
    assert s.note("event", "a glint", salience=0.6)
    d = s.due(5, clock.advance(T0, 2), act)
    assert d.kind == "interrupt" and d.triggers[0].reason == "a glint"


def test_triggers_are_merged_and_finished_activity_wins(s):
    run(s, s.due(0, T0, None), 0, T0)
    act = {"started": T0}
    s.note("body", "hunger band")
    s.note("activity_finished", "sit_and_reflect")
    d = s.due(3, T0, act)
    assert d.kind == "full" and [t.kind for t in d.triggers] == ["body", "activity_finished"]
    run(s, d, 3, T0)
    assert s.pending == []


def test_a_decision_resets_the_check_in_clock(s):
    run(s, s.due(0, T0, None), 0, T0)
    act = {"started": T0}
    s.note("mail", "a letter")
    d = s.due(1, clock.advance(T0, 20), act)
    run(s, d, 1, clock.advance(T0, 20))                  # decided to continue at 08:20
    assert s.due(2, clock.advance(T0, 35), act) is None  # 15 minutes since that decision
    assert s.due(3, clock.advance(T0, 50), act).kind == "check_in"


def test_interval_and_check_in_cannot_be_noted(s):
    with pytest.raises(ValueError):
        s.note("interval")

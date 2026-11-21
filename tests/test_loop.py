"""The engine loop end to end with the mock brain and a fake clock (docs/rewrite-contracts.md §3)."""
import json

import pytest

from cat import clock, content, store, tuning
from cat.llm import MockBackend
from cat.loop import FakeClock, Loop


def make(tmp_path, seed=1, backend=None):
    t = tuning.load()
    fc = FakeClock()
    lp = Loop(store.connect(tmp_path / "nit.db"), content.load(), t, backend or MockBackend(seed=seed), clock=fc, seed=seed)
    return lp, fc, t


def hours(lp, fc, t, h):
    steps = int(h * 3600 / (t["time_scale"] * t["loop"]["step_seconds"]))
    lp.run(steps, sleep=fc.sleep)


def q(lp, sql, *a):
    return lp.conn.execute(sql, a).fetchall()


def test_a_mock_day_end_to_end(tmp_path):
    lp, fc, t = make(tmp_path)
    hours(lp, fc, t, 26)
    assert lp.life.sim_time >= "2026-11-22T08:55"
    for var, lo, hi in (("mood", -1, 1), ("energy", 0, 1), ("hunger", 0, 1)):
        vals = [r[0] for r in q(lp, "SELECT value FROM body_changes WHERE var = ?", var)]
        assert vals and lo <= min(vals) and max(vals) <= hi
    assert q(lp, "SELECT COUNT(*) FROM activities WHERE status = 'running'")[0][0] <= 1
    assert q(lp, "SELECT COUNT(*) FROM activities WHERE action_id = 'sleep' AND status = 'finished'")[0][0] >= 1
    assert q(lp, "SELECT COUNT(*) FROM decisions WHERE fallback = 1")[0][0] == 0
    d = q(lp, "SELECT mind, messages, attempts FROM decisions LIMIT 1")[0]
    assert json.loads(d[0])["options"] and json.loads(d[1])[0]["role"] == "system" and json.loads(d[2])


def test_no_decisions_inside_activities_except_check_ins(tmp_path):
    lp, fc, t = make(tmp_path, seed=4)
    hours(lp, fc, t, 12)
    acts = q(lp, "SELECT started_at, ended_at, action_id FROM activities WHERE status != 'rejected' AND ended_at IS NOT NULL")
    decs = q(lp, "SELECT sim_time, kind FROM decisions")
    for start, end, action in acts:
        inside = [k for s, k in decs if start < s < end]
        assert all(k in ("check_in", "interrupt") for k in inside), (action, start, end, inside)


def test_dozes_while_the_brain_is_offline_and_comes_back(tmp_path):
    b = MockBackend()
    lp, fc, t = make(tmp_path, backend=b)
    b.online = False
    hours(lp, fc, t, 1)
    assert q(lp, "SELECT COUNT(*) FROM decisions")[0][0] == 0
    assert lp.life.sim_time >= "2026-11-21T07:55"                     # time goes on
    b.online = True
    hours(lp, fc, t, 1)
    assert q(lp, "SELECT COUNT(*) FROM decisions")[0][0] > 0


def test_thinking_takes_sim_time(tmp_path):
    b = MockBackend()
    lp, fc, t = make(tmp_path, backend=b)

    def slow(msgs, schema):
        fc.sleep(10)                                                  # 10 real seconds = 4 sim minutes
        return {"thought": "slowly", "reasoning": "slowly", "action": "inspect", "inputs": {}, "preserve": False,
                "letter": None}
    b.handlers["decide"] = slow
    lp.step()
    start = q(lp, "SELECT started_at FROM activities")[0][0]
    assert start == clock.advance("2026-11-21T07:00", 4)
    assert q(lp, "SELECT ms FROM decisions")[0][0] == 10_000


def test_a_failed_step_changes_nothing_and_the_loop_recovers(tmp_path, monkeypatch):
    lp, fc, t = make(tmp_path)
    lp.life.content.events.clear()
    hours(lp, fc, t, 0.5)
    seen = {}

    def boom(*a, **k):                     # runs inside the decision's transaction, after the decision row
        seen.update({tbl: q(lp, f"SELECT COUNT(*) FROM {tbl}")[0][0] for tbl in ("decisions", "activities")})
        seen["state"] = q(lp, "SELECT data FROM state")[0][0]
        lp.life.world.values["market.offerings"] = 99               # a change in memory, then a crash
        raise RuntimeError("boom")
    monkeypatch.setattr(lp.exec, "start", boom)
    with pytest.raises(RuntimeError):
        for _ in range(500):
            lp.step()
            fc.sleep(t["loop"]["step_seconds"])
    assert q(lp, "SELECT COUNT(*) FROM decisions")[0][0] == seen["decisions"] - 1      # the decision row rolled back
    assert q(lp, "SELECT COUNT(*) FROM activities")[0][0] == seen["activities"]
    lp.reload()                                                      # what run() does after an error
    assert lp.life.world.get("market.offerings") != 99
    before = q(lp, "SELECT COUNT(*) FROM decisions")[0][0]
    hours(lp, fc, t, 0.5)
    assert q(lp, "SELECT COUNT(*) FROM decisions")[0][0] > before


def test_reopening_continues_the_same_life(tmp_path):
    lp, fc, t = make(tmp_path)
    hours(lp, fc, t, 2)
    where, when = lp.life.location, lp.life.sim_time
    lp2 = Loop(lp.conn, content.load(), t, MockBackend(), clock=FakeClock(), seed=2)
    assert (lp2.life.location, lp2.life.sim_time) == (where, when) and not lp2.life.spawned


# ---------------------------------------------------------------- hunger takes over (§2.5)


def test_starving_walks_to_the_food_and_eats_without_asking_the_model(tmp_path):
    lp, fc, t = make(tmp_path)
    lp.life.content.events.clear()
    asked = []
    lp.backend.handlers["decide"] = lambda m, s: (asked.append(1), {"thought": "hm", "reasoning": "hm", "action": "inspect",
                                                                    "inputs": {}, "preserve": False, "letter": None})[1]
    lp.life.bodystate.hunger = 0.85
    lp.life.world.values["market.offerings"] = 0                 # the stall is empty
    lp.run(60, sleep=fc.sleep)
    acts = [r[0] for r in q(lp, "SELECT action_id FROM activities ORDER BY id")]
    assert acts[:2] == ["move", "eat_offering"]
    assert [json.loads(r[0]) for r in q(lp, "SELECT attempts FROM decisions ORDER BY id LIMIT 2")] == [[], []]   # no model
    assert lp.life.bodystate.hunger < 0.5 and lp.life.location == "market"
    notes = [json.loads(r[0]) for r in q(lp, "SELECT notes FROM decisions ORDER BY id LIMIT 2")]
    assert notes == [["forced: starving"], ["forced: starving"]]


def test_not_hungry_enough_leaves_the_choice_to_the_model(tmp_path):
    lp, fc, t = make(tmp_path)
    lp.life.bodystate.hunger = 0.5
    lp.step()
    assert q(lp, "SELECT COUNT(*) FROM decisions WHERE notes LIKE '%forced%'")[0][0] == 0

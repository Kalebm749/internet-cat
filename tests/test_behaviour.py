"""Repetition and behavioural quality (docs/rewrite-contracts.md §6), as scenarios."""
import json
import random

import pytest

from cat import behaviour, content, knowledge, life, mind, sleepcycle, store, tuning
from cat.capabilities import Registry
from cat.decision import Decision
from cat.execute import Executor, load_texts
from cat.llm import MockBackend
from cat.loop import FakeClock, Loop
from cat.scheduler import Scheduler


class Rig:
    def __init__(self, tmp_path):
        self.t = tuning.load()
        self.t["body"]["mood_drift_per_hour"] = 0.0
        self.conn = store.connect(tmp_path / "nit.db")
        self.life = life.open_life(self.conn, content.load(), self.t)
        self.life.content.events.clear()
        self.life.bodystate.mood = 0.0
        self.ex = Executor(self.conn, self.life, self.t, load_texts(), Registry(self.life.content),
                           Scheduler(self.t["decisions"]), random.Random(0))
        self.life.content.actions["make_wish"].requires = [{"energy_gte": 0.1}]   # repetition, not the daily limit
        with store.transaction(self.conn):
            for a in ("make_wish", "assess_wishes"):
                knowledge.learn(self.conn, a, "fountain", "inspect", self.life.sim_time)

    def do(self, action, inputs=None, kind="full"):
        with store.transaction(self.conn):
            return self.ex.start(Decision(kind=kind, action=action, inputs=inputs or {}), None)

    def finish(self):
        with store.transaction(self.conn):
            self.ex.advance(int(self.ex.running.planned) + 1 if self.ex.running else 1)

    def last(self, col):
        return self.conn.execute(f"SELECT {col} FROM activities ORDER BY id DESC LIMIT 1").fetchone()[0]

    def mood_gain(self):
        changes = json.loads(self.last("changes"))
        return sum(c["delta"] for c in changes if c["var"] == "mood")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_continuing_is_not_restarting(rig):
    rig.do("sit_and_reflect")
    with store.transaction(rig.conn):
        rig.ex.advance(5)
    for _ in range(3):                                      # three check-ins: continue
        rig.do("continue", kind="check_in")
        with store.transaction(rig.conn):
            rig.ex.advance(3)
    assert rig.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1
    rig.finish()
    assert rig.last("freshness") == 1.0 and rig.last("restart_of") is None
    first = rig.last("id")
    rig.do("sit_and_reflect")                               # finished, then started again at once: a restart
    assert rig.last("restart_of") == first and rig.last("freshness") < 1.0


def test_the_same_thing_again_and_again_feels_flatter(rig):
    gains, fresh = [], []
    for _ in range(4):
        rig.do("make_wish", {"kind": "I wish I could", "what": "to remember"})
        fresh.append(rig.last("freshness"))
        rig.finish()
        gains.append(rig.mood_gain())
    assert fresh[0] == 1.0 and fresh == sorted(fresh, reverse=True) and fresh[-1] < 0.5
    assert gains == sorted(gains, reverse=True) and gains[-1] < gains[0] / 2
    parts = json.loads(rig.last("repeat_parts"))
    assert parts["action"] > 0 and parts["target"] > 0


def test_a_different_target_is_fresher_than_the_same_one(rig):
    rig.do("make_wish", {"kind": "I wish I could", "what": "to remember"})
    rig.finish()
    same = behaviour.freshness(rig.conn, rig.life.content, rig.t, rig.life.content.actions["make_wish"],
                               {"kind": "I wish I could", "what": "  To   Remember "}, rig.life.sim_time, rig.life.world.values)
    other = behaviour.freshness(rig.conn, rig.life.content, rig.t, rig.life.content.actions["make_wish"],
                                {"kind": "I wish I could", "what": "to fly"}, rig.life.sim_time, rig.life.world.values)
    assert same.value < other.value < 1.0


def test_a_meaningful_return_is_fresh(rig):
    rig.life.world.values["fountain.wishes"] = [{"id": "1", "text": "x", "status": "open"}]
    rig.do("assess_wishes")
    rig.finish()
    rig.do("assess_wishes")                                  # nothing changed: a stale check
    stale = rig.last("freshness")
    assert stale < 1.0
    rig.finish()
    rig.do("make_wish", {"kind": "I wish I could", "what": "a new one"})
    rig.finish()
    rig.do("assess_wishes")                                  # a new wish to look at: fresh again
    parts = json.loads(rig.last("repeat_parts"))
    assert parts["revisit_changed"] is True and parts["action"] == 0 and parts["target"] == 0
    assert rig.last("freshness") > stale                     # only the wish family (it just wished) still counts


def test_returning_somewhere_for_nothing_counts(rig):
    rig.do("move", {"to": "market"})
    rig.finish()
    rig.do("move", {"to": "fountain"})                       # did nothing at the market
    rig.finish()
    rig.do("move", {"to": "market"})
    assert json.loads(rig.last("repeat_parts"))["aimless"] > 0
    rig.finish()
    rig.life.world.values["market.offerings"] = 2
    rig.life.bodystate.hunger = 0.6
    rig.do("eat_offering")
    rig.finish()
    rig.do("move", {"to": "fountain"})
    rig.finish()
    f = behaviour.freshness(rig.conn, rig.life.content, rig.t, rig.life.content.actions["move"], {"to": "market"},
                            rig.life.sim_time, rig.life.world.values)
    assert f.parts["aimless"] == 0                           # last time it ate there


def test_stale_options_get_a_note_but_stay(tmp_path):
    t = tuning.load()
    fc = FakeClock()
    lp = Loop(store.connect(tmp_path / "nit.db"), content.load(), t, MockBackend(), clock=fc, seed=1)
    lp.life.content.events.clear()
    lp.life.content.actions["make_wish"].requires = [{"energy_gte": 0.1}]
    with store.transaction(lp.conn):
        knowledge.learn(lp.conn, "make_wish", "fountain", "inspect", lp.life.sim_time)
    for _ in range(3):
        with store.transaction(lp.conn):
            lp.exec.start(Decision(kind="full", action="make_wish", inputs={"kind": "I wish I could", "what": "x"}), None)
            lp.exec.advance(10)
    m = mind.build(lp.life, t, known=knowledge.known_ids(lp.conn), notes=lp._notes())
    assert m.option("make_wish").notes == ["you have done this a lot lately"]
    assert m.option("sit_and_reflect").notes == []
    assert behaviour.repetition_score(lp.conn, t["repetition"]["recent"]) > 0.3


def test_a_stale_routine_makes_it_restless(rig):
    for _ in range(5):
        rig.do("make_wish", {"kind": "I wish I could", "what": "to remember"})
        rig.finish()
    rep = behaviour.repetition_score(rig.conn, rig.t["repetition"]["recent"])
    rig.life.bodystate.mood, rig.life.bodystate.energy = 0.0, 0.8
    assert rig.life.body.describe(rig.life.bodystate, rep).derived == "restless"


def test_the_same_thought_again_costs_a_little(tmp_path):
    t = tuning.load()
    fc = FakeClock()
    b = MockBackend()
    lp = Loop(store.connect(tmp_path / "nit.db"), content.load(), t, b, clock=fc, seed=1)
    lp.life.content.events.clear()
    b.handlers["decide"] = lambda m, s: {"thought": "Where did I come from?", "reasoning": "I wonder.",
                                         "action": "sit_and_reflect", "inputs": {}, "preserve": False, "letter": None}
    while lp.conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0] < 2:
        fc.sleep(t["loop"]["step_seconds"])
        lp.step()
    rows = lp.conn.execute("SELECT repeated FROM thoughts ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [0, 1]
    assert lp.conn.execute("SELECT COUNT(*) FROM body_changes WHERE cause = 'same thought'").fetchone()[0] == 1
    assert "You keep coming back to the same thought." in lp.exec.perception


def test_the_same_night_entry_is_retried_then_penalised(tmp_path):
    t = tuning.load()
    conn = store.connect(tmp_path / "nit.db")
    c = content.load()
    lf = life.open_life(conn, c, t)
    b = MockBackend()
    asked = []

    def same(msgs, schema):
        asked.append(msgs[-1]["content"])
        return {"summary": "I sat by the water and thought about the coins.", "mattered": []}
    b.handlers["sleep_consolidate"] = same
    p = sleepcycle.load_prompts()
    for night in range(3):
        conn.execute("INSERT INTO experiences(sim_time, kind, location, text) VALUES (?, 'activity', 'fountain', 'x')",
                     (f"2026-11-2{1 + night}T10:00",))
        plan = sleepcycle.plan(conn, c, t, b, p, lf.character.sheet, f"2026-11-2{1 + night}T22:00")
        with store.transaction(conn):
            sleepcycle.commit(conn, plan, f"2026-11-2{2 + night}T05:00")
    assert [r[0] for r in conn.execute("SELECT repeated FROM sleep_runs ORDER BY id")] == [0, 1, 1]
    assert "very close to what Nit wrote on 2026-11-21" in asked[2]           # night 2 was asked once more
    assert sleepcycle.repeat_streak(conn) == 2

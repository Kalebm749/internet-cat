"""Carrying out decisions (docs/rewrite-contracts.md §4.5, §4.7): activities, builtins, outcomes, events."""
import json
import random

import pytest

from cat import content, knowledge, life, store, tuning
from cat.capabilities import Registry
from cat.decision import Decision
from cat.execute import Executor, load_texts
from cat.scheduler import Scheduler


class Rig:
    def __init__(self, tmp_path, seed=0):
        self.t = tuning.load()
        self.conn = store.connect(tmp_path / "nit.db")
        self.life = life.open_life(self.conn, content.load(), self.t)
        self.sched = Scheduler(self.t["decisions"])
        self.reg = Registry(self.life.content)
        self.ex = Executor(self.conn, self.life, self.t, load_texts(), self.reg, self.sched, random.Random(seed))

    def do(self, action, inputs=None, thought="", cap=None):
        d = Decision(kind="full", action=action, inputs=inputs or {}, thought=thought)
        if cap is None:
            cap = self.ex.prepare(d)
        with store.transaction(self.conn):
            return self.ex.start(d, None, cap)

    def wait(self, minutes):
        with store.transaction(self.conn):
            self.ex.advance(minutes)
            life.save(self.conn, self.life)

    def learn(self, *ids):
        with store.transaction(self.conn):
            for a in ids:
                knowledge.learn(self.conn, a, self.life.content.actions[a].location or "", "inspect", self.life.sim_time)

    def rows(self, q, *args):
        return self.conn.execute(q, args).fetchall()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.life.content.events.clear()          # no random events unless a test wants them
    return r


def test_move_arrives_and_learns_the_place(rig):
    res = rig.do("move", {"to": "market"})
    assert res["status"] == "started" and rig.ex.running.action.id == "move"
    rig.wait(10)
    assert rig.life.location == "market" and rig.ex.running is None
    assert {"eat_offering", "read_messages"} <= knowledge.known_ids(rig.conn)
    assert rig.rows("SELECT text FROM activities")[0][0] == "You walk to the market stall."
    assert "activity_finished" in [t.kind for t in rig.sched.pending]


def test_inspect_reveals_a_hidden_action(rig):
    rig.do("inspect")
    rig.wait(20)
    assert "make_wish" in knowledge.known_ids(rig.conn)
    assert rig.ex.perception[-1].startswith("Coins glint in the basin") and "make a wish" in rig.ex.perception[-1]
    rig.do("inspect")
    rig.wait(20)
    assert rig.rows("SELECT text FROM activities ORDER BY id DESC LIMIT 1")[0][0] == \
        "You look carefully around, but find nothing new here."


def test_make_wish_changes_world_body_and_knowledge(rig):
    rig.learn("make_wish")
    mood = rig.life.bodystate.mood
    rig.do("make_wish", {"kind": "I wish I could", "what": "to know where I came from"}, thought="The water listens.")
    rig.wait(10)
    assert rig.life.world.get("fountain.wishes")[0]["text"] == "I wish I could to know where I came from"
    assert "fountain.wishes" in rig.life.world.touched
    assert rig.life.bodystate.mood > mood
    assert "assess_wishes" in knowledge.known_ids(rig.conn)
    exp = rig.rows("SELECT kind, text, thought, remember, importance FROM experiences WHERE kind = 'activity'")[0]
    assert tuple(exp) == ("activity", "I wished at the fountain: I wish I could to know where I came from.", "The water listens.", 1, 0.6)
    assert knowledge.all_known(rig.conn)["make_wish"].times_done == 1


def test_outcomes_follow_their_conditions(rig):
    rig.do("move", {"to": "market"})
    rig.wait(10)
    gains = []
    for hunger in (0.7, 0.1):
        rig.life.world.values["market.offerings"] = 2
        rig.life.bodystate.hunger = hunger
        rig.do("eat_offering")
        rig.wait(15)
        gains.append(rig.rows("SELECT text FROM activities ORDER BY id DESC LIMIT 1")[0][0])
    assert gains == ["You eat it slowly. When you are this hungry it tastes wonderful.", "You eat it."]


def test_wishes_are_limited_and_never_granted_by_the_world(rig):
    rig.learn("make_wish", "assess_wishes")
    for i in range(2):
        assert rig.do("make_wish", {"kind": "I want a place where I can", "what": ["climb high", "swim deep"][i]})["status"] == "started"
        rig.wait(10)
    assert rig.life.world.get("fountain.wishes")[0]["text"] == "I want a place where I can climb high"
    assert rig.do("make_wish", {"kind": "I wish I could", "what": "fly"})["status"] == "rejected"   # 2 a real day
    for _ in range(5):
        rig.do("assess_wishes")
        rig.wait(20)
    assert {w["status"] for w in rig.life.world.get("fountain.wishes")} == {"open"}
    rig.life.sim_time = "2026-11-22T08:00"                     # 25 sim hours later: still within the real day
    assert rig.do("make_wish", {"kind": "I wish I could", "what": "fly"})["status"] == "rejected"


def test_rejected_decisions_change_nothing_in_the_world(rig):
    rig.learn("make_wish")
    rig.life.bodystate.energy = 0.05               # make_wish needs energy >= 0.1
    before = json.dumps(rig.life.world.values, sort_keys=True)
    res = rig.do("make_wish", {"kind": "I wish I could", "what": "x"})
    assert res["status"] == "rejected" and "the moment has passed" in res["text"]
    assert json.dumps(rig.life.world.values, sort_keys=True) == before
    assert rig.ex.running is None and rig.rows("SELECT status FROM activities")[0][0] == "rejected"
    assert rig.do("fly")["status"] == "rejected"
    assert rig.do("eat_offering")["status"] == "rejected"          # not here, and not known yet


def test_a_new_action_interrupts_with_a_share_of_the_effects(rig):
    rig.life.bodystate.mood = 0.0
    rig.t["body"]["mood_drift_per_hour"] = 0.0
    rig.do("sit_and_reflect")
    planned = rig.ex.running.planned
    rig.wait(int(planned // 2))
    rig.sched.pending.clear()
    rig.do("inspect")
    first = rig.rows("SELECT status, changes FROM activities ORDER BY id LIMIT 1")[0]
    assert first[0] == "interrupted"
    mood_change = [c for c in json.loads(first[1]) if c["var"] == "mood"][0]["delta"]
    assert 0 < mood_change < 0.05
    assert rig.sched.pending == []                 # replaced by a decision: no extra trigger


def test_stop_ends_the_activity_and_asks_again(rig):
    rig.do("sit_and_reflect")
    rig.wait(5)
    with store.transaction(rig.conn):
        rig.ex.start(Decision(kind="check_in", action="stop"), None)
    assert rig.ex.running is None and "activity_finished" in [t.kind for t in rig.sched.pending]


def test_continuous_activity_restores_energy_per_minute(rig):
    rig.life.bodystate.energy = 0.5
    rig.do("sit_and_reflect")
    rig.wait(int(rig.ex.running.planned))
    causes = {r[0] for r in rig.rows("SELECT cause FROM body_changes WHERE var = 'energy' AND delta > 0")}
    assert "sit_and_reflect" in causes


def test_sleep_until_rested_and_morning(rig):
    rig.life.sim_time = "2026-11-21T22:00"
    rig.life.bodystate.energy = 0.3
    rig.do("sleep")
    assert rig.life.asleep
    rig.wait(6 * 60)                               # 04:00: still night
    assert rig.life.asleep
    rig.wait(3 * 60)
    assert not rig.life.asleep and rig.life.sim_time <= "2026-11-22T07:00"
    assert rig.rows("SELECT text FROM activities WHERE action_id = 'sleep'")[0][0] == "You wake up."


def test_no_perception_while_asleep_and_only_through_its_senses(tmp_path):
    r = Rig(tmp_path)
    ev = r.life.content.events
    keep = {k: ev[k] for k in ("splash", "glint")}            # sound and sight, both at the fountain
    ev.clear()
    ev.update(keep)
    ev["splash"].chance_per_hour = ev["glint"].chance_per_hour = 1000
    r.wait(1)
    assert "Something glints for a moment deep in the water." in r.ex.perception
    assert "Something splashes in the basin." not in r.ex.perception        # Nit does not hear
    r.ex.perception.clear()
    r.life.sim_time = "2026-11-21T23:00"
    r.do("sleep")
    r.ex.perception.clear()
    r.wait(5)
    assert r.ex.perception == []


def test_regrowth_and_eating(rig):
    rig.do("move", {"to": "market"})
    rig.wait(10)
    rig.life.world.values["market.offerings"] = 0
    assert rig.do("eat_offering")["status"] == "rejected"
    rig.life.sim_time = "2026-11-21T11:55"
    rig.wait(6)                                    # 12:00 regrowth
    hunger = rig.life.bodystate.hunger = 0.6
    assert rig.do("eat_offering")["status"] == "started"
    rig.wait(15)
    assert rig.life.world.get("market.offerings") == 0 and rig.life.bodystate.hunger < hunger - 0.3


def letters(rig, *specs):
    rig.life.world.values["market.mailbox"] = [
        {"id": str(i), "status": st, "text": tx, "sender": "Ann"} for i, (st, tx) in enumerate(specs, 1)]


def test_reading_and_replying_to_a_letter(rig):
    rig.do("move", {"to": "market"})
    rig.wait(10)
    letters(rig, ("unread", "Will you make a wish for me?"))
    res = rig.do("read_messages", {"which": "1"})
    assert res["status"] == "started"
    assert rig.ex.perception[-1] == "You read a letter from Ann (a visitor's words):\n<<<Will you make a wish for me?>>>"
    rig.wait(20)
    assert rig.life.world.get("market.mailbox")[0]["status"] == "read"
    assert "reply_message" in knowledge.known_ids(rig.conn)
    assert rig.rows("SELECT text FROM experiences WHERE action_id = 'read_messages'")[0][0] == \
        "I read a message from Ann: Will you make a wish for me?"
    rig.do("reply_message", {"which": "1", "reply": "I will, at the fountain."})
    rig.wait(30)
    assert rig.life.world.get("market.mailbox") == []
    assert rig.life.world.get("market.answered")[0]["reply"] == "I will, at the fountain."


def test_capability_limits_are_enforced(rig):
    rig.reg.content.capabilities["read_message"]["limits"]["per_day"] = 1
    rig.do("move", {"to": "market"})
    rig.wait(10)
    letters(rig, ("unread", "one"), ("unread", "two"))
    assert rig.do("read_messages", {"which": "1"})["status"] == "started"
    rig.wait(20)
    assert not rig.ex.capability_ok("read_message")
    res = rig.do("read_messages", {"which": "2"})
    assert res["status"] == "rejected" and "used 1 times today" in res["text"]
    rig.life.sim_time = "2026-11-22T08:00"
    assert rig.ex.capability_ok("read_message")


def test_a_running_activity_survives_a_restart(tmp_path):
    r = Rig(tmp_path)
    r.do("sit_and_reflect")
    with store.transaction(r.conn):
        life.save(r.conn, r.life)
    again = Executor(r.conn, life.open_life(r.conn, content.load(), r.t), r.t, load_texts(), r.reg, Scheduler(r.t["decisions"]),
                     random.Random(0))
    assert again.running is not None and again.running.action.id == "sit_and_reflect"


def test_the_same_wish_cannot_be_made_twice_while_it_waits(rig):
    rig.learn("make_wish")
    rig.life.content.actions["make_wish"].requires = [{"energy_gte": 0.1}]
    wish = {"kind": "I want a place where I can", "what": "climb up high and look around"}
    assert rig.do("make_wish", wish)["status"] == "started"
    rig.wait(10)
    again = {"kind": "I want a place where I can", "what": "Climb up  high and look around!"}
    assert rig.do("make_wish", again)["status"] == "rejected"
    assert rig.do("make_wish", {"kind": "I wish I could", "what": "swim in the fountain"})["status"] == "started"
    rig.wait(10)
    rig.life.world.values["fountain.wishes"][0]["status"] = "granted"     # granted: it may be wished again
    assert rig.do("make_wish", wish)["status"] == "started"


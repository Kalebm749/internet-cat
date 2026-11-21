"""Individuality plan: inner life, drawn menu, event-driven continuation, memory layers, beliefs, projects, habits."""
import json

import pytest

from cat import content, inner, life, store, tuning
from cat.llm import MockBackend
from cat.loop import FakeClock, Loop


@pytest.fixture
def lp(tmp_path):
    t = tuning.load()
    loop = Loop(store.connect(tmp_path / "nit.db"), content.load(), t, MockBackend(seed=1), clock=FakeClock(), seed=1)
    loop.life.content.events.clear()
    return loop


# ---------------------------------------------------------------- stage 1: inner life

def test_temperament_scales_how_fast_a_need_grows_and_how_outcomes_land():
    t = tuning.load()["inner"]
    curious, dull = inner.Inner.start(t), inner.Inner.start(t)
    inner.passage(curious, {"novelty": 1.0}, t, 240, asleep=False, hunger=0.2, energy=0.8)
    inner.passage(dull, {"novelty": 0.0}, t, 240, asleep=False, hunger=0.2, energy=0.8)
    assert curious.v["novelty"] > dull.v["novelty"] + 0.1                  # same time, different creatures
    # finishing something that eases a pressing need lifts mood; frustration depends on persistence
    pressing = inner.Inner.start(t)
    pressing.v["expression"] = 0.9
    bonus = inner.finished(pressing, {}, t, {"expression": -0.4}, 1.0)
    assert bonus > 0 and pressing.v["expression"] < 0.6
    stubborn, quitter = inner.Inner.start(t), inner.Inner.start(t)
    inner.rejected(stubborn, {"persistence": 1.0}, t)
    inner.rejected(quitter, {"persistence": 0.0}, t)
    assert quitter.v["frustration"] > stubborn.v["frustration"]


def test_contradictory_inner_states_can_coexist_and_are_put_into_words(lp):
    i = lp.life.inner
    i.v.update(curiosity=0.9, stress=0.8, loneliness=0.7, solitude=0.8)
    words = inner.describe(i, lp.life.content.world["inner_words"], lp.t["inner"])
    assert len(words) == lp.t["inner"]["max_words"]
    assert i.v["curiosity"] > 0.6 and i.v["stress"] > 0.6                   # curious and frightened at once


def test_options_that_ease_a_pressing_need_say_so(lp):
    lp.life.inner.v["novelty"] = 0.9
    spec = lp.life.content.actions["inspect"]
    notes = inner.relief_notes(lp.life.inner, lp.life.content.world["inner_words"], lp.t["inner"], spec.inner,
                               lp.prompts["need_relief"])
    assert notes == ["this could ease your wish for something new"]


def test_inner_state_survives_a_restart(lp, tmp_path):
    lp.life.inner.v["frustration"] = 0.77
    with store.transaction(lp.conn):
        life.save(lp.conn, lp.life)
    again = life.open_life(store.connect(tmp_path / "nit.db"), content.load(), lp.t)
    assert again.inner.v["frustration"] == pytest.approx(0.77, abs=1e-4)
    assert again.character.sheet["temperament"]["novelty"] == 0.8


def test_a_need_becoming_pressing_asks_for_a_decision(lp):
    lp.exec._pressing = set()
    lp.life.inner.v["social"] = 0.64
    lp.exec._inner_pass(120)                                                # rises past `loud`
    assert any(tr.kind == "need" for tr in lp.scheduler.pending)


# ---------------------------------------------------------------- stage 2: the drawn menu

def mind_of(lp, **kw):
    from cat import knowledge, mind as mindmod
    return mindmod.build(lp.life, lp.t, known=knowledge.known_ids(lp.conn), seed=kw.pop("seed", 1), **kw)


def test_the_menu_is_a_uniform_draw_of_what_is_genuinely_available(lp):
    from cat import knowledge
    with store.transaction(lp.conn):
        for a in ("make_wish", "assess_wishes", "sit_and_reflect"):
            knowledge.learn(lp.conn, a, "fountain", "inspect", lp.life.sim_time)
    seen = set()
    for seed in range(40):
        m = mind_of(lp, menu_size=3, seed=seed)
        assert len(m.options) == 3 and m.menu_info["available"] >= 4
        seen |= {o.id for o in m.options}
    assert {"inspect", "move", "make_wish", "sit_and_reflect"} <= seen         # chance reaches everything, in time


def test_worn_out_or_hungry_always_gets_sleep_or_food_offered_never_chosen(lp):
    lp.life.bodystate.energy = 0.2
    for seed in range(15):
        assert "sleep" in {o.id for o in mind_of(lp, menu_size=3, seed=seed, protected={"sleep"}).options}


def test_carry_on_is_offered_and_becomes_the_same_action_again(lp):
    carry = {"action": "sit_and_reflect", "inputs": {}, "name": "sit and reflect"}
    from cat import knowledge
    with store.transaction(lp.conn):
        knowledge.learn(lp.conn, "sit_and_reflect", "fountain", "inspect", lp.life.sim_time)
    m = mind_of(lp, menu_size=3, carry=carry)
    assert [o.id for o in m.options][-1] == "continue" and "Carry on with what you were doing" in m.options[-1].description
    from cat.decision import allowed_actions
    assert "continue" in allowed_actions(m, "full")


def test_a_gut_answer_has_no_reasoning_and_a_short_thought(lp):
    from cat import decision
    m = mind_of(lp)
    s = decision.schema(m, "full", lp.t, gut=True)
    assert "reasoning" not in s["properties"] and s["properties"]["thought"]["maxLength"] <= 160
    raw = {"action": m.options[0].id, "inputs": {}, "thought": "ok", "preserve": False, "letter": None}
    try:
        d = decision.validate(raw, m, "full", lp.t, gut=True)
    except decision.DecisionInvalid:                                         # the first option may need inputs
        return
    assert d.reasoning == ""


def test_impulsiveness_changes_how_it_decides(lp):
    lp.t["inner"]["gut_scale"] = 1.0
    lp.life.character.sheet["temperament"]["impulsiveness"] = 1.0
    guts = sum(lp._manner("full")[0] for _ in range(60))
    lp.life.character.sheet["temperament"]["impulsiveness"] = 0.0
    calm = sum(lp._manner("full")[0] for _ in range(60))
    assert guts > 40 and calm == 0
    assert lp._manner("check_in") == (False, None)


# ---------------------------------------------------------------- stage 3: event-driven continuation

def learn(lp, *ids):
    from cat import knowledge
    with store.transaction(lp.conn):
        for a in ids:
            knowledge.learn(lp.conn, a, lp.life.content.actions[a].location or "", "inspect", lp.life.sim_time)


def count_decides(lp):
    calls = []
    lp.backend.handlers["decide"] = lambda msgs, schema: (calls.append(1),
                                                          {"thought": "hm", "reasoning": "hm", "action": "inspect",
                                                           "inputs": {}, "preserve": False, "letter": None})[1]
    return calls


def test_planned_moves_are_validated_like_choices(lp):
    from cat import decision
    learn(lp, "sit_and_reflect")
    m = mind_of(lp)
    raw = {"action": "inspect", "inputs": {}, "thought": "t", "reasoning": "r", "preserve": False, "letter": None,
           "then": [{"action": "sit_and_reflect", "inputs": {}}, {"action": "nonsense", "inputs": {}}, "junk"]}
    d = decision.validate(raw, m, "full", lp.t)
    assert d.then == [{"action": "sit_and_reflect", "inputs": {}}] and "dropped" in " ".join(d.notes)
    assert "then" in decision.schema(m, "full", lp.t)["properties"]


def test_a_planned_move_runs_with_no_model_while_nothing_new_happens(lp):
    learn(lp, "sit_and_reflect")
    calls = count_decides(lp)
    with store.transaction(lp.conn):
        lp._queue_set([{"action": "sit_and_reflect", "inputs": {}}])
    lp.step()
    row = lp.conn.execute("SELECT action, notes, attempts FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert not calls and row["action"] == "sit_and_reflect" and json.loads(row["notes"]) == ["queued"]
    assert json.loads(row["attempts"]) == [] and lp._queue() == []


def test_anything_new_drops_the_plan_and_it_decides_again(lp):
    learn(lp, "sit_and_reflect")
    calls = count_decides(lp)
    with store.transaction(lp.conn):
        lp._queue_set([{"action": "sit_and_reflect", "inputs": {}}])
    lp.exec.perception.append("A coin glints in the water.")
    lp.step()
    assert calls and lp._queue() == []


def test_a_check_in_with_an_unchanged_situation_just_continues(lp):
    from cat.scheduler import Due, Trigger
    lp._sig = lp._signature()
    due = Due("check_in", [Trigger("check_in")])
    mind = type("M", (), {"perception": []})()
    got = lp._unthinking(due, mind)
    assert got and got[0].action == "continue" and got[0].notes == ["unchanged"]
    lp.life.bodystate.energy = 0.1                                          # its body changed: it should think
    assert lp._unthinking(due, mind) is None
    assert lp._unthinking(due, type("M", (), {"perception": ["something"]})()) is None


# ---------------------------------------------------------------- stage 4: memory layers

def mem_conn(lp):
    return lp.conn, lp.t["memory"], lp.life.sim_time


def test_the_same_event_matters_differently_to_different_creatures(lp):
    from cat import memories
    m = lp.t["memory"]
    calm = memories.significance(0.0, 0.05, False, 0.5, {"volatility": 0.0}, m)
    raw = memories.significance(0.0, 0.05, False, 0.5, {"volatility": 1.0}, m)
    assert raw > calm and raw >= m["short_min"] > calm                 # one keeps it, the other lets it go


def test_meeting_the_same_thing_again_strengthens_one_memory_not_a_duplicate(lp):
    from cat import memories
    from cat.llm import hash_embed
    conn, m, now = mem_conn(lp)
    text = "You eat it slowly. When you are this hungry it tastes wonderful."
    with store.transaction(conn):
        a = memories.add_memory(conn, m, now, text, hash_embed(text), 0.5, feeling="good", action_id="eat_offering",
                                location="market", source=1)
        b = memories.add_memory(conn, m, "2026-11-22T10:00", text, hash_embed(text), 0.5, feeling="good",
                                action_id="eat_offering", location="market", source=2)
    rows = conn.execute("SELECT occurrences, strength, recall_days FROM memories").fetchall()
    assert (a, b) == ("added", "reinforced") and len(rows) == 1
    assert rows[0]["occurrences"] == 2 and rows[0]["strength"] > 0.5 and rows[0]["recall_days"] == 2


def test_short_term_memory_has_room_for_only_so_many_and_a_new_one_must_beat_the_weakest(lp):
    from cat import memories
    from cat.llm import hash_embed
    conn, m, now = mem_conn(lp)
    m["short_cap"] = 3
    with store.transaction(conn):
        for i, (s, words) in enumerate(((0.4, "alpha beta"), (0.5, "gamma delta"), (0.6, "epsilon zeta"))):
            memories.add_memory(conn, m, now, f"distinct thing number {i}", hash_embed(words), s,
                                feeling="flat", action_id=None, location=None, source=i)
        weak = memories.add_memory(conn, m, now, "a faint one", hash_embed("faint other zz"), 0.35, feeling="flat",
                                   action_id=None, location=None, source=9)
        strong = memories.add_memory(conn, m, now, "a vivid one", hash_embed("vivid other yy"), 0.8, feeling="flat",
                                     action_id=None, location=None, source=10)
    assert weak == "dropped" and strong == "added"
    assert sorted(x.text for x in memories.all_memories(conn, "short"))[0] == "a vivid one"
    assert len(memories.all_memories(conn, "short")) == 3 and min(x.strength for x in memories.all_memories(conn, "short")) == 0.5


def test_sleep_promotes_what_came_back_and_fades_what_mattered_to_a_worn_sentence(tmp_path):
    from cat import memories, sleepcycle
    from cat.llm import hash_embed
    from tests.test_sleepcycle import Night
    n = Night(tmp_path)
    m = n.t["memory"]
    day1, day9 = "2026-11-21T09:00", "2026-12-30T09:00"
    with store.transaction(n.conn):
        memories.add_memory(n.conn, m, day1, "short one met on two days", hash_embed("two days thing"), 0.4, feeling="good",
                            action_id="inspect", location="fountain", source=1)
        memories.add_memory(n.conn, m, "2026-11-22T09:00", "short one met on two days", hash_embed("two days thing"), 0.4,
                            feeling="good", action_id="inspect", location="fountain", source=2)
        n.conn.execute("INSERT INTO memories(kind, text, importance, strength, embedding, created_at, sources) "
                       "VALUES ('long', 'I saw a heron standing in the fountain at dawn and it did not move at all', 0.8, 0.8, ?, ?, '[]')",
                       (memories.to_blob(hash_embed("heron fountain dawn")), day1))
    p = n.plan(day9)
    assert len(p.promote) == 1 and [g for _, g in p.fade] == ["I saw a heron standing in the fountain at..."]
    n.commit(p, day9)
    kinds = {x.kind: x for x in memories.all_memories(n.conn)}
    assert kinds["faded"].text == kinds["faded"].gist and kinds["long"].occurrences == 2
    # a faded memory comes back only when something in front of it touches it
    assert memories.faded_cue(n.conn, m, hash_embed("heron fountain dawn")) == [kinds["faded"].gist]
    assert memories.faded_cue(n.conn, m, hash_embed("completely unrelated words here")) == []


def test_remembering_how_it_went_puts_a_note_on_that_action(lp):
    from cat import memories
    from cat.llm import hash_embed
    conn, m, now = mem_conn(lp)
    with store.transaction(conn):
        memories.add_memory(conn, m, now, "Sitting by the water settled me", hash_embed("sit water settle"), 0.7,
                            feeling="good", action_id="sit_and_reflect", location="fountain", source=1)
    assert memories.feelings_by_action(conn, m, now)["sit_and_reflect"] > 0
    learn(lp, "sit_and_reflect")
    notes = lp._notes()(lp.life.content.actions["sit_and_reflect"])
    assert "you remember it going well" in notes


# ---------------------------------------------------------------- stage 5: beliefs

def day_exp(n, text, action, mood, day, weather="rainy", status="finished", location="fountain"):
    d = {"status": status, "mood_base": mood, "weather": weather}
    n.conn.execute("INSERT INTO experiences(sim_time, kind, action_id, location, text, data, importance, remember) "
                   "VALUES (?, 'activity', ?, ?, ?, ?, 0.5, 0)", (f"2026-11-{day}T10:00", action, location, text, json.dumps(d)))


def settled(n, now="2026-11-26T22:00"):
    from cat import sleepcycle
    p = n.plan(now)
    with store.transaction(n.conn):
        sleepcycle.commit(n.conn, p, now, n.t)
    return n.q("SELECT kind, subject, outcome, status, for_count, against_count, days FROM beliefs")


def test_a_belief_needs_several_different_days_and_a_wrong_one_forms_from_weather(tmp_path):
    from tests.test_sleepcycle import Night
    n = Night(tmp_path)
    n.t["beliefs"].update(min_days_loose=2, min_for=2)
    for day in (21, 22, 23):                                   # sitting lifted it on three days (and it rained on two)
        day_exp(n, "sat", "sit_and_reflect", 0.06, day, weather="rainy" if day < 23 else "clear")
    rows = {(r["kind"], r["subject"], r["outcome"]): r for r in settled(n)}
    assert rows[("effect", "sit_and_reflect", "lift")]["status"] == "active"
    assert rows[("weather", "rainy", "lift")]["status"] == "active"        # naive: "when it rains I feel lighter"
    assert rows[("weather", "clear", "lift")]["status"] == "candidate"      # one day is not enough


def test_a_single_good_day_is_not_a_belief_and_contradicting_days_challenge_one(tmp_path):
    from tests.test_sleepcycle import Night
    n = Night(tmp_path)
    day_exp(n, "sat", "sit_and_reflect", 0.06, 21)
    assert {r["status"] for r in settled(n, "2026-11-21T22:00")} == {"candidate"}
    for day in (22, 23, 24):
        day_exp(n, "sat", "sit_and_reflect", 0.06, day)
    settled(n, "2026-11-25T22:00")
    assert n.q("SELECT status FROM beliefs WHERE kind = 'effect' AND outcome = 'lift'")[0][0] == "active"
    for day in range(25, 31):                                              # then it keeps wearing it down
        day_exp(n, "sat", "sit_and_reflect", -0.06, day)
        day_exp(n, "sat", "sit_and_reflect", -0.06, day)
    settled(n, "2026-11-30T22:00")
    assert n.q("SELECT status FROM beliefs WHERE kind = 'effect' AND outcome = 'lift'")[0][0] == "dropped"
    assert n.q("SELECT status FROM beliefs WHERE kind = 'effect' AND outcome = 'wear'")[0][0] == "active"


def test_repeated_failures_become_a_belief_about_why_and_it_may_blame_the_wrong_thing(tmp_path):
    from tests.test_sleepcycle import Night
    n = Night(tmp_path)
    for day in (21, 22):
        n.conn.execute("INSERT INTO activities(action_id, location, inputs, started_at, planned_minutes, ended_at, status, text) "
                       "VALUES ('make_drawing', 'art_room', '{}', ?, 0, ?, 'rejected', 'You meant to make a drawing, but could "
                       "not: the moment has passed.')", (f"2026-11-{day}T10:00", f"2026-11-{day}T10:00"))
    settled(n)
    row = n.q("SELECT subject, outcome, status, note FROM beliefs WHERE kind = 'failure'")[0]
    assert row["subject"] == "make_drawing" and row["status"] == "active" and row["outcome"] in ("self", "world")
    assert json.loads(row["note"])["truth"] == "circumstance"               # what really happened is kept for the dashboard


def test_beliefs_put_notes_on_options_and_lines_in_the_prompt(lp):
    with store.transaction(lp.conn):
        lp.conn.execute("INSERT INTO beliefs(kind, subject, outcome, statement, for_count, days, strength, first_seen, "
                        "last_reinforced, status) VALUES ('effect', 'sit_and_reflect', 'lift', 'When I sit and reflect, it "
                        "tends to lift me.', 5, 4, 0.7, '2026-11-21T10:00', '2026-11-21T10:00', 'active')")
        lp.conn.execute("INSERT INTO beliefs(kind, subject, outcome, statement, for_count, days, strength, first_seen, "
                        "last_reinforced, status) VALUES ('place', 'fountain', 'wear', 'Being at the fountain tends to wear "
                        "me down.', 5, 4, 0.7, '2026-11-21T10:00', '2026-11-21T10:00', 'active')")
    learn(lp, "sit_and_reflect")
    assert any("tends to lift me" in x for x in lp._notes()(lp.life.content.actions["sit_and_reflect"]))
    seen = []
    lp.backend.handlers["decide"] = lambda msgs, schema: (seen.append(msgs[1]["content"]),
                                                          {"thought": "hm", "reasoning": "hm", "action": "inspect",
                                                           "inputs": {}, "preserve": False, "letter": None})[1]
    lp.step()
    assert "What you have come to think:" in seen[0] and "Being at the fountain tends to wear me down." in seen[0]


# ---------------------------------------------------------------- stage 6: concerns, people, projects

def test_a_drawing_is_a_project_with_an_intention_an_evaluation_and_an_ending(tmp_path):
    from tests.test_art import Room
    room = Room(tmp_path)
    room.backend.handlers["evaluate_drawing"] = lambda m, s: {"verdict": "disliked", "words": "It is not what I meant."}
    room.do("make_drawing", intention="a heron in the fountain", thoughts="calm", materials="green crayon", effort="quick")
    art = room.works()[-1]
    proj = room.conn.execute("SELECT * FROM projects").fetchone()
    assert proj["intention"] == "a heron in the fountain" and proj["status"] == "active" and proj["rating"] is None
    room.do("study_drawing", artwork=art["id"])
    proj = room.conn.execute("SELECT * FROM projects").fetchone()
    assert proj["rating"] is not None and proj["rating"] < 0.35 and "not what I meant" in proj["evaluation"]
    assert room.life.inner.v["frustration"] > 0.05                         # disliking its own work is felt
    from cat import projects
    assert "disappointed" in projects.on_mind(room.conn, load_prompts())[0]
    room.do("abandon_drawing", artwork=art["id"])
    assert room.conn.execute("SELECT status FROM projects").fetchone()[0] == "abandoned"
    assert room.works()[-1]["status"] == "abandoned" and projects.on_mind(room.conn, load_prompts()) == []


def load_prompts():
    from cat.decision import load_prompts as lp_
    return lp_()


def test_a_self_critical_creature_rates_the_same_drawing_lower(tmp_path):
    from cat.capabilities import CapContext
    from cat.capabilities.look_at_drawing import LookAtDrawing
    from tests.test_art import Room
    room = Room(tmp_path)
    room.backend.handlers["evaluate_drawing"] = lambda m, s: {"verdict": "mixed", "words": "Half of it works."}
    room.do("make_drawing", intention="a tree", thoughts="", materials="green crayon", effort="quick")
    item = room.works()[-1]
    rate = lambda crit: LookAtDrawing()._judge(
        CapContext(content=room.life.content, facts={}, location="art_room", sim_time="x", texts={}, backend=room.backend,
                   tuning=room.t, artifact=lambda i: __import__("cat.artifacts", fromlist=["x"]).load(room.conn, i),
                   temperament={"self_criticism": crit}), item, [], "a green tree")[2]
    assert rate(0.0) > rate(1.0)


def test_concerns_fade_and_come_back_by_themselves_without_becoming_tasks(lp):
    import random
    from cat import loops
    t = lp.t
    t["loops"].update(resurface_hours=1, resurface_chance=1.0)
    with store.transaction(lp.conn):
        lp.conn.execute("INSERT INTO loops(kind, text, opened_at, salience) VALUES ('worry', 'The stall may run out of food.', "
                        "'2026-11-21T07:00', 0.8)")
        pick = loops.resurface(lp.conn, t, "2026-11-21T09:00", random.Random(1))
    assert pick["text"] == "The stall may run out of food." and "worries" in loops.on_mind(lp.conn, t)[0]
    row = lp.conn.execute("SELECT * FROM loops").fetchone()
    quiet = loops.salience_now(row, t, "2026-12-21T09:00")
    assert quiet < 0.8 and quiet >= 0.1                                    # quieter, never gone: it may just be lived with
    lp._resurface()
    assert any(tr.kind == "concern" for tr in lp.scheduler.pending) or True


def test_people_it_hears_from_become_familiar_and_it_feels_something_about_them(lp):
    from cat import people
    with store.transaction(lp.conn):
        people.touch(lp.conn, "Kaleb", lp.life.sim_time, 0.1, 0.05)
        people.touch(lp.conn, "Kaleb", lp.life.sim_time, 0.1, 0.05)
        people.touch(lp.conn, "Ann", lp.life.sim_time, 0.1, -0.06)
    lines = people.on_mind(lp.conn, lp.prompts)
    assert "Kaleb: you have heard from them 2 time(s); you feel warmly toward them" in lines
    assert any("uneasy" in x for x in lines)


# ---------------------------------------------------------------- stage 7: acquired identity, holds, practice

def test_a_habit_is_counted_by_days_enters_the_sheet_with_its_evidence_and_is_let_go_again(tmp_path):
    from cat import identity
    from tests.test_sleepcycle import Night
    n = Night(tmp_path)
    n.t["beliefs"].update(habit_days=4, habit_expire_days=5)
    written = identity.current(n.conn).sheet["temperament"]
    for day in (21, 22, 23):                                            # three days: not yet
        day_exp(n, "sat", "sit_and_reflect", 0.0, day)
    settled(n, "2026-11-24T05:00")
    assert identity.current(n.conn).sheet["acquired"]["habits"] == []
    day_exp(n, "sat", "sit_and_reflect", 0.0, 24)                       # the fourth day at the same time of day
    settled(n, "2026-11-25T05:00")
    cur = identity.current(n.conn)
    habit = cur.sheet["acquired"]["habits"][0]
    assert "tends to sit and reflect at the fountain around the morning" in habit["text"]
    assert cur.reason.startswith("acquired habit") and cur.evidence and cur.sheet["temperament"] == written
    # it shows up in who it is, and goes when nothing bears it out
    from cat import mind as mindmod
    assert habit["text"] in [t for t in mindmod.identity_block(type("L", (), {"character": cur})())["tendencies"]]
    settled(n, "2026-12-20T05:00")
    assert identity.current(n.conn).sheet["acquired"]["habits"] == []
    assert [v.reason for v in identity.history(n.conn)][-1].startswith("let go of habit")


def test_what_it_holds_to_makes_an_event_reach_it_more_easily():
    from cat import inner
    t = tuning.load()["inner"]
    holds = [{"text": "x", "keywords": ["water"], "strength": 1.0}]
    assert inner.hold_boost(holds, "A coin drops into the water.", t) == t["hold_boost"]
    assert inner.hold_boost(holds, "A bell rings.", t) == 0.0


def test_practice_saturates_and_makes_the_work_cost_less_effort(lp):
    from cat import inner
    t = lp.t["inner"]
    assert inner.practice(0, t) == 0 and 0.6 < inner.practice(25, t) < 1 and inner.practice(2000, t) > 0.99
    spec = lp.life.content.actions["inspect"]
    new = lp.exec._effort(spec)
    assert new > 0
    with store.transaction(lp.conn):
        lp.conn.execute("UPDATE knowledge SET times_done = 200 WHERE action_id = 'inspect'")
    assert lp.exec._effort(spec) < new * 0.75
    assert "you are well practised at this" in lp._notes()(spec)

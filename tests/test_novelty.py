"""Being drawn to the same subject (docs/rewrite-contracts.md §6.6): recall that does not feed on itself, subjects
compared across different wording, and things learned from reading coming back to mind."""
import json

import pytest

from cat import behaviour, content, memories, store, tuning
from cat.decision import Decision
from cat.llm import MockBackend, hash_embed
from cat.loop import FakeClock, Loop


@pytest.fixture
def lp(tmp_path):
    t = tuning.load()
    loop = Loop(store.connect(tmp_path / "nit.db"), content.load(), t, MockBackend(seed=1), clock=FakeClock(), seed=1)
    loop.life.content.events.clear()
    return loop


def add_memory(conn, text, importance=0.6, created="2026-11-21T07:00", recalled=None):
    with store.transaction(conn):
        cur = conn.execute("INSERT INTO memories(kind, text, importance, embedding, created_at, last_recalled) "
                           "VALUES ('long', ?, ?, ?, ?, ?)",
                           (text, importance, memories.to_blob(hash_embed(text)), created, recalled))
    return cur.lastrowid


# ---------------------------------------------------------------- recall


def test_recall_does_not_keep_the_same_memories_on_its_mind(lp):
    t = lp.t
    t["memory"].update(recall_k=2, wander_slots=0)
    for i in range(6):
        add_memory(lp.conn, f"the moving water in the fountain {i}", importance=0.5 + i * 0.05)
    q = hash_embed("the moving water in the fountain")
    now = "2026-11-21T10:00"
    first = [r["id"] for r in memories.recall(lp.conn, t, q, now)]
    with store.transaction(lp.conn):
        memories.mark_recalled(lp.conn, first, now)
    second = [r["id"] for r in memories.recall(lp.conn, t, q, "2026-11-21T10:06")]
    assert not set(first) & set(second)                       # the ones just thought of rest; others get a turn
    third = [r["id"] for r in memories.recall(lp.conn, t, q, "2026-11-21T12:00")]
    assert set(third) & set(first)                            # after resting they can come back


def test_being_recalled_does_not_make_a_memory_recent(lp):
    old = add_memory(lp.conn, "an old thing", created="2026-11-01T07:00", recalled="2026-11-21T08:00")
    m = memories.all_memories(lp.conn, "long")[0]
    s = memories.score(m, None, "2026-11-21T10:00", lp.t["memory"])
    assert m.id == old and s["recency"] < 0.01


def test_a_wandering_slot_brings_back_something_not_thought_of_for_a_while(lp):
    t = lp.t
    t["memory"].update(recall_k=2, wander_slots=1, wander_after_minutes=360)
    for i in range(3):
        add_memory(lp.conn, f"the moving water in the fountain {i}", created="2026-11-21T09:00")
    idle = add_memory(lp.conn, "the first time I ate bread at the stall", importance=0.9, created="2026-11-20T07:00")
    got = memories.recall(lp.conn, t, hash_embed("the moving water in the fountain"), "2026-11-21T10:00")
    assert len(got) == 2 and got[-1]["id"] == idle and got[-1]["wandered"]


# ---------------------------------------------------------------- subjects


def test_subjects_are_the_creature_s_own_words():
    c = content.load()
    assert behaviour.subject_text(c.actions["make_drawing"], {"intention": "  the moving\nwater "}) == "the moving water"
    assert behaviour.subject_text(c.actions["work_on_drawing"], {"intention": "x"}) is None   # returning is not repeating
    assert behaviour.subject_text(c.actions["move"], {"to": "market"}) is None


def record(lp, text, minutes_ago_from, aid="make_drawing"):
    with store.transaction(lp.conn):
        lp.conn.execute("INSERT INTO subjects(activity_id, action_id, sim_time, text, embedding) VALUES (NULL, ?, ?, ?, ?)",
                        (aid, minutes_ago_from, text, memories.to_blob(hash_embed(text))))


def test_the_same_subject_in_new_words_is_less_fresh(lp):
    for when in ("2026-11-21T08:00", "2026-11-21T09:00", "2026-11-21T09:30"):
        record(lp, "the moving water in the fountain", when)
    now = "2026-11-21T10:00"
    same = behaviour.echoes(lp.conn, hash_embed("the water moving in the old fountain"), now, lp.t)
    other = behaviour.echoes(lp.conn, hash_embed("a tall tree with red apples"), now, lp.t)
    assert len(same) == 3 and other == []
    assert behaviour.subject_freshness(same, lp.t) < 0.5 < behaviour.subject_freshness(other, lp.t) == 1.0
    later = behaviour.echoes(lp.conn, hash_embed("the water moving in the old fountain"), "2026-11-24T10:00", lp.t)
    assert later == []                                          # long ago, it is fresh again
    assert behaviour.dwelling(lp.conn, now, lp.t) == ["the moving water in the fountain"]


def test_the_guard_quotes_its_own_earlier_words_and_asks_once(lp):
    for when in ("2026-11-21T06:30", "2026-11-21T06:40", "2026-11-21T06:50"):
        record(lp, "the moving water in the fountain", when)
    d = Decision(kind="full", action="make_drawing",
                 inputs={"intention": "the water moving in the old fountain", "thoughts": "", "materials": "",
                         "effort": "quick"})
    mind = type("M", (), {"options": [], "option": lambda self, x: None})()
    problems = lp._reconsider_stale_choice(mind, d)
    assert len(problems) == 1 and '"the moving water in the fountain"' in problems[0] and "3 times" in problems[0]
    fresh = Decision(kind="full", action="make_drawing",
                     inputs={"intention": "a tall tree with red apples", "thoughts": "", "materials": "", "effort": "quick"})
    assert lp._reconsider_stale_choice(mind, fresh) == []


def test_a_repeated_subject_feels_flatter(lp):
    for when in ("2026-11-21T06:30", "2026-11-21T06:40", "2026-11-21T06:50"):
        record(lp, "the moving water in the fountain", when)
    with store.transaction(lp.conn):
        cur = lp.conn.execute("INSERT INTO activities(action_id, location, inputs, started_at, planned_minutes, "
                              "status, freshness) VALUES ('make_drawing', 'art_room', '{}', ?, 10, 'running', 1.0)",
                              (lp.life.sim_time,))
    aid = cur.lastrowid
    lp._record_subject(Decision(kind="full", action="make_drawing",
                                inputs={"intention": "the water moving in the old fountain"}), aid)
    row = lp.conn.execute("SELECT freshness, repeat_parts FROM activities WHERE id = ?", (aid,)).fetchone()
    assert row["freshness"] < 0.5
    assert json.loads(row["repeat_parts"])["subject"]["close_to"][0] == "the moving water in the fountain"
    assert lp.conn.execute("SELECT COUNT(*) FROM subjects WHERE activity_id = ?", (aid,)).fetchone()[0] == 1


def test_the_prompt_says_when_it_keeps_coming_back(lp):
    for when in ("2026-11-21T06:30", "2026-11-21T06:40", "2026-11-21T06:50"):
        record(lp, "the moving water in the fountain", when)
    seen = []
    lp.backend.handlers["decide"] = lambda msgs, schema: (seen.append(msgs[1]["content"]),
                                                          {"thought": "hm", "reasoning": "hm", "action": "inspect",
                                                           "inputs": {}, "preserve": False, "letter": None})[1]
    lp.step()
    assert 'keep coming back to the same thing' in seen[0] and '"the moving water in the fountain"' in seen[0]


# ---------------------------------------------------------------- learning comes back


def test_kept_reading_comes_back_to_mind(lp):
    from cat import mind as mindmod
    arts = [{"id": str(i), "title": f"Page {i}", "status": "kept", "summary": f"what I learned {i}",
             "meaning": "it matters", "reviewed_at": f"2026-11-2{i}T08:00"} for i in (0, 1)]
    arts += [{"id": "9", "title": "Discarded", "status": "discarded", "reviewed_at": "2026-11-21T08:00"},
             {"id": "2", "title": "Unread yet", "status": "read", "read_at": "2026-11-21T08:00"}]
    lp.life.world.values["terminal.articles"] = arts
    got = mindmod.learned_on_mind(lp.life, lp.t)
    assert got[0].startswith("Page 1: what I learned 1 (what it means to you: it matters)")
    assert len(got) == 2 and got[1].startswith("Page 0")
    lp.life.sim_time = "2026-12-30T08:00"
    assert mindmod.learned_on_mind(lp.life, lp.t) == []          # long ago: it has faded from the slot


def test_the_guard_says_it_was_already_wished_and_asks_again(lp):
    lp.life.world.values["fountain.wishes"] = [{"text": "I wish I could fly", "made": "x", "status": "open"}]
    mind = type("M", (), {"options": [], "option": lambda self, x: None})()
    d = Decision(kind="full", action="make_wish", inputs={"kind": "I wish I could", "what": "fly"})
    problems = lp._reconsider_stale_choice(mind, d)
    assert len(problems) == 1 and "already wished" in problems[0]


# ---------------------------------------------------------------- the same thought twice (§6.4)


def test_the_guard_quotes_a_thought_that_copies_a_recent_one(lp):
    said = "The quiet feels real now. I want to keep this moment."
    with store.transaction(lp.conn):
        lp.conn.execute("INSERT INTO thoughts(decision_id, sim_time, text, embedding, repeated) VALUES (NULL, ?, ?, ?, 0)",
                        (lp.life.sim_time, said, memories.to_blob(hash_embed(said))))
    mind = type("M", (), {"options": [], "option": lambda self, x: None})()
    again = Decision(kind="full", action="inspect", inputs={}, thought=said)
    problems = lp._reconsider_stale_choice(mind, again)
    assert len(problems) == 1 and said in problems[0] and "different" in problems[0]
    new = Decision(kind="full", action="inspect", inputs={}, thought="A bird sat on the stall roof for a while.")
    assert lp._reconsider_stale_choice(mind, new) == []


def test_a_guarded_but_valid_choice_beats_a_fallback(lp):
    calls = []

    def handler(msgs, schema):
        calls.append(1)
        if len(calls) == 1:
            return {"thought": "same old thought", "reasoning": "r", "action": "inspect", "inputs": {}, "preserve": False,
                    "letter": None}
        return {"thought": "x", "reasoning": "r", "action": "no_such_action", "inputs": {}, "preserve": False, "letter": None}
    lp.backend.handlers["decide"] = handler
    with store.transaction(lp.conn):
        lp.conn.execute("INSERT INTO thoughts(decision_id, sim_time, text, embedding, repeated) VALUES (NULL, ?, ?, ?, 0)",
                        (lp.life.sim_time, "same old thought", memories.to_blob(hash_embed("same old thought"))))
    lp.step()
    row = lp.conn.execute("SELECT action, fallback FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["fallback"] == 0 and row["action"] == "inspect"


def add_thought(lp, text, vec=None):
    with store.transaction(lp.conn):
        lp.conn.execute("INSERT INTO thoughts(decision_id, sim_time, text, embedding, repeated) VALUES (NULL, ?, ?, ?, 0)",
                        (lp.life.sim_time, text, memories.to_blob(vec if vec is not None else hash_embed(text))))


def test_the_same_theme_in_new_words_is_caught_and_shown_as_mulling(lp):
    t = lp.t["repetition"]
    t.update(thought_theme_threshold=0.25, thought_dup_threshold=0.99, thought_theme_min=3)
    for w in ("the quiet feels real now here", "the quiet feels deep now here", "the quiet is real here now"):
        add_thought(lp, w)
    mind = type("M", (), {"options": [], "option": lambda self, x: None})()
    again = Decision(kind="full", action="inspect", inputs={}, thought="the quiet feels real in this place")
    problems = lp._reconsider_stale_choice(mind, again)
    assert len(problems) == 1 and "3 thoughts like this" in problems[0] and "think about something else" in problems[0]
    other = Decision(kind="full", action="inspect", inputs={}, thought="a bird landed on the stall roof today")
    assert lp._reconsider_stale_choice(mind, other) == []
    add_thought(lp, "the quiet feels real in this place")
    assert behaviour.mulling(lp.conn, lp.t) == ["the quiet feels real in this place"]
    seen = []
    lp.backend.handlers["decide"] = lambda msgs, schema: (seen.append(msgs[1]["content"]),
                                                          {"thought": "hm", "reasoning": "hm", "action": "inspect",
                                                           "inputs": {}, "preserve": False, "letter": None})[1]
    lp.step()
    assert "Lately your thoughts keep going the same way" in seen[0]

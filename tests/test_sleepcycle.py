"""The sleep cycle (docs/rewrite-contracts.md §5.2-§5.6): each stage with the mock, plan/commit all-or-nothing,
character development checks, preferences, maintenance, and the clock waiting for the night."""
import json

import pytest

from cat import content, identity, knowledge, life, memories, sleepcycle, store, tuning
from cat.llm import MockBackend
from cat.sleepcycle import check_proposal


class Night:
    def __init__(self, tmp_path):
        self.t = tuning.load()
        self.conn = store.connect(tmp_path / "nit.db")
        self.c = content.load()
        self.life = life.open_life(self.conn, self.c, self.t)
        self.b = MockBackend()
        self.p = sleepcycle.load_prompts()

    def exp(self, kind="activity", text="x", action="sit_and_reflect", when="2026-11-21T09:00", location="fountain",
            thought=None, mood=0.05, **data):
        d = {"status": "finished", "mood_base": mood, **data}
        cur = self.conn.execute(
            "INSERT INTO experiences(sim_time, kind, action_id, location, text, data, thought, importance, remember) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, 0)", (when, kind, action, location, text, json.dumps(d), thought))
        return cur.lastrowid

    def plan(self, now="2026-11-21T22:00"):
        return sleepcycle.plan(self.conn, self.c, self.t, self.b, self.p, identity.current(self.conn).sheet, now)

    def commit(self, plan, now="2026-11-22T05:00"):
        with store.transaction(self.conn):
            return sleepcycle.commit(self.conn, plan, now)

    def q(self, sql, *a):
        return self.conn.execute(sql, a).fetchall()


@pytest.fixture
def n(tmp_path):
    return Night(tmp_path)


def test_a_night_with_the_default_mock(n):
    for i in range(6):
        n.exp(text=f"I sat at the rim {i}.")
    p = n.plan()
    assert p.stages["consolidate"]["status"] == "ok" and p.summary
    assert p.stages["long_term"]["status"] == "ok" and p.long_term
    run = n.commit(p)
    assert n.q("SELECT status FROM sleep_runs WHERE id = ?", run)[0][0] == "ok"
    assert n.q("SELECT COUNT(*) FROM memories WHERE kind = 'long'")[0][0] >= 1
    assert all(not k.provisional for k in knowledge.all_known(n.conn).values())
    assert n.q("SELECT value FROM meta WHERE key = ?", sleepcycle.CURSOR)[0][0] == str(n.q("SELECT MAX(id) FROM experiences")[0][0])


def test_citations_are_checked_against_real_experiences(n):
    a, b = n.exp(text="I made a wish."), n.exp(text="I sat.")
    n.b.handlers["sleep_consolidate"] = lambda m, s: {"summary": "A day.", "mattered": [
        {"id": a, "why": "wishing"}, {"id": 999, "why": "invented"}, {"id": a, "why": "again"}]}
    n.b.handlers["sleep_long_term"] = lambda m, s: {"memories": [
        {"text": "I made my first wish.", "importance": 3, "sources": [a, 777]},
        {"text": "Something that never happened.", "importance": 0.9, "sources": [555]}]}
    p = n.plan()
    assert p.mattered == [{"id": a, "why": "wishing"}]
    assert [(m["text"], m["importance"], m["sources"]) for m in p.long_term] == [("I made my first wish.", 1.0, [a])]


def test_a_failed_consolidation_keeps_the_day_for_the_next_night(n):
    n.exp()
    n.b.handlers["sleep_consolidate"] = lambda m, s: {"summary": "", "mattered": []}     # unusable
    p = n.plan()
    assert p.stages["consolidate"]["status"] == "failed" and p.stages["long_term"]["status"] == "failed"
    assert p.cursor is None
    n.commit(p)
    assert n.q("SELECT status FROM sleep_runs")[0][0] == "failed"
    assert not n.q("SELECT 1 FROM meta WHERE key = ?", sleepcycle.CURSOR)
    assert all(not k.provisional for k in knowledge.all_known(n.conn).values())   # learned still confirmed
    del n.b.handlers["sleep_consolidate"]
    assert n.plan().cursor is not None


def test_core_memories_come_only_from_kept_thoughts(n):
    keep = n.exp(kind="thought", text="The water listens.", thought="The water listens.", preserve=True)
    other = n.exp(text="I sat.")
    n.b.handlers["sleep_core"] = lambda m, s: {"keep": [{"id": other, "text": "not a kept thought"},
                                                         {"id": keep, "text": "The water listens to me."}]}
    p = n.plan()
    assert p.core == [{"text": "The water listens to me.", "sources": [keep]}]
    n.commit(p)
    assert memories.core_texts(n.conn) == ["The water listens to me."]


def test_commit_is_all_or_nothing(n, monkeypatch):
    for i in range(3):
        n.exp(text=f"moment {i}")
    p = n.plan()
    p.proposal, p.accepted = {"applied": {"kind": "add_trait", "target": "x", "value": 0.2}, "evidence": []}, True
    monkeypatch.setattr(identity, "commit_change", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        n.commit(p)
    assert n.q("SELECT COUNT(*) FROM memories")[0][0] == 0
    assert n.q("SELECT COUNT(*) FROM sleep_runs")[0][0] == 0
    assert any(k.provisional for k in knowledge.all_known(n.conn).values())


def test_preferences_from_mood_evidence(n):
    for i in range(4):
        n.exp(action="make_wish", mood=0.08, when=f"2026-11-21T0{i + 1}:00")
        n.exp(action="sit_and_reflect", mood=0.0, when=f"2026-11-21T0{i + 1}:30")
        n.exp(action="move", mood=0.0, when=f"2026-11-21T0{i + 1}:40")
    n.b.handlers["sleep_preferences"] = lambda m, s: {"verdicts": [
        {"subject": "p:action:make_wish", "keep": True, "words": "I like making wishes."},
        {"subject": "p:action:sit_and_reflect", "keep": False, "words": ""}]}
    p = n.plan()
    n.commit(p)
    prefs = memories.preferences(n.conn)
    assert prefs[("action", "make_wish")].valence > 0 and prefs[("action", "make_wish")].words == "I like making wishes."
    assert ("action", "sit_and_reflect") not in prefs                     # a new one it rejected
    assert ("action", "move") not in prefs and ("family", "move") not in prefs   # utilities are never judged
    v0 = prefs[("action", "make_wish")].valence
    n.conn.execute("DELETE FROM experiences")
    n.commit(n.plan("2026-11-28T22:00"))
    assert memories.preferences(n.conn)[("action", "make_wish")].valence < v0    # decays without evidence


def test_preferences_colour_mood_and_the_menu(n):
    with store.transaction(n.conn):
        n.conn.execute("INSERT INTO preferences VALUES ('action', 'make_wish', 0.8, 1.0, 'I like it', '[]', 'x')")
    prefs = memories.preferences(n.conn)
    wish = n.c.actions["make_wish"]
    assert memories.mood_factor(prefs, wish, "fountain", 0.5) == pytest.approx(1.4)
    assert memories.note_for(prefs, wish, 0.15) == 1
    assert memories.note_for(prefs, n.c.actions["sit_and_reflect"], 0.15) == 0


def own_days(n, k, days=3, action="sit_and_reflect", **kw):
    return [n.exp(action=action, when=f"2026-11-2{1 + i % days}T10:{i:02d}", **kw) for i in range(k)]


def check(n, proposal, now="2026-11-24T22:00"):
    return check_proposal(n.conn, n.t, n.b, n.p, identity.current(n.conn).sheet, proposal, now)


def test_development_accepts_a_small_well_evidenced_change(n):
    ev = own_days(n, 6)
    verdicts, ok = check(n, {"kind": "add_tendency", "target": "lingers by the water", "value": "0.9",
                             "rationale": "I keep sitting at the rim.", "evidence": ev})
    assert ok, verdicts
    p = sleepcycle.SleepPlan(started_at="x", proposal={"kind": "add_tendency", "evidence": ev, "rationale": "r",
                                                       "applied": {"kind": "add_tendency", "target": "lingers by the water",
                                                                   "value": n.t["sleep"]["dev_new_strength"]}},
                             verdicts=verdicts, accepted=True)
    n.commit(p)
    v = identity.current(n.conn)
    assert v.version == 2 and v.sheet["tendencies"] == {"lingers by the water": 0.2} and v.evidence == ev


@pytest.mark.parametrize("setup, proposal, failing", [
    (lambda n: own_days(n, 3), {"kind": "add_trait", "target": "patient"}, "evidence"),               # too few
    (lambda n: own_days(n, 6, days=1), {"kind": "add_trait", "target": "patient"}, "evidence"),       # one day only
    (lambda n: own_days(n, 6, action="read_messages", external=True), {"kind": "add_trait", "target": "musical"},
     "evidence"),                                                                                      # letters
    (lambda n: own_days(n, 6), {"kind": "add_trait", "target": "cruel"}, "conflict_words"),
    (lambda n: own_days(n, 6), {"kind": "adjust_trait", "target": "brave", "value": "0.9"}, "size"),
])
def test_development_rejects(n, setup, proposal, failing):
    ev = setup(n)
    verdicts, ok = check(n, {"value": "0.5", "rationale": "", "evidence": ev, **proposal})
    assert not ok
    assert not next(v for v in verdicts if v["check"] == failing)["ok"], verdicts


def test_development_adjustments_are_small_and_cool_down(n):
    ev = own_days(n, 6)
    proposal = {"kind": "adjust_trait", "target": "curious", "value": "0.2", "rationale": "", "evidence": ev}
    verdicts, ok = check(n, proposal)
    assert ok and proposal["applied"]["value"] == pytest.approx(0.75)                  # 0.8 - 0.05 at most
    with store.transaction(n.conn):
        identity.commit_change(n.conn, proposal["applied"], "sleep", ev, "2026-11-23T03:00")
    _, ok = check(n, {**proposal, "value": "0.1"})
    assert not ok                                                                       # cooldown


def test_the_conflict_check_can_veto(n):
    ev = own_days(n, 6)
    n.b.handlers["sleep_conflict"] = lambda m, s: {"conflict": True, "why": "it would stop being truthful"}
    verdicts, ok = check(n, {"kind": "add_tendency", "target": "tells tall tales", "value": "", "rationale": "",
                             "evidence": ev})
    assert not ok and verdicts[-1] == {"check": "conflict_check", "ok": False, "detail": "it would stop being truthful"}


def test_rejected_proposals_are_kept(n):
    ev = own_days(n, 2)
    n.b.handlers["sleep_develop"] = lambda m, s: {"change": True, "kind": "add_trait", "target": "patient",
                                                  "value": "", "rationale": "I wait a lot.", "evidence": ev}
    n.t["sleep"]["dev_min_evidence"] = 2
    n.t["sleep"]["dev_min_days"] = 3
    p = n.plan("2026-11-23T22:00")
    n.commit(p)
    row = n.q("SELECT accepted, version, proposal FROM dev_proposals")[0]
    assert row[0] == 0 and row[1] is None and json.loads(row[2])["target"] == "patient"
    assert identity.current(n.conn).version == 1


def test_maintenance_merges_duplicates_and_forgets_faded_memories(n):
    vec = memories.to_blob([1.0, 0.0, 0.0])
    with store.transaction(n.conn):
        for text, imp, when, blob in (("I wished.", 0.9, "2026-11-21T10:00", vec), ("I made a wish.", 0.5, "2026-11-21T11:00", vec),
                                      ("A dull moment.", 0.06, "2026-11-01T10:00", memories.to_blob([0.0, 1.0, 0.0])),
                                      ("A kept moment.", 0.06, "2026-11-01T10:00", memories.to_blob([0.0, 0.0, 1.0]))):
            n.conn.execute("INSERT INTO memories(kind, text, importance, embedding, created_at, sources) "
                           "VALUES ('long', ?, ?, ?, ?, ?)", (text, imp, blob, when, json.dumps([1] if 'kept' in text else [])))
        n.conn.execute("INSERT INTO character_versions(version, sheet, created_at, reason, evidence) "
                       "VALUES (9, '{}', 'x', 'protect', '[1]')")
    p = n.plan("2026-11-22T22:00")
    n.commit(p)
    left = [r[0] for r in n.q("SELECT text FROM memories ORDER BY id")]
    assert left == ["I wished.", "A kept moment."]


def test_recall_and_notable_moments(n):
    with store.transaction(n.conn):
        for text, vec in (("I made a wish at the fountain.", [1.0, 0.0]), ("I ate bread.", [0.0, 1.0])):
            n.conn.execute("INSERT INTO memories(kind, text, importance, embedding, created_at) VALUES ('long', ?, 0.5, ?, ?)",
                           (text, memories.to_blob(vec), "2026-11-21T08:00"))
        n.conn.execute("INSERT INTO experiences(sim_time, kind, location, text, importance, remember) VALUES "
                       "('2026-11-21T09:00', 'activity', 'fountain', 'I found the coins.', 0.2, 1), "
                       "('2026-11-21T09:10', 'event', 'fountain', 'A faint splash.', 0.1, 0)")
    n.t["memory"]["recall_k"] = 1
    assert memories.recall(n.conn, n.t, [0.9, 0.1], "2026-11-21T10:00")[0]["text"] == "I made a wish at the fountain."
    assert memories.recall(n.conn, n.t, [0.1, 0.9], "2026-11-21T10:00")[0]["text"] == "I ate bread."
    assert memories.notable_today(n.conn, n.t, "") == ["I found the coins."]


def test_the_clock_waits_for_the_night(tmp_path):
    from cat.loop import FakeClock, Loop, Sleeper
    t = tuning.load()
    fc = FakeClock()
    lp = Loop(store.connect(tmp_path / "nit.db"), content.load(), t, MockBackend(), clock=fc, seed=1)
    lp.life.content.events.clear()
    lp.life.sim_time = "2026-11-21T22:00"
    lp.life.bodystate.energy = 0.95
    from cat.decision import Decision
    with store.transaction(lp.conn):
        lp.exec.start(Decision(kind="full", action="sleep"), None)
    stuck = Sleeper(lp)
    stuck.start = lambda: None                                   # a sleep cycle that has not finished
    lp.sleeper = stuck
    for _ in range(int(10 * 60 / 1)):                            # 10 sim hours of steps
        fc.sleep(t["loop"]["step_seconds"])
        lp.step()
    assert lp.life.asleep and lp.life.sim_time == "2026-11-22T06:00"     # held at the waking moment
    stuck.plan = sleepcycle.SleepPlan(started_at="2026-11-21T22:00")
    stuck.done = True
    fc.sleep(t["loop"]["step_seconds"])
    lp.step()
    fc.sleep(t["loop"]["step_seconds"])
    lp.step()
    assert not lp.life.asleep and lp.conn.execute("SELECT COUNT(*) FROM sleep_runs").fetchone()[0] == 1


# ---------------------------------------------------------------- open loops (§5.7)


def test_sleep_opens_a_loop_from_its_own_experiences_and_the_mind_shows_it(n):
    from cat import loops
    e = n.exp(text="I saw a door in the art room.")
    n.b.handlers["sleep_loops"] = lambda m, s: {"close": [], "open": [
        {"kind": "question", "text": "What is behind the door?", "sources": [e]},
        {"kind": "want", "text": "Something invented.", "sources": [9999]}]}      # cites nothing real: dropped
    p = n.plan()
    assert [x["text"] for x in p.loops["open"]] == ["What is behind the door?"]
    n.commit(p)
    assert loops.on_mind(n.conn, n.t) == ["(something you wonder) What is behind the door?"]


def test_a_loop_closes_only_on_real_evidence_and_never_opens_twice(n):
    from cat import loops
    e1 = n.exp(text="I saw a door.")
    n.b.handlers["sleep_loops"] = lambda m, s: {"close": [], "open": [
        {"kind": "question", "text": "What is behind the door?", "sources": [e1]}]}
    n.commit(n.plan())
    lid = loops.open_rows(n.conn)[0]["id"]
    e2 = n.exp(text="I opened the door.", when="2026-11-22T10:00")
    n.b.handlers["sleep_loops"] = lambda m, s: {"close": [{"id": lid, "why": "opened it", "sources": [123456]}], "open": [
        {"kind": "question", "text": "What is behind the door?", "sources": [e2]}]}
    p = n.plan("2026-11-22T22:00")
    assert p.loops["close"] == [] and p.loops["open"] == []          # no real evidence; same loop is not opened twice
    n.b.handlers["sleep_loops"] = lambda m, s: {"close": [{"id": lid, "why": "opened it", "sources": [e2]}], "open": []}
    n.commit(n.plan("2026-11-22T22:00"))
    row = n.q("SELECT status, why_closed FROM loops WHERE id = ?", lid)[0]
    assert (row["status"], row["why_closed"]) == ("closed", "opened it") and loops.on_mind(n.conn, n.t) == []


def test_loops_fade_and_a_full_mind_pushes_the_oldest_out(n):
    from cat import loops
    n.t["loops"].update(max_open=2, per_night=2, expire_days=3)
    for i, when in enumerate(("2026-11-10T08:00", "2026-11-20T08:00", "2026-11-20T09:00")):
        n.conn.execute("INSERT INTO loops(kind, text, opened_at) VALUES ('want', ?, ?)", (f"loop {i}", when))
    e = n.exp(text="I did something new.", when="2026-11-21T09:00")
    n.b.handlers["sleep_loops"] = lambda m, s: {"close": [], "open": [
        {"kind": "want", "text": "a brand new thing", "sources": [e]}]}
    p = n.plan("2026-11-21T22:00")
    assert p.loops["fade"] == [1]                                      # older than expire_days
    assert p.loops["push"] == [2]                                      # max 2 open: loop 3 + the new one
    n.commit(p, "2026-11-22T05:00")
    assert [r["text"] for r in loops.open_rows(n.conn)] == ["loop 2", "a brand new thing"]


def test_a_full_memory_pushes_the_weakest_out(n):
    n.t["memory"]["long_cap"] = 5
    for i in range(7):
        n.conn.execute("INSERT INTO memories(kind, text, importance, created_at, sources) VALUES ('long', ?, ?, ?, '[]')",
                       (f"memory number {i}", 0.1 * (i + 1), "2026-11-21T07:00"))
    p = n.plan("2026-11-21T22:00")
    forgot = [r["text"] for r in n.q("SELECT text FROM memories WHERE id IN (%s)" % ",".join(map(str, p.forget)))]
    assert sorted(forgot) == ["memory number 0", "memory number 1"]

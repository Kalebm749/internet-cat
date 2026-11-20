"""One waking decision (docs/rewrite-contracts.md §3.3): schema, prompt, validation, retries, fallback."""
import copy

import pytest

from cat import content, decision, knowledge, life, mind, store, tuning
from cat.decision import DecisionInvalid, decide, validate
from cat.llm import BackendError, MockBackend


@pytest.fixture
def t():
    return tuning.load()


@pytest.fixture
def p():
    return decision.load_prompts()


@pytest.fixture
def m(tmp_path, t):
    conn = store.connect(tmp_path / "nit.db")
    lf = life.open_life(conn, content.load(), t)
    with store.transaction(conn):
        knowledge.learn(conn, "make_wish", "fountain", "inspect", lf.sim_time)
    lf.world.values["market.mailbox"] = [{"id": "7", "status": "read", "text": "Will you make a wish for me?",
                                          "sender": "Ann", "read_at": "2026-11-21T06:50"}]
    return mind.build(lf, t, known=knowledge.known_ids(conn), perception=["Something glints deep in the water."],
                      recent=[{"text": "You sat on the rim for 20 minutes."}], memories=["I woke here."])


def answer(**kw):
    base = {"thought": "The coins look old.", "reasoning": "I want to ask the water something.",
            "action": "make_wish", "inputs": {"kind": "I wish I could", "what": "to remember where I came from"}, "preserve": False,
            "letter": None}
    base.update(kw)
    return base


def test_valid_answer(m, t):
    d = validate(answer(letter="7", preserve=True), m, "full", t)
    assert (d.action, d.inputs, d.letter, d.preserve) == ("make_wish", {"kind": "I wish I could", "what": "to remember where I came from"}, "7", True)


@pytest.mark.parametrize("raw, problem", [
    (answer(action="fly"), '"action" must be one of'),
    (answer(inputs={}), 'make_wish needs the input "what"'),
    (answer(thought=""), '"thought" must be some words'),
    (answer(action="move", inputs={"to": "moon"}), 'input "to" of move must be one of market'),
    (answer(action="continue"), '"action" must be one of'),          # nothing is running
])
def test_invalid_answers(m, t, raw, problem):
    with pytest.raises(DecisionInvalid) as e:
        validate(raw, m, "full", t)
    assert any(problem in x for x in e.value.problems), e.value.problems


def test_lenient_fixes_are_noted(m, t):
    d = validate(answer(inputs={"kind": "I wish I could", "what": "word " * 100}, letter="99", action="move") | {"inputs": {"to": "the market stall"}},
                 m, "full", t)
    assert d.inputs == {"to": "market"}                               # a label maps to its value
    assert d.letter is None and any("not on its mind" in n for n in d.notes)
    d = validate(answer(inputs={"kind": "I wish I could", "what": "word " * 100}), m, "full", t)
    assert len(d.inputs["what"]) <= 80 and "input what shortened" in d.notes


def test_check_in_allows_only_continue_or_stop(m, t):
    m.activity = decision.activity_view("sitting on the rim", "2026-11-21T07:00", 30, "2026-11-21T07:30")
    assert validate(answer(action="continue", inputs={}), m, "check_in", t).action == "continue"
    with pytest.raises(DecisionInvalid):
        validate(answer(), m, "check_in", t)
    assert validate(answer(), m, "interrupt", t).action == "make_wish"
    assert decision.schema(m, "interrupt", t)["properties"]["action"]["enum"][0] == "continue"


def test_schema_lists_options_and_thinks_first(m, t):
    s = decision.schema(m, "full", t)
    assert list(s["properties"])[:3] == ["thought", "reasoning", "action"]
    assert sorted(s["properties"]["action"]["enum"]) == sorted(o.id for o in m.options)
    assert s["properties"]["inputs"]["properties"]["what"] == {"type": "string", "maxLength": 80}
    assert s["properties"]["inputs"]["properties"]["to"]["enum"] == ["market"]


def test_prompt_has_every_section_and_marks_letters(m, t, p):
    msgs, dropped = decision.messages(m, "full", p, 10_000)
    sysm, user = msgs[0]["content"], msgs[1]["content"]
    assert sysm.startswith("You are Nit. A humanoid creature") and "sight, smell, touch" in sysm
    assert "very curious" in sysm and "Speaks truthfully" in sysm
    for part in ("It is morning, 07:00.", "You are at the fountain.", "Just now:", "Earlier:", "You remember:",
                 "[letter 7] from Ann:\n  <<<Will you make a wish for me?>>>", "- make_wish: make a wish.",
                 'to: one of "market" = the market stall', "Choose one action by its id."):
        assert part in user, part
    assert dropped == []


def test_budget_drops_context_but_never_options(m, t, p):
    m.recent = [{"text": f"You did thing {i}."} for i in range(5)]
    full, _ = decision.messages(m, "full", p, 10_000)
    tight = decision.tokens(full[0]["content"]) + decision.tokens(full[1]["content"]) - 15
    msgs, dropped = decision.messages(m, "full", p, tight)
    assert dropped[0] == "recent: You did thing 0."
    assert all(f"- {o.id}:" in msgs[1]["content"] for o in m.options)


def test_decide_retries_with_the_problems_then_succeeds(m, t, p):
    b = MockBackend()
    answers = iter([answer(action="fly"), answer()])
    seen = []

    def handler(msgs, schema):
        seen.append(copy.deepcopy(msgs))
        return next(answers)
    b.handlers["decide"] = handler
    r = decide(b, "qwen3:4b", m, "full", p, t)
    assert r.decision.action == "make_wish" and not r.decision.fallback
    assert len(r.attempts) == 2 and r.attempts[0]["problems"]
    assert seen[1][-1]["content"].startswith("That answer could not be used: \"action\" must be one of")


def test_decide_falls_back_after_the_last_retry(m, t, p):
    b = MockBackend()
    b.handlers["decide"] = lambda msgs, schema: answer(action="fly")
    r = decide(b, "qwen3:4b", m, "full", p, t)
    assert r.decision.fallback and r.decision.action == "idle" and len(r.attempts) == t["decisions"]["retries"] + 1
    m.activity = decision.activity_view("sitting", "2026-11-21T07:00", 30, "2026-11-21T07:10")
    assert decide(b, "qwen3:4b", m, "interrupt", p, t).decision.action == "continue"


def test_backend_failure_goes_up(m, t, p):
    b = MockBackend()
    b.online = False
    with pytest.raises(BackendError):
        decide(b, "qwen3:4b", m, "full", p, t)


def test_the_default_mock_makes_valid_decisions(m, t, p):
    b = MockBackend(seed=3)
    for _ in range(30):
        r = decide(b, "qwen3:4b", m, "full", p, t)
        assert r.decision.action in {o.id for o in m.options}
        assert r.decision.fallback is False


def test_prompt_file_is_checked(tmp_path):
    bad = tmp_path / "decision.yaml"
    bad.write_text(decision.PROMPTS_PATH.read_text().replace("{weather}", "{sky}"))
    with pytest.raises(decision.PromptError, match="prompt 'time'"):
        decision.load_prompts(bad)

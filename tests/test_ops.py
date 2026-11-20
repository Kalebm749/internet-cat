"""World effect operations (docs/rewrite-contracts.md §4.4): each op, all-or-nothing application, bounds."""
import random

import pytest

from cat import content, ops
from cat.ops import OpError, apply_all


@pytest.fixture(scope="module")
def c():
    return content.load()


def facts(**over):
    base = {"world.weather": "clear", "fountain.wishes": [], "market.offerings": 2,
            "market.mailbox": [], "market.answered": []}
    base.update(over)
    return base


def run(c, op_list, f=None, vars=None, seed=0, seq=None):
    return apply_all(op_list, f or facts(), c.facts, vars or {"now": "2026-11-21T08:00"}, random.Random(seed), seq)


def test_set_and_type_check(c):
    r = run(c, [{"op": "set", "fact": "world.weather", "value": "rain"}])
    assert r.facts["world.weather"] == "rain" and r.changed == ["world.weather"]
    with pytest.raises(OpError, match="does not fit"):
        run(c, [{"op": "set", "fact": "world.weather", "value": "snow"}])


def test_add_clamps_to_min_and_max(c):
    assert run(c, [{"op": "add", "fact": "market.offerings", "value": -5}]).facts["market.offerings"] == 0
    assert run(c, [{"op": "add", "fact": "market.offerings", "value": 5}]).facts["market.offerings"] == 3
    r = run(c, [{"op": "add", "fact": "market.offerings", "value": 1}], f=facts(**{"market.offerings": 3}))
    assert r.changed == []


def test_append_renders_templates_and_gives_ids(c):
    op = {"op": "append", "fact": "fountain.wishes",
          "value": {"text": "{inputs.what}", "made": "{now}", "status": "open"}}
    r = run(c, [op, op], vars={"now": "2026-11-21T08:00", "inputs": {"what": "to know"}})
    assert r.facts["fountain.wishes"] == [
        {"id": "1", "text": "to know", "made": "2026-11-21T08:00", "status": "open"},
        {"id": "2", "text": "to know", "made": "2026-11-21T08:00", "status": "open"}]
    assert r.seq == {"fountain.wishes": 2}


def test_append_drops_the_oldest_beyond_max_len(c):
    full = [{"id": str(i)} for i in range(50)]
    r = run(c, [{"op": "append", "fact": "market.mailbox", "value": {"text": "new"}}],
            f=facts(**{"market.mailbox": full}), seq={"market.mailbox": 50})
    box = r.facts["market.mailbox"]
    assert len(box) == 50 and box[0]["id"] == "1" and box[-1] == {"id": "51", "text": "new"}


def mailbox():
    return [{"id": "1", "status": "read"}, {"id": "2", "status": "unread"}, {"id": "3", "status": "unread"}]


@pytest.mark.parametrize("pick, ids", [("all", ["2", "3"]), ("oldest", ["2"]), ("newest", ["3"])])
def test_update_where_picks(c, pick, ids):
    r = run(c, [{"op": "update_where", "fact": "market.mailbox", "match": {"status": "unread"},
                 "set": {"status": "read"}, "pick": pick}], f=facts(**{"market.mailbox": mailbox()}))
    changed = [m["id"] for m, before in zip(r.facts["market.mailbox"], mailbox()) if m != before]
    assert changed == ids


def test_update_where_random_one_is_seeded(c):
    op = {"op": "update_where", "fact": "market.mailbox", "match": {"status": "unread"},
          "set": {"status": "read"}, "pick": "random_one"}
    a = run(c, [op], f=facts(**{"market.mailbox": mailbox()}), seed=3).facts
    b = run(c, [op], f=facts(**{"market.mailbox": mailbox()}), seed=3).facts
    assert a == b
    assert sum(m["status"] == "unread" for m in a["market.mailbox"]) == 1


def test_remove_where(c):
    r = run(c, [{"op": "remove_where", "fact": "market.mailbox", "match": {"status": "unread"}}],
            f=facts(**{"market.mailbox": mailbox()}))
    assert r.facts["market.mailbox"] == [{"id": "1", "status": "read"}]
    assert r.log[0]["count"] == 2


def test_move_where_moves_to_the_answered_pile(c):
    """The reply_message contract (§4.8)."""
    op = c.actions["reply_message"].on_finish["world"][0]
    r = run(c, [op], f=facts(**{"market.mailbox": mailbox()}),
            vars={"now": "2026-11-21T09:00", "inputs": {"which": "1", "reply": "Thank you."}})
    assert [m["id"] for m in r.facts["market.mailbox"]] == ["2", "3"]
    assert r.facts["market.answered"] == [{"id": "1", "status": "answered", "reply": "Thank you.",
                                           "answered_at": "2026-11-21T09:00"}]
    assert r.changed == ["market.mailbox", "market.answered"]


def test_emit_event_changes_no_facts(c):
    r = run(c, [{"op": "emit_event", "event": "glint", "delay_minutes": 5}])
    assert r.events == [{"event": "glint", "delay_minutes": 5}] and r.changed == []


def test_a_failing_op_changes_nothing(c):
    before = facts(**{"market.mailbox": mailbox()})
    snapshot = repr(before)
    with pytest.raises(OpError):
        run(c, [{"op": "add", "fact": "market.offerings", "value": -1},
                {"op": "set", "fact": "world.weather", "value": "hail"}], f=before)
    assert repr(before) == snapshot


def test_missing_template_value_fails_cleanly(c):
    with pytest.raises(OpError, match="inputs"):
        run(c, [{"op": "append", "fact": "fountain.wishes", "value": {"text": "{inputs.what}"}}])


def test_random_op_sequences_respect_bounds(c):
    """Property test: whatever sequence of ops runs, numbers stay in min..max and lists within max_len."""
    rng = random.Random(42)
    f = facts()
    seq: dict = {}
    for _ in range(400):
        choice = rng.randrange(4)
        if choice == 0:
            op = {"op": "add", "fact": "market.offerings", "value": rng.randint(-3, 3)}
        elif choice == 1:
            op = {"op": "append", "fact": "market.mailbox", "value": {"status": rng.choice(["unread", "read"])}}
        elif choice == 2:
            op = {"op": "move_where", "from": "market.mailbox", "to": "market.answered",
                  "match": {"status": "read"}, "pick": rng.choice(ops.PICKS)}
        else:
            op = {"op": "update_where", "fact": "market.mailbox", "match": {"status": "unread"},
                  "set": {"status": "read"}, "pick": rng.choice(ops.PICKS)}
        r = apply_all([op], f, c.facts, {}, rng, seq)
        f, seq = r.facts, r.seq
        assert 0 <= f["market.offerings"] <= 3
        assert len(f["market.mailbox"]) <= 50 and len(f["market.answered"]) <= 200
        ids = [m["id"] for m in f["market.mailbox"] + f["market.answered"]]
        assert len(ids) == len(set(ids))


def test_validate_reports_fields(c):
    v = dict(fact=c.fact, event_exists=lambda e: e in c.events, check_template=lambda ph: None)
    assert ops.validate({"op": "add", "fact": "market.offerings"}, **v) == ["add: missing 'value'"]
    assert ops.validate({"op": "add", "fact": "market.offerings", "value": 1, "by": 2}, **v) == ["add: unknown field 'by'"]
    assert ops.validate({"op": "emit_event", "event": "thunder"}, **v) == ["emit_event: no event 'thunder'"]
    assert "pick must be one of" in ops.validate({"op": "remove_where", "fact": "market.mailbox",
                                                  "match": {"a": 1}, "pick": "first"}, **v)[0]

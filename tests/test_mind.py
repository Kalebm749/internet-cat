"""Knowledge and the Mind (docs/rewrite-contracts.md §3.3, §4.8, §5.4)."""
import pytest

from cat import content, knowledge, life, mind, store, tuning


@pytest.fixture
def t():
    return tuning.load()


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "nit.db")


@pytest.fixture
def lf(conn, t):
    return life.open_life(conn, content.load(), t)


def build(lf, conn, t, **kw):
    return mind.build(lf, t, known=knowledge.known_ids(conn), **kw)


def ids(m):
    return sorted(o.id for o in m.options)


def test_spawn_knows_innate_and_arrival_actions(conn, lf):
    k = knowledge.all_known(conn)
    assert {a for a, x in k.items() if x.discovered_by == "innate"} == {"move", "inspect", "sleep"}
    assert {a for a, x in k.items() if x.discovered_by == "arrival"} == {"sit_and_reflect"}
    assert not k["move"].provisional and k["sit_and_reflect"].provisional


def test_arrival_learns_once(conn, lf):
    with store.transaction(conn):
        assert knowledge.arrive(conn, lf.content, "market", lf.sim_time) == ["eat_offering", "read_messages"]
        assert knowledge.arrive(conn, lf.content, "market", lf.sim_time) == []


def test_options_are_known_and_possible_actions_only(conn, lf, t):
    m = build(lf, conn, t)
    assert ids(m) == ["inspect", "move", "sit_and_reflect"]        # 07:00, energy 0.8: no sleep; wish still hidden
    lf.bodystate.energy = 0.2
    assert "sleep" in ids(build(lf, conn, t))
    with store.transaction(conn):
        knowledge.learn(conn, "make_wish", "fountain", "inspect", lf.sim_time)
    assert "make_wish" in ids(build(lf, conn, t))
    lf.bodystate.energy = 0.05                                     # make_wish requires energy >= 0.1
    assert "make_wish" not in ids(build(lf, conn, t))


def test_move_offers_the_exits(conn, lf, t):
    mv = build(lf, conn, t).option("move")
    assert mv.inputs["to"]["choices"] == [{"value": "market", "label": "the market stall"}]


def letter(i, status, text="hello", sender="Ann", read_at=None):
    d = {"id": str(i), "status": status, "text": text, "sender": sender}
    if read_at:
        d["read_at"] = read_at
    return d


def test_mail_choices_and_reply_need_a_read_letter(conn, lf, t):
    with store.transaction(conn):
        knowledge.arrive(conn, lf.content, "market", lf.sim_time)
        knowledge.learn(conn, "reply_message", "market", "effect", lf.sim_time)
    lf.location = "market"
    assert "read_messages" not in ids(build(lf, conn, t))          # empty mailbox
    lf.world.values["market.mailbox"] = [letter(1, "read", read_at="2026-11-21T06:00"), letter(2, "unread", "a question")]
    m = build(lf, conn, t)
    choices = m.option("read_messages").inputs["which"]["choices"]
    assert [c["value"] for c in choices] == ["2", "1"]              # unread first
    assert choices[0]["label"] == 'Ann: "a question" (unread)'
    assert [c["value"] for c in m.option("reply_message").inputs["which"]["choices"]] == ["1"]


def test_letters_on_mind_are_read_unanswered_recent_and_bounded(conn, lf, t):
    t["mail"].update(on_mind=2, on_mind_chars=40, on_mind_days=1)
    lf.world.values["market.mailbox"] = [
        letter(1, "read", "old", read_at="2026-11-19T07:00"),        # older than a day
        letter(2, "read", "x" * 100, "Bo", read_at="2026-11-21T06:00"),
        letter(3, "unread", "not yet read"),
        letter(4, "read", "newest", read_at="2026-11-21T06:30"),
    ]
    m = build(lf, conn, t)
    assert [l["id"] for l in m.letters] == ["4", "2"]
    assert len(m.letters[1]["text"]) <= 40 and m.letters[1]["text"].endswith("...")
    assert m.letters[1]["sender"] == "Bo"


def test_visible_facts_and_body(conn, lf, t):
    lf.location = "market"
    lf.world.values["market.mailbox"] = [letter(1, "read"), letter(2, "unread"), letter(3, "unread")]
    lf.bodystate.hunger = 0.7
    m = build(lf, conn, t)
    assert "mailbox: 3 (1 read, 2 unread)" in m.location["visible_facts"]
    assert "offerings: 2" in m.location["visible_facts"]
    assert m.body["words"][0] == "Your stomach is a small, insistent knot."


def test_prohibited_and_disabled_capabilities_are_never_offered(conn, lf, t):
    lf.content.character["core"]["prohibited_actions"] = ["sit_and_reflect"]
    assert "sit_and_reflect" not in ids(build(lf, conn, t))
    lf.content.character["core"]["prohibited_actions"] = []
    lf.location = "market"
    with store.transaction(conn):
        knowledge.arrive(conn, lf.content, "market", lf.sim_time)
    lf.world.values["market.mailbox"] = [letter(1, "unread")]
    assert "read_messages" in ids(build(lf, conn, t))
    lf.content.capabilities["read_message"]["enabled"] = False
    assert "read_messages" not in ids(build(lf, conn, t))


def test_options_are_shuffled_with_a_seed(conn, lf, t):
    lf.bodystate.energy = 0.2
    a = [o.id for o in build(lf, conn, t, seed=1).options]
    assert a == [o.id for o in build(lf, conn, t, seed=1).options]
    orders = {tuple(o.id for o in build(lf, conn, t, seed=s).options) for s in range(20)}
    assert len(orders) > 1


def test_identity_block(conn, lf, t):
    i = build(lf, conn, t).identity
    assert i["name"] == "Nit" and i["senses"] == ["sight", "smell", "touch"]
    assert i["traits"] == ["very curious", "very truthful", "inward"]

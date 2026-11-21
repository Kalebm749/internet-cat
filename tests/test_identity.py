"""Character versions (docs/rewrite-contracts.md §2.1, §5.6)."""
import pytest

from cat import content, identity, store
from cat.identity import IdentityError, apply_change
from tests.rewrite_helpers import copy_pack


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "nit.db")
    store.migrate(c)
    return c


def ensure(conn, c, t="2026-11-21T07:00"):
    with store.transaction(conn):
        return identity.ensure(conn, c, t)


def test_spawn_writes_version_one_once(conn):
    c = content.load()
    rep = ensure(conn, c)
    assert rep.created and rep.version == 1
    v = identity.current(conn)
    assert v.reason == "spawn" and v.sheet["name"] == "Nit"
    assert v.sheet["core"]["nature"].startswith("A humanoid creature")
    assert v.sheet["traits"] == {"curious": 0.8, "truthful": 0.7, "practical": 0.5}
    assert not ensure(conn, c).created and len(identity.history(conn)) == 1


def test_author_edits_to_core_make_a_new_version(conn, tmp_path):
    pack = copy_pack(tmp_path)
    ensure(conn, content.load(pack.root))
    pack.replace("character.yaml", '- "Never harms the places or things others care about."',
                 '- "Never harms the places or things others care about."\n    - "Never lies to a visitor."')
    rep = ensure(conn, content.load(pack.root), "2026-11-22T07:00")
    assert rep.author_changes == ["core"] and rep.version == 2
    assert "Never lies to a visitor." in identity.current(conn).sheet["core"]["boundaries"]
    assert identity.current(conn).reason == "author edit: core"


def test_mutable_seed_edits_after_spawn_are_ignored(conn, tmp_path):
    pack = copy_pack(tmp_path)
    ensure(conn, content.load(pack.root))
    pack.replace("character.yaml", "curious: 0.8", "curious: 0.2")
    rep = ensure(conn, content.load(pack.root))
    assert rep.ignored_seed_changes == ["traits"] and rep.version == 1
    assert identity.current(conn).sheet["traits"]["curious"] == 0.8


def sheet():
    return identity.seed_sheet(content.load())


@pytest.mark.parametrize("change, check", [
    ({"kind": "add_trait", "target": "patient", "value": 0.2}, lambda s: s["traits"]["patient"] == 0.2),
    ({"kind": "adjust_trait", "target": "curious", "value": 0.75}, lambda s: s["traits"]["curious"] == 0.75),
    ({"kind": "remove_trait", "target": "practical"}, lambda s: "practical" not in s["traits"]),
    ({"kind": "add_tendency", "target": "lingers near water", "value": 0.2},
     lambda s: s["tendencies"] == {"lingers near water": 0.2}),
    ({"kind": "background_note", "value": "It has made a wish."},
     lambda s: s["background"].endswith("before. It has made a wish.")),
])
def test_apply_change(change, check):
    before = sheet()
    after = apply_change(before, change)
    assert check(after)
    assert before == sheet()                      # the input is not mutated
    assert after["core"] == before["core"]


@pytest.mark.parametrize("change, msg", [
    ({"kind": "rewrite_core"}, "unknown change kind"),
    ({"kind": "add_trait", "target": "curious", "value": 0.3}, "already exists"),
    ({"kind": "adjust_trait", "target": "brave", "value": 0.3}, "no trait 'brave'"),
    ({"kind": "adjust_trait", "target": "curious", "value": 1.5}, "between 0 and 1"),
    ({"kind": "remove_tendency", "target": "sings"}, "no tendency 'sings'"),
    ({"kind": "background_note", "value": "  "}, "needs a sentence"),
])
def test_bad_changes_are_refused(change, msg):
    with pytest.raises(IdentityError, match=msg):
        apply_change(sheet(), change)


def test_commit_change_stores_reason_and_evidence(conn):
    ensure(conn, content.load())
    with store.transaction(conn):
        v = identity.commit_change(conn, {"kind": "add_tendency", "target": "lingers near water", "value": 0.2},
                                   "sleep day 4", [12, 40, 77], "2026-11-25T03:00")
    assert v.version == 2
    back = identity.current(conn)
    assert back.evidence == [12, 40, 77] and back.change["target"] == "lingers near water"
    assert [h.reason for h in identity.history(conn)] == ["spawn", "sleep day 4"]

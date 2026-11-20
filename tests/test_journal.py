"""The journal: the entity's own entry, written AFTER code decided what its move did, from facts only. It decides
nothing and changes nothing. (Replaces the narrator: the diary's world line is code's plain account.)"""
import random
import re

from cat import journal
from cat.engine import Engine
from cat.llm import MockBackend
from tests.helpers import pick


def make(cfg, label, entry="I did it.", enabled=True):
    cfg.world["events"] = []
    cfg.settings["director"]["enabled"] = False
    cfg.settings["journal"]["enabled"] = enabled
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    seen = []
    b.handlers["brain"] = lambda m, s: pick(m, label, why="it was there")
    b.handlers["journal"] = lambda m, s: (seen.append(m), {"entry": entry})[1]
    e = Engine(cfg, b, rng=random.Random(1))
    e.world.events = None
    return e, b, seen


def test_the_journal_is_written_after_the_outcome_from_facts(cfg):
    e, b, seen = make(cfg, "look out and watch the world for a while")
    e.tick()
    row = e.conn.execute("SELECT outcome, result, entry, intent FROM ticks WHERE kind='tick'").fetchone()
    sysm, user = seen[0][0]["content"], seen[0][1]["content"]
    assert row["entry"] == "I did it."
    assert row["outcome"] == row["result"] and row["result"] in user          # it is told what really happened
    assert "it was there" in user                                              # and the reason it gave
    assert not any(re.search(rf"\b{nid}\b", user) for nid in cfg.character["needs"])   # needs are never named
    assert "Do not add colours" in sysm and "Never name your own traits or needs" in sysm
    assert "move:" not in sysm                                                 # it is not asked to pick a move


def test_the_brain_no_longer_writes_the_entry(cfg):
    from cat import brain
    assert "entry" not in brain.BRAIN_SCHEMA["properties"]
    e, b, seen = make(cfg, "look out and watch the world for a while")
    calls = []
    b.handlers["brain"] = lambda m, s: (calls.append(m[0]["content"]), pick(m, "look out"))[1]
    e.tick()
    assert "- entry:" not in calls[0]


def test_the_journal_is_told_which_needs_crossed_a_band_as_impressions(cfg):
    e, b, seen = make(cfg, "sit quietly and do nothing for a while")
    sus = next(iter(cfg.character["needs"]))
    lines = journal.need_changes(cfg, {**e.state["needs"], sus: 41.0}, {**e.state["needs"], sus: 39.0})
    assert lines == [cfg.character["needs"][sus]["expressions"]["low"].strip()]
    eased = journal.need_changes(cfg, {**e.state["needs"], sus: 39.0}, {**e.state["needs"], sus: 50.0})
    exp_okay = cfg.character["needs"][sus]["expressions"].get("okay")
    assert eased == ([exp_okay.strip()] if exp_okay else ["Something that had been lacking in you eased."])
    assert journal.need_changes(cfg, e.state["needs"], e.state["needs"]) == []


def test_a_failed_journal_means_no_entry_and_the_tick_carries_on(cfg):
    e, b, _ = make(cfg, "look out and watch the world for a while")
    b.handlers["journal"] = lambda m, s: {"entry": ""}                        # unusable: retried, then given up
    info = e.tick()
    row = e.conn.execute("SELECT entry, intent, kind FROM ticks").fetchone()
    assert info["kind"] == "tick" and row["entry"] is None and row["intent"] == "it was there"


def test_journal_off_makes_no_call(cfg):
    e, b, seen = make(cfg, "look out and watch the world for a while", enabled=False)
    e.tick()
    assert seen == [] and not any(t == "journal" for t, _ in b.calls)
    assert e.conn.execute("SELECT entry FROM ticks").fetchone()["entry"] is None


def test_an_impulse_keeps_its_own_line_when_the_journal_fails(cfg):
    cfg.settings["anxiety"]["enabled"] = True
    cfg.settings["anxiety"].update(impulse_above=0, impulse_max_chance=1.0, start=100)
    cfg.settings["anxiety"]["impulses"] = {"freeze": {"weight": 1, "verbs": ["wait"]}}
    e, b, seen = make(cfg, "look out")
    e.tick()
    assert "before you had thought it through" in seen[0][1]["content"]      # the journal knows it was an impulse
    b.handlers["journal"] = lambda m, s: {"entry": ""}
    e.tick()
    entries = [r["entry"] for r in e.conn.execute("SELECT entry FROM ticks WHERE kind='tick' ORDER BY id")]
    assert entries[0] == "I did it." and "held still" in entries[1]


def test_whatever_the_journal_says_the_world_is_unchanged(cfg):
    e, b, _ = make(cfg, "go to the kitchen")
    b.handlers["journal"] = lambda m, s: {"entry": "x", "gains": {"joy": "++"},
                                          "changes": [{"op": "move_self", "area": "garden"}]}
    before = dict(e.state["needs"])
    e.tick()
    assert e.state["area"] == "kitchen" and e.state["needs"]["joy"] < before["joy"]


# ---------- the sustenance rule, by construction ----------
def test_only_eating_raises_sustenance(cfg):
    e, b, _ = make(cfg, "sit quietly and do nothing for a while")
    e.state["needs"]["sustenance"] = 10.0
    for _ in range(4):
        e.tick()
    assert e.state["needs"]["sustenance"] < 10                                   # nothing but decay
    cfg.settings["db_path"] = cfg.settings["db_path"] + "2"
    e2, b2, _ = make(cfg, "eat a bowl of fish")
    e2.state["area"] = "kitchen"
    e2.state["needs"]["sustenance"] = 10.0
    e2.tick()
    assert e2.state["needs"]["sustenance"] > 10 + cfg.settings["gain_values"]["++"] - 3


def test_eating_is_not_offered_without_food(cfg):
    e, b, _ = make(cfg, "sit quietly and do nothing for a while")
    assert not any(m.verb == "eat" for m in e._menu())
    e.state["area"] = "kitchen"
    assert any(m.verb == "eat" for m in e._menu())

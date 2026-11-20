"""The narrator is prose only: it decides nothing and changes nothing. Code decides what a move does."""
import random
import re

import pytest

from cat import narrator
from cat.engine import Engine
from cat.llm import BackendError, MockBackend
from cat.world import WorldState
from tests.helpers import pick


def world_text(cfg, conn):
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    return narrator.describe_world(cfg, "2026-11-18T19:40", "kitchen", "rain", w.here("kitchen"), [])


def test_input_is_the_move_the_result_and_the_scene_never_needs(cfg, conn):
    msgs = narrator.build_messages(cfg, "eat a bowl of fish", world_text(cfg, conn), "You ate a bowl of fish.")
    blob = msgs[0]["content"] + msgs[1]["content"]
    assert "The entity chose: eat a bowl of fish" in blob and "You ate a bowl of fish." in blob
    assert "Place: the kitchen" in blob and "7:40 pm" in blob and "Weather: rain" in blob and "- a bowl of fish" in blob and "edible" not in blob
    for word in ("sustenance", "wonder", "needs", "mood", "energy", "affinit", "gain", "project"):
        assert word not in blob.lower(), word


def test_the_narrator_cannot_decide_anything(cfg):
    out = narrator.parse({"outcome": "ok", "gains": {"joy": "++"}, "effort": "high", "duration": 90,
                          "changes": [{"op": "consume", "object_id": "bowl"}], "project": {"id": 1}}, cfg)
    assert out == {"outcome": "ok"}


def make(cfg, label, outcome="It happens.", narrate=True):
    cfg.world["events"] = []
    cfg.settings["director"]["enabled"] = False
    cfg.settings["moves"]["narrate"] = narrate
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    seen = []
    b.handlers["brain"] = lambda m, s: pick(m, label)
    b.handlers["narrator"] = lambda m, s: (seen.append(m[-1]["content"]), {"outcome": outcome})[1]
    e = Engine(cfg, b, rng=random.Random(1))
    e.world.events = None
    return e, b, seen


def test_the_diary_gets_the_narrators_prose_and_the_narrator_gets_the_template(cfg):
    e, b, seen = make(cfg, "go to the kitchen", outcome="She wandered into the kitchen, nose up.")
    e.tick()
    assert "What happened (decided by the world): You went to the kitchen." in seen[0]
    assert e.conn.execute("SELECT outcome FROM ticks").fetchone()[0] == "She wandered into the kitchen, nose up."


def test_a_failed_narration_uses_the_template_and_the_tick_carries_on(cfg):
    e, b, _ = make(cfg, "go to the kitchen")
    def down(m, s): raise BackendError("narrator down")
    b.handlers["narrator"] = down
    info = e.tick()
    assert info["kind"] == "tick" and e.state["area"] == "kitchen"
    assert e.conn.execute("SELECT outcome FROM ticks").fetchone()[0] == "You went to the kitchen."


def test_narrate_false_makes_no_narrator_call(cfg):
    e, b, seen = make(cfg, "go to the kitchen", narrate=False)
    e.tick()
    assert seen == [] and not any(t == "narrator" for t, _ in b.calls)
    assert e.conn.execute("SELECT outcome FROM ticks").fetchone()[0] == "You went to the kitchen."


def test_whatever_the_narrator_says_the_world_is_unchanged(cfg):
    e, b, _ = make(cfg, "go to the kitchen")
    b.handlers["narrator"] = lambda m, s: {"outcome": "x", "gains": {"joy": "++"}, "effort": "high", "duration": 90,
                                           "changes": [{"op": "move_self", "area": "garden"}]}
    before = dict(e.state["needs"])
    e.tick()
    assert e.state["area"] == "kitchen" and e.state["needs"]["joy"] < before["joy"]


# ---------- the sustenance rule, now by construction ----------
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

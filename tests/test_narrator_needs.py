"""The narrator sees each need's meaning and band (as a word), no affinity table, and only ever adds."""
import random
import re

import pytest

from cat import narrator
from cat.engine import Engine
from cat.llm import MockBackend
from cat.world import WorldState

LEVELS = {"sustenance": 12, "wonder": 50, "joy": 90, "peace": 30, "curiosity": 60}


def world_text(cfg, conn, needs=LEVELS):
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    return narrator.describe_world(cfg, "2026-11-18T19:40", "kitchen", "rain", w.here("kitchen"), [], needs)


def needs_block(text):
    return text[text.index(cfg_heading(text)):]


def cfg_heading(text):
    return "Needs (what each means"


def test_input_has_each_needs_meaning_and_band_word(cfg, conn):
    text = world_text(cfg, conn)
    for nid, spec in cfg.character["needs"].items():
        assert f"- {nid}: {spec['meaning']}" in text
    assert "sustenance: " in text and "Right now: empty." in text
    assert "joy: Lightness and play: enjoyment for its own sake. Right now: full." in text
    assert "peace: Inner calm and stillness: freedom from restlessness, noise and worry. Right now: low." in text


def test_needs_block_shows_words_never_numbers(cfg, conn):
    block = needs_block(world_text(cfg, conn))
    assert not re.search(r"\d", block)


def test_no_affinity_table_or_action_ranking(cfg, conn):
    text = narrator.build_messages(cfg, "eat the fish", world_text(cfg, conn))
    blob = (text[0]["content"] + text[1]["content"]).lower()
    for word in ("affinit", "fed_by", "drained_by", "best action", "should do"):
        assert word not in blob


def test_input_still_has_action_place_objects_inventory_time_weather(cfg, conn):
    msgs = narrator.build_messages(cfg, "eat the fish", world_text(cfg, conn))
    user = msgs[1]["content"]
    assert "The creature does: eat the fish" in user
    assert "Location: kitchen" in user and "bowl" in user and "Inventory:" in user
    assert "7:40 pm" in user and "Weather: rain" in user


def test_the_engine_passes_current_bands_to_the_narrator(cfg):
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    seen = []
    b.handlers["narrator"] = lambda m, s: (seen.append(m[-1]["content"]) or
                                           {"outcome": "ok", "gains": {}, "effort": "low", "duration": 10, "changes": []})
    e = Engine(cfg, b, rng=random.Random(1))
    e.state["needs"]["peace"] = 5.0
    e.tick()
    assert "- peace: " in seen[0] and "Right now: empty." in seen[0]


def test_system_prompt_says_the_narrator_only_adds(cfg):
    s = narrator.system_prompt(cfg)
    assert "You only ever add to a need" in s and "Never give negative values" in s


def test_output_schema_is_unchanged(cfg):
    sch = narrator.schema(cfg)
    assert set(sch["properties"]) == {"outcome", "gains", "effort", "duration", "changes"}
    assert set(sch["properties"]["gains"]["properties"]) == set(cfg.character["needs"])
    assert sorted(sch["properties"]["gains"]["properties"]["joy"]["enum"]) == ["+", "++", "0"]


def test_negative_or_unknown_buckets_are_dropped(cfg):
    out = narrator.parse({"outcome": "x", "gains": {"joy": "-", "peace": "--", "wonder": "++", "nope": "+"},
                          "effort": "low", "duration": 10, "changes": []}, cfg)
    assert out["gains"] == {"wonder": "++"}


def _eat_engine(cfg, gains):
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": "eat the fish", "intent": "",
                                        "feeling": "neutral", "wants_sleep": False}
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok", "gains": gains, "effort": "low", "duration": 6,
                                           "changes": s["changes"]}
    e = Engine(cfg, b, rng=random.Random(1))
    e.world.events = None
    cfg.world["events"] = []
    e.state["area"] = "kitchen"
    e.state["needs"]["sustenance"] = 10.0
    return e, b


def test_sustenance_does_not_rise_unless_something_edible_is_consumed(cfg):
    cfg.world["events"] = []
    e, b = _eat_engine(cfg, {"sustenance": "++"})
    b.handlers["narrator"] = lambda m, s: {"outcome": "It sniffs the bowl.", "gains": {"sustenance": "++"},
                                           "effort": "low", "duration": 6, "changes": []}
    e.tick()
    decayed = 10 - cfg.character["needs"]["sustenance"]["decay_per_hour"] * 6 / 60
    assert e.state["needs"]["sustenance"] == pytest.approx(decayed)


def test_sustenance_rises_when_an_edible_object_is_consumed(cfg):
    cfg.world["events"] = []
    e, b = _eat_engine(cfg, {"sustenance": "++"})
    b.handlers["narrator"] = lambda m, s: {"outcome": "It eats.", "gains": {"sustenance": "++"},
                                           "effort": "low", "duration": 6,
                                           "changes": [{"op": "consume", "object_id": "bowl"}]}
    e.tick()
    assert e.state["needs"]["sustenance"] > 10 + cfg.settings["gain_values"]["++"] - 2


def test_consuming_guarantees_the_minimum_gain_even_if_the_narrator_forgets(cfg):
    cfg.world["events"] = []
    e, b = _eat_engine(cfg, {})
    b.handlers["narrator"] = lambda m, s: {"outcome": "It eats.", "gains": {}, "effort": "low", "duration": 6,
                                           "changes": [{"op": "consume", "object_id": "bowl"}]}
    e.tick()
    assert e.state["needs"]["sustenance"] > 10 + cfg.settings["gain_values"][cfg.settings["consume_min_gain"]] - 2

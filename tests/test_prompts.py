import re

from cat import brain, narrator
from cat.world import WorldState


def ctx(cfg, conn, **kw):
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    base = dict(sim_time="2026-11-18T19:40", area_id="living_room",
                needs={k: 12.0 + i * 17 for i, k in enumerate(cfg.character["needs"])},
                energy=41.0, mood={"description": "calm and tired", "word": "calm"},
                tendencies={"boldness": 0.8, "sociability": 0.1, "tidiness": 0.5},
                weather="rain", objects_here=w.here("living_room"), inventory=[])
    base.update(kw)
    return brain.BrainContext(**base)


def test_brain_prompt_has_words_not_numbers(cfg, conn):
    p = brain.situation_prompt(cfg, ctx(cfg, conn, last_outcome="A bird lands.", events=["It rains."],
                                       recent_actions=["nap", "wash"]))
    assert "7:40 pm" in p and "evening" in p and "the living room" in p
    assert "running on nothing" in p                  # sustenance 12 -> its empty-band expression
    assert "calm and tired" in p and "tired" in p     # mood, energy words
    assert "the sofa [soft, warm, climbable]" in p
    assert "bold (boldness)" in p and "solitary (sociability)" in p and "neither scruffy nor tidy" in p
    assert "A bird lands." in p and "It rains." in p and "nap; then wash" in p
    assert "the kitchen" in p and "Memories" not in p
    assert not re.search(r"\b\d+(\.\d+)?\b", p.replace("7:40", "")), "no raw numbers"
    assert "sofa:" not in p and "object_id" not in p   # brain never sees ids


def test_brain_prompt_optional_sections(cfg, conn):
    p = brain.situation_prompt(cfg, ctx(
        cfg, conn, just_woke_from_doze=True, long_memories=["The oak was lovely."],
        short_memories=["Ate fish."],
        preferences=[{"subject": "the old tree", "valence": 0.8, "reason": "x"},
                     {"subject": "rain", "valence": -0.5, "reason": "y"}]))
    assert "doze" in p and "The oak was lovely." in p and "Ate fish." in p
    assert "You like: the old tree" in p and "You dislike: rain" in p
    assert "start of your day" in brain.situation_prompt(cfg, ctx(cfg, conn))


def test_brain_system_prompt_has_identity_not_numbers(cfg):
    s = brain.system_prompt(cfg)
    assert cfg.character["name"] in s and "curious" in s and "wants_sleep" in s


def test_brain_parse_defaults_and_errors(cfg):
    import pytest
    from cat.llm import BackendError
    out = brain.parse({"action": " nap ", "feeling": "weird", "wants_sleep": "yes"})
    assert out["action"] == "nap" and out["feeling"] == "neutral" and out["wants_sleep"] is False
    with pytest.raises(BackendError):
        brain.parse({"action": "  "})


def test_narrator_prompt_lists_ops_ids_and_rules(cfg, conn):
    sysmsg = narrator.system_prompt(cfg)
    for op in narrator.OPS:
        assert op in sysmsg
    assert "ONLY if the creature eats" in sysmsg and "sustenance" in sysmsg
    assert "personality" in sysmsg
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    d = narrator.describe_world(cfg, "2026-11-18T19:40", "kitchen", "rain", w.here("kitchen"), [])
    assert "- bowl: a bowl of fish | tags: edible, movable" in d and "Exits: living_room (" in d
    msgs = narrator.build_messages(cfg, "eat the fish", d)
    assert msgs[1]["content"].endswith("The creature does: eat the fish")


def test_narrator_schema_matches_config(cfg):
    sc = narrator.schema(cfg)
    assert set(sc["properties"]["gains"]["properties"]) == set(cfg.character["needs"])
    assert sc["properties"]["changes"]["items"]["properties"]["op"]["enum"] == narrator.OPS


def test_narrator_parse_clamps_and_filters(cfg):
    out = narrator.parse({"outcome": "x", "gains": {"joy": "++", "bogus": "+", "rest": "+++"},
                          "effort": "huge", "duration": "9999", "changes": [{"op": "take"}, 5]}, cfg)
    assert out["gains"] == {"joy": "++"} and out["effort"] == "medium"
    assert out["duration"] == cfg.settings["sim_minutes_max"] and out["changes"] == [{"op": "take"}]
    assert narrator.parse({"outcome": "x", "gains": None, "changes": None, "duration": "a"},
                          cfg)["duration"] == cfg.settings["sim_minutes_per_tick_default"]


def test_system_prompt_says_it_does_not_know_what_it_is(cfg):
    s = brain.system_prompt(cfg)
    assert "## Identity" in s
    assert "a name" in s and "a voice" in s and "core traits" in s and "tendencies" in s
    assert "do not know what you are" in s and "any physical form" in s
    assert "must not assert a form, species or body that is not in your self-image" in s
    for word in ("species:", "appearance", "fur", "paw", "humanoid"):
        assert word not in s.lower().replace("species or body", ""), word


def test_empty_self_image_is_stated(cfg, conn):
    p = brain.situation_prompt(cfg, ctx(cfg, conn))
    assert "self-image is empty" in p


def test_self_image_text_is_in_the_prompt(cfg, conn):
    p = brain.situation_prompt(cfg, ctx(cfg, conn, self_image=["I seem to be small.", "I like high places."]))
    assert "I seem to be small. I like high places." in p and "self-image is empty" not in p


def test_seed_hint_comes_from_config(cfg):
    assert brain.seed_self_image(cfg) == []
    cfg.character["self_image"] = {"seed": "  Something quiet.  "}
    assert brain.seed_self_image(cfg) == ["Something quiet."]


def test_brain_output_schema_unchanged(cfg):
    assert set(brain.BRAIN_SCHEMA["properties"]) == {"reaction", "thought", "action", "intent",
                                                     "feeling", "wants_sleep"}

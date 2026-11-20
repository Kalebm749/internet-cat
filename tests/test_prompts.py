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
    assert "very boldness" in p and "a little sociability" in p and "tidiness" in p
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
    assert cfg.character["name"] in s and "curious" in s and "move" in s and "why" in s
    assert "wants_sleep" not in s and "- action:" not in s


def test_brain_parse_picks_from_the_menu_and_rejects_everything_else(cfg):
    import pytest
    from cat import moves as M
    from cat.llm import BackendError
    menu = [M.Move("m1", "wait", "wait", None, "", "sit quietly"), M.Move("m2", "go:kitchen", "go", "kitchen", "the kitchen", "go to the kitchen")]
    out = brain.parse({"move": " m2 ", "why": " hungry ", "feeling": "weird", "reaction": "r"}, menu)
    assert out["move"].key == "go:kitchen" and out["why"] == "hungry" and out["feeling"] == "neutral"
    for bad in ({"move": "m9"}, {"move": ""}, {"move": "eat:bowl"}, {}, {"action": "nap"}):
        with pytest.raises(BackendError):
            brain.parse(bad, menu)


def test_the_brain_menu_is_in_the_prompt_in_order(cfg, conn):
    from cat import moves as M
    menu = [M.Move("m1", "wait", "wait", None, "", "sit quietly"), M.Move("m2", "go:kitchen", "go", "kitchen", "the kitchen", "go to the kitchen")]
    p = brain.situation_prompt(cfg, ctx(cfg, conn, menu=menu))
    assert "## What you can do right now\n- [m1] sit quietly\n- [m2] go to the kitchen" in p
    assert p.rstrip().endswith("Choose one move and reply with JSON.")
    assert "What you can do right now" not in brain.situation_prompt(cfg, ctx(cfg, conn))


def test_the_brain_output_schema_is_move_and_why():
    assert set(brain.BRAIN_SCHEMA["properties"]) == {"reaction", "thought", "move", "why", "feeling", "entry"}
    assert set(brain.BRAIN_SCHEMA["required"]) == {"reaction", "thought", "move", "why", "feeling", "entry"}


def test_narrator_is_prose_only(cfg, conn):
    sysmsg = narrator.system_prompt(cfg)
    assert "you do not decide anything and you cannot change anything" in sysmsg
    assert "gains" not in sysmsg and "consume" not in sysmsg and "changes" not in sysmsg
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    d = narrator.describe_world(cfg, "2026-11-18T19:40", "kitchen", "rain", w.here("kitchen"), [])
    assert "- a bowl of fish" in d and "edible" not in d and "Place: the kitchen" in d
    msgs = narrator.build_messages(cfg, "eat a bowl of fish", d, "You ate a bowl of fish.")
    assert "The entity chose: eat a bowl of fish" in msgs[1]["content"]
    assert "What happened (decided by the world): You ate a bowl of fish." in msgs[1]["content"]


def test_narrator_schema_and_parse_are_just_an_outcome(cfg):
    import pytest
    from cat.llm import BackendError
    assert narrator.schema(cfg)["required"] == ["outcome"]
    assert narrator.parse({"outcome": "  A  quiet  moment. ", "gains": {"joy": "++"}, "changes": [{"op": "take"}]}, cfg) == \
        {"outcome": "A quiet moment."}
    with pytest.raises(BackendError):
        narrator.parse({"outcome": "  "}, cfg)


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



"""The brain's "What's pulling at you right now" section: bands -> words, only the most pressing."""
import re

import pytest

from cat import brain
from cat.config import load_config
from cat.world import WorldState

LEVELS = {"sustenance": 5, "wonder": 30, "joy": 10, "peace": 80, "curiosity": 20}   # empty, low, empty, full, low


def X(cfg, need, band):
    return cfg.character["needs"][need]["expressions"][band]


def ctx(cfg, conn, **kw):
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    base = dict(sim_time="2026-11-18T19:40", area_id="living_room", needs=dict(LEVELS), energy=10.0,
                mood={"description": "calm", "word": "calm"}, tendencies={}, weather="clear",
                objects_here=w.here("living_room"), inventory=[])
    base.update(kw)
    return brain.BrainContext(**base)


def section(prompt):
    m = re.search(r"## What's pulling at you right now\n(.*?)(?:\n\n## |\n\nWhat do you do next)", prompt, re.S)
    assert m, prompt
    return m.group(1)


def test_section_for_a_fixed_set_of_levels(cfg, conn):
    p = brain.situation_prompt(cfg, ctx(cfg, conn))
    assert section(p) == X(cfg, "sustenance", "empty") + "\n" + X(cfg, "joy", "empty")


def test_lowest_bands_first_then_config_order(cfg):
    sel = brain.select_needs(cfg, LEVELS)
    assert sel["selected"] == ["sustenance", "joy"]                      # both empty; sustenance comes first in config
    sel = brain.select_needs(cfg, {**LEVELS, "sustenance": 60, "joy": 60})
    assert sel["selected"] == ["wonder", "curiosity"]                    # both low; config order
    sel = brain.select_needs(cfg, {**LEVELS, "wonder": 30, "peace": 20, "curiosity": 30, "sustenance": 60, "joy": 60})
    assert sel["selected"] == ["wonder", "peace"]                        # three low, tie -> config order, cap 2


def test_an_empty_band_outranks_a_low_band_even_later_in_config_order(cfg):
    levels = {"sustenance": 60, "wonder": 30, "joy": 60, "peace": 60, "curiosity": 5}      # wonder low, curiosity empty
    assert brain.select_needs(cfg, levels)["selected"] == ["curiosity", "wonder"]


def test_at_most_max_pressing_needs_are_shown(cfg):
    for n in (1, 2, 3):
        cfg.settings["max_pressing_needs"] = n
        assert len(brain.select_needs(cfg, {k: 1 for k in cfg.character["needs"]})["selected"]) == n


def test_why_each_need_was_or_was_not_selected(cfg):
    sel = brain.select_needs(cfg, LEVELS)
    rows = {r["need"]: r for r in sel["rows"]}
    assert rows["sustenance"]["selected"] and "pressing, #1 of 4" in rows["sustenance"]["reason"]
    assert not rows["wonder"]["selected"] and "only 2 are shown" in rows["wonder"]["reason"]
    assert not rows["peace"]["selected"] and rows["peace"]["reason"] == "full band: not pressing"
    assert rows["sustenance"]["band"] == "empty" and rows["wonder"]["band"] == "low"


def test_none_pressing_says_so_and_may_show_a_full_band_expression(cfg, conn):
    levels = {"sustenance": 60, "wonder": 60, "joy": 90, "peace": 60, "curiosity": 60}
    p = brain.situation_prompt(cfg, ctx(cfg, conn, needs=levels))
    assert section(p) == (cfg.prompts["brain"]["situation"]["none_pulling"] + "\n" + X(cfg, "joy", "full"))
    quiet = {k: 60 for k in cfg.character["needs"]}
    assert section(brain.situation_prompt(cfg, ctx(cfg, conn, needs=quiet))) == \
        cfg.prompts["brain"]["situation"]["none_pulling"]


def test_okay_expression_is_never_shown_even_if_present(cfg, conn):
    cfg.character["needs"]["joy"]["expressions"]["okay"] = "OKAY-TEXT"
    levels = {k: 60 for k in cfg.character["needs"]}
    assert "OKAY-TEXT" not in brain.situation_prompt(cfg, ctx(cfg, conn, needs=levels))


def test_stale_here_appears_only_when_the_flag_is_set(cfg, conn):
    stale = cfg.character["stale_here"]
    assert stale not in brain.situation_prompt(cfg, ctx(cfg, conn))
    p = brain.situation_prompt(cfg, ctx(cfg, conn, stale_here=True))
    assert section(p).endswith(stale)


def test_no_numbers_and_no_action_suggestions_in_the_section(cfg, conn):
    for levels in (LEVELS, {k: 1 for k in cfg.character["needs"]}, {k: 99 for k in cfg.character["needs"]}):
        s = section(brain.situation_prompt(cfg, ctx(cfg, conn, needs=levels, stale_here=True)))
        assert not re.search(r"\d", s)
        for bad in ("you should", "you could", "try ", "go to", "eat", "options", "suggest"):
            assert bad not in s.lower(), (bad, s)


def test_the_old_needs_list_is_gone(cfg, conn):
    p = brain.situation_prompt(cfg, ctx(cfg, conn))
    assert "Needs:" not in p
    for nid in cfg.character["needs"]:
        assert f"{nid}:" not in p


def test_changing_a_level_changes_the_words(cfg, conn):
    a = section(brain.situation_prompt(cfg, ctx(cfg, conn, needs={**LEVELS, "wonder": 5, "joy": 60})))
    assert X(cfg, "wonder", "empty") in a
    assert X(cfg, "wonder", "low") not in a


def test_the_section_heading_and_none_text_are_editable_in_yaml(cfg, conn):
    cfg.prompts["brain"]["situation"]["heading_pulling"] = "## Tugging at you"
    cfg.prompts["brain"]["situation"]["none_pulling"] = "Quiet."
    quiet = {k: 60 for k in cfg.character["needs"]}
    assert "## Tugging at you\nQuiet." in brain.situation_prompt(cfg, ctx(cfg, conn, needs=quiet))


def test_the_engine_feeds_the_prompt_real_levels(cfg, tmp_path):
    import random
    from cat.engine import Engine
    from cat.llm import MockBackend
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    seen = []
    b.handlers["brain"] = lambda m, s: (seen.append(m[-1]["content"]) or
                                        {"reaction": "", "thought": "", "action": "sit", "intent": "",
                                         "feeling": "neutral", "wants_sleep": False})
    e = Engine(cfg, b, rng=random.Random(1))
    e.state["needs"]["sustenance"] = 3.0
    e.tick()
    assert X(cfg, "sustenance", "empty") in seen[0]

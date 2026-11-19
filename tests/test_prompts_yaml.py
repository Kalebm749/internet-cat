"""config/prompts.yaml: every fixed prompt text is editable, validated and live."""
import random
import shutil

import pytest
import yaml

from cat import brain, narrator, sleep
from cat import prompts as P
from cat.config import CONFIG_DIR, ConfigError, load_config
from cat.engine import Engine
from cat.llm import MockBackend


def _edit(tmp_path, fn):
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    p = d / "prompts.yaml"
    data = yaml.safe_load(p.read_text())
    fn(data)
    p.write_text(yaml.safe_dump(data))
    return d


def _problems(tmp_path, fn):
    with pytest.raises(ConfigError) as e:
        load_config(_edit(tmp_path, fn))
    return str(e.value)


def test_default_prompts_have_every_entry_and_no_unknown_placeholders(cfg):
    errs, warns = P.check(cfg.prompts)
    assert errs == [] and warns == []
    assert all(P.lookup(cfg.prompts, k) for k in P.SPEC)


def test_missing_entry_refused(tmp_path):
    assert "missing required entry 'brain.system.intro'" in _problems(
        tmp_path, lambda d: d["brain"]["system"].pop("intro"))


def test_empty_entry_refused(tmp_path):
    assert "'narrator.user': must be non-empty text" in _problems(
        tmp_path, lambda d: d["narrator"].update(user="   "))


def test_unknown_placeholder_refused_and_names_the_allowed_ones(tmp_path):
    msg = _problems(tmp_path, lambda d: d["brain"]["system"].update(intro="You are {nmae}."))
    assert "unknown placeholder {nmae}" in msg and "{name}" in msg


def test_bad_braces_refused(tmp_path):
    assert "bad braces" in _problems(tmp_path, lambda d: d["brain"]["system"].update(intro="You are {name"))


def test_every_problem_reported_at_once(tmp_path):
    def bad(d):
        d["brain"]["system"].pop("intro")
        d["narrator"].update(user="")
        d["sleep"].update(system="{who}")
    msg = _problems(tmp_path, bad)
    assert msg.startswith("4 config problems") or msg.startswith("3 config problems") or "config problems" in msg
    assert "brain.system.intro" in msg and "narrator.user" in msg and "{who}" in msg


def test_unknown_top_level_field_is_a_warning(tmp_path, caplog):
    d = _edit(tmp_path, lambda p: p.update(extras={"x": 1}))
    with caplog.at_level("WARNING", logger="cat.config"):
        load_config(d)
    assert "prompts.yaml: unknown top-level field 'extras'" in caplog.text


def test_missing_prompts_file_refused(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    (d / "prompts.yaml").unlink()
    with pytest.raises(ConfigError, match="missing config file"):
        load_config(d)


def test_editing_the_yaml_changes_the_prompts(tmp_path):
    def edit(d):
        d["brain"]["system"]["intro"] = "Hello {name}, this is custom."
        d["brain"]["situation"]["closing"] = "Go on."
        d["narrator"]["user"] = "{context}\n--\nDid: {action}"
        d["sleep"]["jobs"]["consolidate"] = "Custom job."
        d["sleep"]["consolidate"] = "Memories: {shorts} | {log} | {name} | {n}"
    cfg = load_config(_edit(tmp_path, edit))
    assert brain.system_prompt(cfg).startswith(f"Hello {cfg.character['name']}, this is custom.")
    assert narrator.build_messages(cfg, "eat", "CTX")[1]["content"] == "CTX\n--\nDid: eat"
    msgs = sleep.consolidate_messages(cfg, ["a"], ["b"])
    assert "Custom job." in msgs[0]["content"] and msgs[1]["content"].startswith("Memories: - a | - b |")


def test_literal_braces_survive_in_the_narrator_prompt(cfg):
    s = narrator.system_prompt(cfg)
    assert "move_self {area}" in s and "create {name, tags, area}" in s


def test_the_situation_prompt_text_is_editable(tmp_path, conn):
    from cat.world import WorldState
    cfg = load_config(_edit(tmp_path, lambda d: d["brain"]["situation"].update(heading_who="## About you")))
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    ctx = brain.BrainContext(sim_time="2026-11-18T19:40", area_id="living_room",
                             needs={k: 50.0 for k in cfg.character["needs"]}, energy=40.0,
                             mood={"description": "calm", "word": "calm"}, tendencies={}, weather="rain",
                             objects_here=w.here("living_room"), inventory=[])
    assert brain.situation_prompt(cfg, ctx).startswith("## About you")


def test_reload_applies_edited_prompts_live(cfg):
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    e = Engine(cfg, b, rng=random.Random(1))
    path = cfg.config_dir / "prompts.yaml"
    data = yaml.safe_load(path.read_text())
    data["brain"]["situation"]["closing"] = "Well?"
    path.write_text(yaml.safe_dump(data))
    e.reload_settings()
    assert cfg.prompts["brain"]["situation"]["closing"] == "Well?"


def test_prompts_contain_no_need_numbers_or_action_lists(cfg):
    text = yaml.safe_dump(cfg.prompts).lower()
    assert "you could" not in text and "options:" not in text and "choose one" not in text

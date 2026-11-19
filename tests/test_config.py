import shutil

import pytest
import yaml

from cat.config import CONFIG_DIR, ConfigError, load_config


def _edit(tmp_path, fname, fn):
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    p = d / fname
    data = yaml.safe_load(p.read_text())
    fn(data)
    p.write_text(yaml.safe_dump(data))
    return d


def test_default_config_loads():
    c = load_config()
    assert c.character["name"]
    assert set(c.settings["gain_values"]) == {"0", "+", "++"}


def test_energy_need_rejected(tmp_path):
    d = _edit(tmp_path, "character.yaml",
              lambda c: c["needs"].update(energy={"start": 50, "decay": 1}))
    with pytest.raises(ConfigError, match="derived"):
        load_config(d)


def test_needs_flags_required(tmp_path):
    d = _edit(tmp_path, "character.yaml", lambda c: c["needs"]["rest"].pop("sleep_restore"))
    with pytest.raises(ConfigError, match="sleep_restore"):
        load_config(d)


def test_unmirrored_exit(tmp_path):
    d = _edit(tmp_path, "world.yaml", lambda w: w["areas"]["kitchen"].update(exits=[]))
    with pytest.raises(ConfigError, match="not mirrored"):
        load_config(d)


def test_unknown_object_area(tmp_path):
    d = _edit(tmp_path, "world.yaml", lambda w: w["objects"][0].update(area="nowhere"))
    with pytest.raises(ConfigError, match="unknown area"):
        load_config(d)


def test_missing_setting(tmp_path):
    d = _edit(tmp_path, "settings.yaml", lambda s: s.pop("recall"))
    with pytest.raises(ConfigError, match="recall"):
        load_config(d)


# ---- validator: every check, plus "all problems at once" and warnings ----
def _problems(tmp_path, fname, fn):
    d = _edit(tmp_path, fname, fn)
    with pytest.raises(ConfigError) as e:
        load_config(d)
    return str(e.value)


def test_default_character_has_no_form():
    c = load_config().character
    assert not {"species", "appearance", "portrait"} & set(c)
    assert c["self_image"] == {"seed": ""}


def test_event_need_effect_must_name_a_real_need(tmp_path):
    msg = _problems(tmp_path, "world.yaml",
                    lambda w: w["events"].append({"text": "A feast.", "chance": 0.1, "needs": {"hunger": "+"}}))
    assert "unknown need 'hunger'" in msg


def test_event_need_effect_with_real_need_is_fine(tmp_path):
    d = _edit(tmp_path, "world.yaml",
              lambda w: w["events"].append({"text": "A feast.", "chance": 0.1, "needs": {"food": "+"}}))
    load_config(d)


def test_tendency_labels_must_match_tendencies(tmp_path):
    msg = _problems(tmp_path, "character.yaml",
                    lambda c: c["tendency_labels"].update(luck=["unlucky", "lucky"]))
    assert "tendency_labels: unknown tendency 'luck'" in msg


def test_tendency_labels_must_be_a_pair(tmp_path):
    msg = _problems(tmp_path, "character.yaml", lambda c: c["tendency_labels"].update(boldness=["timid"]))
    assert "[low_label, high_label]" in msg


def test_start_area_must_exist(tmp_path):
    msg = _problems(tmp_path, "character.yaml", lambda c: c.update(start_area="moon"))
    assert "start_area 'moon' is not an area" in msg


def test_unknown_behaviour_flag_rejected(tmp_path):
    msg = _problems(tmp_path, "character.yaml", lambda c: c["needs"]["fun"].update(sparkly=True))
    assert "unknown behaviour flag or field 'sparkly'" in msg


def test_behaviour_flag_must_be_boolean(tmp_path):
    msg = _problems(tmp_path, "character.yaml", lambda c: c["needs"]["food"].update(requires_consume="yes"))
    assert "requires_consume must be true or false" in msg


def test_numeric_ranges(tmp_path):
    def bad(c):
        c["needs"]["fun"]["start"] = 150
        c["needs"]["fun"]["decay"] = -1
        c["tendencies"]["boldness"] = 1.5
    msg = _problems(tmp_path, "character.yaml", bad)
    assert "need 'fun': start must be 0..100" in msg and "decay must be a number >= 0" in msg
    assert "tendency 'boldness': must be 0..1" in msg


def test_event_ranges(tmp_path):
    msg = _problems(tmp_path, "world.yaml",
                    lambda w: w["events"].append({"text": "Odd.", "chance": 2, "hours": [8, 30], "mood": 5}))
    assert "chance must be 0..1" in msg and "hours must be" in msg and "mood must be" in msg


@pytest.mark.parametrize("value,expect", [
    ({"seed": ["a", "b"]}, "self_image.seed: must be text only"),
    ({"seed": "", "model": "x.glb"}, "self_image must be a mapping with only 'seed'"),
    ("a small grey cat", "self_image must be a mapping"),
])
def test_self_image_must_be_text_only(tmp_path, value, expect):
    assert expect in _problems(tmp_path, "character.yaml", lambda c: c.update(self_image=value))


def test_self_image_seed_text_accepted(tmp_path):
    d = _edit(tmp_path, "character.yaml", lambda c: c.update(self_image={"seed": "I might be small."}))
    assert load_config(d).character["self_image"]["seed"] == "I might be small."


def test_every_problem_is_reported_at_once(tmp_path):
    def bad(c):
        c.update(start_area="moon")
        c["tendency_labels"].update(luck=["a", "b"])
        c["needs"]["fun"]["start"] = -5
    msg = _problems(tmp_path, "character.yaml", bad)
    assert msg.startswith("3 config problems") and msg.count("\n  - ") == 3


def test_unknown_top_level_field_is_a_warning(tmp_path, caplog):
    d = _edit(tmp_path, "character.yaml", lambda c: c.update(species="a small grey cat", mood_ring=1))
    with caplog.at_level("WARNING", logger="cat.config"):
        cfg = load_config(d)
    assert cfg.character["name"]
    text = caplog.text
    assert "unknown top-level field 'species'" in text and "'mood_ring'" in text


def test_main_refuses_to_start_and_prints_everything(tmp_path, capsys):
    from cat.main import main
    d = _edit(tmp_path, "character.yaml", lambda c: c.update(start_area="moon", pronoun="she",
                                                           tendency_labels={"luck": ["a", "b"]}))
    assert main(["--mock", "--ticks", "1", "--config-dir", str(d)]) == 2
    err = capsys.readouterr().err
    assert "refusing to start" in err and "start_area" in err and "luck" in err

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






def test_unmirrored_exit(tmp_path):
    d = _edit(tmp_path, "world.yaml", lambda w: w["areas"]["clock_tower"].update(exits=[]))
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






def test_tendency_labels_must_match_tendencies(tmp_path):
    msg = _problems(tmp_path, "character.yaml",
                    lambda c: c.setdefault("tendency_labels", {}).update(luck=["unlucky", "lucky"]))
    assert "tendency_labels: unknown tendency 'luck'" in msg


def test_tendency_labels_must_be_a_pair(tmp_path):
    msg = _problems(tmp_path, "character.yaml", lambda c: c.setdefault("tendency_labels", {}).update(boldness=["timid"]))
    assert "[low_label, high_label]" in msg


def test_start_area_must_exist(tmp_path):
    msg = _problems(tmp_path, "character.yaml", lambda c: c.update(start_area="moon"))
    assert "start_area 'moon' is not an area" in msg








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


# ---- needs: meaning, expressions, bands, drains ----
def test_default_needs_are_the_five_with_meaning_and_expressions():
    needs = load_config().character["needs"]
    assert list(needs) == ["sustenance", "wonder", "joy", "peace", "curiosity"]
    for n in needs.values():
        assert set(n) == {"start", "decay_per_hour", "meaning", "expressions"}
        assert all(n["expressions"][b] for b in ("empty", "low", "full"))
    assert load_config().character["stale_here"]


def test_energy_is_not_a_need(tmp_path):
    msg = _problems(tmp_path, "character.yaml", lambda c: c["needs"].update(
        energy=dict(c["needs"]["joy"])))
    assert "'energy' is not a need" in msg


def test_old_need_fields_are_unknown(tmp_path):
    msg = _problems(tmp_path, "character.yaml", lambda c: c["needs"]["joy"].update(
        requires_consume=True, energy_weight=1, decay=3, label="x"))
    for f in ("requires_consume", "energy_weight", "decay", "label"):
        assert f"unknown field '{f}'" in msg


def test_event_drains_must_name_a_real_need(tmp_path):
    msg = _problems(tmp_path, "world.yaml", lambda w: w["events"].append(
        {"text": "Bad news.", "chance": 0.1, "drains": {"hunger": 10}}))
    assert "drains references unknown need 'hunger'" in msg


def test_event_drains_with_real_needs_are_fine(tmp_path):
    d = _edit(tmp_path, "world.yaml", lambda w: w["events"].append(
        {"text": "Bad news.", "chance": 0.1, "drains": {"peace": 10, "joy": 5}}))
    assert load_config(d)


def test_event_drain_amount_must_be_a_non_negative_number(tmp_path):
    msg = _problems(tmp_path, "world.yaml", lambda w: w["events"].append(
        {"text": "Odd.", "chance": 0.1, "drains": {"peace": -5, "joy": "lots"}}))
    assert msg.count("must be a number >= 0") == 2


@pytest.mark.parametrize("band", ["empty", "low", "full"])
def test_expressions_required_for_empty_low_and_full(tmp_path, band):
    msg = _problems(tmp_path, "character.yaml", lambda c: c["needs"]["joy"]["expressions"].pop(band))
    assert f"need 'joy': expressions.{band} is required" in msg


def test_okay_expression_may_be_null_or_text_but_not_blank(tmp_path):
    load_config(_edit(tmp_path, "character.yaml",
                      lambda c: c["needs"]["joy"]["expressions"].update(okay="Mostly light.")))
    msg = _problems(tmp_path / "b", "character.yaml",
                    lambda c: c["needs"]["joy"]["expressions"].update(okay="  "))
    assert "expressions.okay must be null or non-empty text" in msg


def test_unknown_band_rejected(tmp_path):
    msg = _problems(tmp_path, "character.yaml", lambda c: c["needs"]["joy"]["expressions"].update(great="x"))
    assert "unknown band 'great'" in msg


@pytest.mark.parametrize("where", ["meaning", "empty", "low", "full"])
def test_any_present_text_must_be_non_empty(tmp_path, where):
    def blank(c):
        n = c["needs"]["peace"]
        if where == "meaning":
            n["meaning"] = " "
        else:
            n["expressions"][where] = ""
    assert "non-empty text" in _problems(tmp_path, "character.yaml", blank)


def test_stale_here_must_be_non_empty_when_present(tmp_path):
    assert "stale_here must be non-empty text" in _problems(
        tmp_path, "character.yaml", lambda c: c.update(stale_here=" "))


def test_need_numbers_ranges(tmp_path):
    def bad(c):
        c["needs"]["joy"]["start"] = 150
        c["needs"]["joy"]["decay_per_hour"] = -1
        c["tendencies"]["boldness"] = 1.5
    msg = _problems(tmp_path, "character.yaml", bad)
    assert "need 'joy': start must be 0..100" in msg and "decay_per_hour must be a number >= 0" in msg
    assert "tendency 'boldness': must be 0..1" in msg


def test_band_thresholds_must_ascend(tmp_path):
    msg = _problems(tmp_path, "settings.yaml", lambda s: s["need_bands"].update(empty_below=50, low_below=40))
    assert "thresholds must ascend" in msg


def test_band_thresholds_must_be_complete_numbers_in_range(tmp_path):
    assert "must be numbers for empty_below, low_below, okay_below" in _problems(
        tmp_path, "settings.yaml", lambda s: s["need_bands"].pop("low_below"))
    assert "thresholds must be 0..100" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["need_bands"].update(okay_below=150))


def test_pressing_and_stale_settings_are_validated(tmp_path):
    assert "max_pressing_needs: must be an integer >= 1" in _problems(
        tmp_path, "settings.yaml", lambda s: s.update(max_pressing_needs=0))
    assert "stale_after_ticks: must be an integer >= 0" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s.update(stale_after_ticks=-1))


def test_sustenance_need_and_repetition_needs_must_exist(tmp_path):
    assert "'hunger' is not a need" in _problems(
        tmp_path, "settings.yaml", lambda s: s.update(sustenance_need="hunger"))
    assert "repetition_penalty.needs: 'fun' is not a need" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["repetition_penalty"].update(needs=["wonder", "fun"]))


def test_energy_start_range(tmp_path):
    assert "energy_start: must be a number -50..50" in _problems(
        tmp_path, "settings.yaml", lambda s: s.update(energy_start=80))


def test_every_problem_is_reported_at_once(tmp_path):
    def bad(c):
        c.update(start_area="moon")
        c.setdefault("tendency_labels", {}).update(luck=["a", "b"])
        c["needs"]["joy"]["start"] = -5
    msg = _problems(tmp_path, "character.yaml", bad)
    assert msg.startswith("3 config problems") and msg.count("\n  - ") == 3



def test_dislike_drain_settings_are_validated(tmp_path):
    assert "dislike_drain.needs: 'fun' is not a need" in _problems(
        tmp_path, "settings.yaml", lambda s: s["dislike_drain"].update(needs=["peace", "fun"]))
    assert "dislike_drain.points_per_hour: must be a number >= 0" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["dislike_drain"].update(points_per_hour=-1))
    assert "dislike_drain.max_matches: must be an integer >= 1" in _problems(
        tmp_path / "c", "settings.yaml", lambda s: s["dislike_drain"].update(max_matches=0))


def test_the_placeholder_world_has_an_event_with_drains():
    events = load_config().world["events"]
    assert any(e.get("drains") for e in events)


# ---- mood mapping ----
def test_mood_mapping_is_validated(tmp_path):
    assert "mood.bands: 'fun' is not a need" in _problems(
        tmp_path, "settings.yaml", lambda s: s["mood"]["bands"].update(fun={"full": 5}))
    assert "unknown band 'great'" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["mood"]["bands"]["joy"].update(great=1))
    assert "mood.bands.joy.full: must be a number -50..50" in _problems(
        tmp_path / "c", "settings.yaml", lambda s: s["mood"]["bands"]["joy"].update(full=90))


def test_mood_numbers_are_validated(tmp_path):
    def bad(s):
        s["mood"].update(event_decay=2, word_threshold=80, event_points="x")
    msg = _problems(tmp_path, "settings.yaml", bad)
    for part in ("event_decay: must be a number 0..1", "word_threshold: must be a number 0..50",
                 "event_points: must be a number"):
        assert part in msg


def test_there_is_no_arousal_setting():
    mood = load_config().settings["mood"]
    assert not any("arousal" in k for k in mood) and "valence_bands" not in mood


def test_artifacts_enabled_and_work_bucket_are_validated(tmp_path):
    assert "projects.artifacts_enabled: must be true or false" in _problems(
        tmp_path, "settings.yaml", lambda s: s["projects"].update(artifacts_enabled="yes"))
    assert "projects.work_bucket: must be a key of progress_values" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["projects"].update(work_bucket="+++"))

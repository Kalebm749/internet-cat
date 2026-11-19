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

import shutil
from pathlib import Path

import pytest

from cat import db
from cat.config import CONFIG_DIR, load_config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    # tests use a fixed world (the old house), so they do not depend on the default content of config/world.yaml
    shutil.copy(FIXTURES / "world_house.yaml", d / "world.yaml")
    shutil.copy(FIXTURES / "moves_core.yaml", d / "moves.yaml")     # the 12 original verbs; the default file has about 100
    shutil.copy(FIXTURES / "character_house.yaml", d / "character.yaml")   # with the three predefined tendencies
    c = load_config(d)
    c.settings["gain_values"] = {"0": 0, "+": 15, "++": 30}      # tests keep the original bucket sizes
    c.settings["db_path"] = str(tmp_path / "cat.db")
    c.settings["activity"]["enabled"] = False       # most tests want one decision per tick; test_activity turns it on
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_db(c)
    return c

import shutil
from pathlib import Path

import pytest

from cat import db
from cat.config import CONFIG_DIR, load_config


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    c = load_config(d)
    c.settings["db_path"] = str(tmp_path / "cat.db")
    c.settings["activity"]["enabled"] = False       # most tests want one decision per tick; test_activity turns it on
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_db(c)
    return c

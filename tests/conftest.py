import pytest

from cat.config import load_config


@pytest.fixture
def cfg(tmp_path):
    """Engine-wide settings with every file pointed into the test's own directory."""
    c = load_config()
    c.settings["nit"]["db_path"] = str(tmp_path / "nit.db")
    c.settings["web"]["mail_db"] = str(tmp_path / "mail.db")
    c.settings["heartbeat_file"] = str(tmp_path / "engine.heartbeat")
    c.settings["log_dir"] = str(tmp_path / "logs")
    return c

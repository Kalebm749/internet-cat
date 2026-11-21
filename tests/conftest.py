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


@pytest.fixture(autouse=True)
def classic_menu(monkeypatch):
    """Most tests were written for a menu that offers everything and replies that are never a gut answer; the drawn
    menu and the gut tier are tested explicitly in test_individuals.py."""
    from cat import tuning
    real = tuning.load

    def load(*a, **k):
        t = real(*a, **k)
        t["decisions"]["menu_size"] = 0
        t["inner"]["gut_scale"] = 0.0
        return t
    monkeypatch.setattr(tuning, "load", load)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach the internet (a real search would make a mock day depend on the world)."""
    import urllib.error
    import urllib.request

    def refuse(*a, **k):
        raise urllib.error.URLError("tests do not use the network")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)

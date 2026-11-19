"""The docs and README must match the single-host Docker Compose setup (phase 2, part A)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()


def test_old_docs_are_gone_and_renamed():
    assert (ROOT / "docs" / "server-architecture.md").exists()
    assert not (ROOT / "docs" / "pi-architecture.md").exists()
    assert not (ROOT / "docs" / "laptop-brain.md").exists()


def test_no_stale_references():
    skip = {"phase-2-docker-and-devdash.md"}          # the plan itself talks about the old names
    offenders = []
    for p in list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("*.md")) + list((ROOT / "cat").rglob("*.py")) \
            + list((ROOT / "config").glob("*.yaml")) + list((ROOT / "deploy").glob("*")) + [ROOT / "compose.yaml"]:
        if p.name in skip:
            continue
        text = p.read_text()
        for bad in ("pi-architecture", "laptop-brain", "Raspberry Pi 3", "192.168.1.50", "cat-engine.service"):
            if bad in text:
                offenders.append(f"{p.relative_to(ROOT)}: {bad}")
    assert not offenders, offenders


def test_readme_covers_the_host_prerequisites():
    for needle in ("ubuntu-drivers", "nvidia-smi", "docker compose version", "NVIDIA Container Toolkit",
                   "sudo nvidia-ctk runtime configure --runtime=docker", "systemctl restart docker",
                   "docker run --rm --gpus all nvidia/cuda:", "sudo systemctl disable --now ollama",
                   "systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target",
                   "HandleLidSwitch=ignore", "HandleLidSwitchExternalPower=ignore", "HandleLidSwitchDocked=ignore",
                   "ufw allow from", "port 8080", "battery", "sqlite3"):
        assert needle in README, needle


def test_readme_covers_operations():
    for needle in ("docker compose up -d --build", "deploy/pull-models.sh", "deploy/backup.sh", "deploy/update.sh",
                   "Restore", "downtime_policy", "catch_up", "pause", ".env", "CAT_UID", "http://<laptop-ip>:8080",
                   "docker compose logs -f engine"):
        assert needle in README, needle


def test_readme_links_resolve_and_commands_reference_real_files():
    for target in re.findall(r"\]\(([^)#]+)\)", README):
        if not target.startswith("http"):
            assert (ROOT / target).exists(), target
    for script in re.findall(r"(?:deploy|scripts)/[\w-]+\.(?:sh|py)", README):
        assert (ROOT / script).exists(), script


def test_server_architecture_has_host_setup_and_compose():
    doc = (ROOT / "docs" / "server-architecture.md").read_text()
    for needle in ("## 14. Host setup", "GTX 1060", "Docker Compose", "NVIDIA Container Toolkit", "downtime_policy",
                   "Battery", "ufw", "HandleLidSwitch"):
        assert needle in doc, needle
    for gone in ("1GB RAM", "SD card", "microSD", "Raspberry Pi Imager", "systemctl restart cat-"):
        assert gone not in doc, gone


def test_hardware_references_updated():
    assert "Raspberry Pi" not in (ROOT / "docs" / "creature-architecture.md").read_text()
    web = (ROOT / "docs" / "web-gui.md").read_text()
    assert "Pi 3B+" not in web and "Pi-hosted" not in web
    assert "docker" in (ROOT / "CLAUDE.md").read_text().lower()


# ---------- the behavior guide ----------
def _settings_path_exists(settings, dotted):
    cur = settings
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def test_behavior_guide_covers_the_essentials():
    doc = (ROOT / "docs" / "behavior-guide.md").read_text()
    for needle in ("Anatomy of one tick", "Master table", "Tuning guide", "Resetting the simulation",
                   "Full reset", "Partial resets", "data/cat.db", "What's pulling at you right now",
                   "sustenance rule", "never subtracts", "separate resource", "−50 to 50",
                   "downtime_policy", "Reload settings", "prompts.yaml"):
        assert needle in doc, needle
    assert "docs/behavior-guide.md" in README and "behavior-guide.md" in (ROOT / "CLAUDE.md").read_text()


def test_every_setting_the_behavior_guide_names_exists():
    import yaml
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
    doc = (ROOT / "docs" / "behavior-guide.md").read_text()
    bad = []
    for tok in set(re.findall(r"`([a-z_]+(?:\.[a-z_*]+)+)`", doc)):
        head = tok.split(".")[0]
        if head in settings and "*" not in tok and not _settings_path_exists(settings, tok):
            bad.append(tok)
    assert not bad, bad


def test_behavior_guide_reset_commands_match_the_real_tables():
    import sqlite3
    from cat import db
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    doc = (ROOT / "docs" / "behavior-guide.md").read_text()
    for sql in re.findall(r"`((?:DELETE FROM|UPDATE) [^`]+;)`", doc):
        if sql.startswith("DELETE FROM") or "json_object" in sql or "state SET" in sql or "meta SET" in sql:
            conn.execute(sql)                     # every documented statement must be valid against the schema

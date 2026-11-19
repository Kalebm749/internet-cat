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

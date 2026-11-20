"""Engine-wide settings: config/settings.yaml (everything outside the `nit:` block, which cat/tuning.py checks).

World content lives in content/ (cat/content.py), the creature's tuning in the `nit:` block (cat/tuning.py).
`load_config` validates the engine-wide keys and reports every problem at once. CAT_CONFIG_DIR may point at another
config directory (the containers mount it read-only at /app/config).
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("CAT_CONFIG_DIR") or ROOT / "config")

REQUIRED = {
    "ollama_host": str, "ollama_think": bool, "llm_timeout_seconds": (int, float), "health_timeout_seconds": (int, float),
    "llm_temperature": dict, "llm_num_ctx": int, "heartbeat_file": str, "log_dir": str, "log_level": str,
    "log_max_bytes": int, "log_backup_count": int, "backup_keep": int, "healthcheck": dict,
    "pull_timeout_seconds": (int, float), "web": dict, "devdash": dict, "nit": dict,
}
WEB_KEYS = {"port", "refresh_seconds", "diary_page", "mail_db", "mail_max_chars", "mail_name_max",
            "mail_per_ip_per_hour", "mail_per_hour", "mail_poll_seconds", "mail_blocklist"}


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    settings: dict[str, Any]
    config_dir: Path = CONFIG_DIR
    root: Path = ROOT
    overrides: dict | None = field(default=None)

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    @property
    def nit(self) -> dict:
        return self.settings["nit"]

    @property
    def db_path(self) -> Path:
        return self.path(self.settings["nit"]["db_path"])

    @property
    def mail_db_path(self) -> Path:
        return self.path(self.settings["web"]["mail_db"])

    @property
    def heartbeat_path(self) -> Path:
        return self.path(self.settings["heartbeat_file"])

    @property
    def log_dir(self) -> Path:
        return self.path(self.settings["log_dir"])


def validate(s: Any) -> list[str]:
    if not isinstance(s, dict):
        return ["settings.yaml: the top level must be a mapping"]
    probs = []
    for k, t in REQUIRED.items():
        if k not in s:
            probs.append(f"settings.yaml: missing '{k}'")
        elif not isinstance(s[k], t) or (t in (int, (int, float)) and isinstance(s[k], bool)):
            probs.append(f"settings.yaml: '{k}' has the wrong type")
    hc = s.get("healthcheck")
    if isinstance(hc, dict) and not {"engine_max_age_seconds", "http_timeout_seconds"} <= set(hc):
        probs.append("settings.yaml: healthcheck needs engine_max_age_seconds and http_timeout_seconds")
    web = s.get("web")
    if isinstance(web, dict):
        for k in sorted(WEB_KEYS - set(web)):
            probs.append(f"settings.yaml: web: missing '{k}'")
        if "mail_blocklist" in web and not isinstance(web["mail_blocklist"], list):
            probs.append("settings.yaml: web.mail_blocklist must be a list")
    return probs


def load_config(config_dir: Path | str | None = None, overrides: dict | None = None) -> Config:
    d = Path(config_dir) if config_dir else CONFIG_DIR
    try:
        s = yaml.safe_load((d / "settings.yaml").read_text())
    except FileNotFoundError:
        raise ConfigError(f"missing config file: {d / 'settings.yaml'}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"settings.yaml: invalid YAML: {e}") from None
    probs = validate(s)
    if probs:
        raise ConfigError("\n".join(probs))
    s = copy.deepcopy(s)
    for k, v in (overrides or {}).items():
        if v is not None:
            s[k] = v
    return Config(settings=s, config_dir=d, overrides=overrides)

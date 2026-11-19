"""Load and validate config/{settings,character,world}.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    settings: dict[str, Any]
    character: dict[str, Any]
    world: dict[str, Any]
    root: Path = ROOT

    def path(self, key: str) -> Path:
        p = Path(self.settings[key])
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        return self.path("db_path")

    @property
    def heartbeat_path(self) -> Path:
        return self.path("heartbeat_file")


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"missing config file: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name}: invalid YAML: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: top level must be a mapping")
    return data


def _require(d: dict, keys: list[str], where: str) -> None:
    for k in keys:
        if k not in d:
            raise ConfigError(f"{where}: missing required field '{k}'")


REQUIRED_SETTINGS = [
    "ollama_host", "brain_model", "narrator_model", "sleep_model", "embed_model",
    "db_path", "tick_interval_seconds", "sim_start", "sim_minutes_per_tick_default",
    "sim_minutes_min", "sim_minutes_max", "phases", "gain_values", "effort_energy_cost",
    "repetition_penalty", "create_limit_per_tick", "allowed_tags", "mood", "doze",
    "sleep", "recall", "notable", "default_need_words", "energy_words",
    "events_max_per_tick", "max_changes_per_tick", "inventory_max", "name_max_length",
    "consume_min_gain", "llm_temperature", "llm_num_ctx", "llm_retries", "error_backoff_seconds", "web_ui", "heartbeat_file", "healthcheck", "health_require_models",
    "pull_timeout_seconds", "backup_keep", "downtime_policy", "max_catch_up_hours",
    "catch_up_min_gap_ticks", "catch_up_decay_multiplier", "tendency_words", "max_objects_per_area",
]


def validate(settings: dict, character: dict, world: dict) -> None:
    _require(settings, REQUIRED_SETTINGS, "settings.yaml")
    _require(character, ["name", "species", "core_traits", "tendencies", "needs", "start_area"],
             "character.yaml")
    _require(world, ["areas", "objects"], "world.yaml")

    areas = world["areas"]
    if not areas:
        raise ConfigError("world.yaml: needs at least one area")
    for aid, a in areas.items():
        _require(a, ["name", "description", "exits"], f"world.yaml area '{aid}'")
        for ex in a["exits"]:
            if ex not in areas:
                raise ConfigError(f"world.yaml area '{aid}': exit '{ex}' is not an area")
            if aid not in areas[ex]["exits"]:
                raise ConfigError(
                    f"world.yaml: exit '{aid}' -> '{ex}' is not mirrored ('{ex}' has no exit back)")
    if character["start_area"] not in areas:
        raise ConfigError(f"character.yaml: start_area '{character['start_area']}' is not an area")

    ids = set()
    for o in world["objects"]:
        _require(o, ["id", "name", "area", "tags"], "world.yaml object")
        if o["id"] in ids:
            raise ConfigError(f"world.yaml: duplicate object id '{o['id']}'")
        ids.add(o["id"])
        if o["area"] not in areas and o["area"] != "inventory":
            raise ConfigError(f"world.yaml object '{o['id']}': unknown area '{o['area']}'")

    weather = world.get("weather") or ["clear"]
    for ev in world.get("events", []):
        _require(ev, ["text", "chance"], "world.yaml event")
        if not 0 <= ev["chance"] <= 1:
            raise ConfigError(f"world.yaml event '{ev['text']}': chance must be 0..1")
        if "area" in ev and ev["area"] not in areas:
            raise ConfigError(f"world.yaml event '{ev['text']}': unknown area '{ev['area']}'")
        if "set_weather" in ev and ev["set_weather"] not in weather:
            raise ConfigError(f"world.yaml event '{ev['text']}': unknown weather")
        if "spawn" in ev:
            _require(ev["spawn"], ["name", "tags", "area"], "world.yaml event spawn")

    needs = character["needs"]
    if not needs:
        raise ConfigError("character.yaml: needs must not be empty")
    for nid, n in needs.items():
        _require(n, ["start", "decay"], f"character.yaml need '{nid}'")
        if not 0 <= n["start"] <= 100:
            raise ConfigError(f"character.yaml need '{nid}': start must be 0..100")
    for flag in ("effort_drain", "sleep_restore"):
        flagged = [k for k, n in needs.items() if n.get(flag)]
        if len(flagged) != 1:
            raise ConfigError(
                f"character.yaml: exactly one need must set {flag}: true (found {flagged})")
    if not any(n.get("energy_weight", 0) > 0 for n in needs.values()):
        raise ConfigError("character.yaml: at least one need needs energy_weight > 0 "
                          "(energy is derived from the needs)")
    if "energy" in needs:
        raise ConfigError("character.yaml: 'energy' is derived, not a need; "
                          "use energy_weight on needs (e.g. a 'rest' need)")
    for t, v in character["tendencies"].items():
        if not 0 <= v <= 1:
            raise ConfigError(f"character.yaml tendency '{t}': must be 0..1")
    for t in character.get("tendency_labels", {}):
        if t not in character["tendencies"]:
            raise ConfigError(f"character.yaml tendency_labels: unknown tendency '{t}'")

    if settings["downtime_policy"] not in ("pause", "catch_up"):
        raise ConfigError(f"settings.yaml: downtime_policy must be 'pause' or 'catch_up', "
                          f"got {settings['downtime_policy']!r}")
    if settings["max_catch_up_hours"] <= 0:
        raise ConfigError("settings.yaml: max_catch_up_hours must be > 0")
    if settings["sim_minutes_min"] > settings["sim_minutes_max"]:
        raise ConfigError("settings.yaml: sim_minutes_min > sim_minutes_max")
    for b in ("0", "+", "++"):
        if b not in {str(k) for k in settings["gain_values"]}:
            raise ConfigError(f"settings.yaml gain_values: missing bucket '{b}'")


def load_config(config_dir: Path | str | None = None, overrides: dict | None = None) -> Config:
    d = Path(config_dir or os.environ.get("CAT_CONFIG_DIR") or CONFIG_DIR)
    settings = _load_yaml(d / "settings.yaml")
    if overrides:
        settings.update(overrides)
    character = _load_yaml(d / "character.yaml")
    world = _load_yaml(d / "world.yaml")
    validate(settings, character, world)
    # YAML may parse bucket keys as strings already ("0"); normalise
    settings["gain_values"] = {str(k): v for k, v in settings["gain_values"].items()}
    return Config(settings, character, world, root=d.resolve().parent)

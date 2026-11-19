"""Load and validate config/{settings,character,world}.yaml."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
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
    prompts: dict[str, Any] = field(default_factory=dict)   # config/prompts.yaml: every fixed prompt text
    root: Path = ROOT
    config_dir: Path = CONFIG_DIR           # where the YAML was loaded from (for reload_settings)
    overrides: dict | None = None           # CLI overrides applied on top of settings.yaml

    def path(self, key: str) -> Path:
        p = Path(self.settings[key])
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        return self.path("db_path")

    @property
    def dev_db_path(self) -> Path:
        return self.path("dev_db_path")

    @property
    def log_dir(self) -> Path:
        return self.path("log_dir")

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


REQUIRED_SETTINGS = [
    "ollama_host", "brain_model", "narrator_model", "sleep_model", "embed_model",
    "db_path", "tick_interval_seconds", "sim_start", "sim_minutes_per_tick_default",
    "sim_minutes_min", "sim_minutes_max", "phases", "gain_values", "effort_energy_cost",
    "repetition_penalty", "create_limit_per_tick", "allowed_tags", "mood", "doze",
    "sleep", "recall", "notable", "default_need_words", "energy_words",
    "events_max_per_tick", "max_changes_per_tick", "inventory_max", "name_max_length",
    "consume_min_gain", "llm_temperature", "llm_num_ctx", "llm_retries", "error_backoff_seconds", "web_ui", "heartbeat_file", "healthcheck", "health_require_models",
    "pull_timeout_seconds", "backup_keep", "downtime_policy", "max_catch_up_hours",
    "catch_up_min_gap_ticks", "catch_up_decay_multiplier", "trace_enabled", "trace_level",
    "trace_retention_days", "trace_recall_candidates", "log_dir", "log_level", "log_max_bytes",
    "log_backup_count", "dev_db_path", "dev_commands_enabled", "dev_poll_seconds",
    "paused_alive_seconds", "devdash", "tendency_words", "max_objects_per_area",
]


CHARACTER_FIELDS = {"name", "pronoun", "core_traits", "tendencies", "tendency_labels", "needs",
                    "start_area", "voice", "backstory", "self_image"}
WORLD_FIELDS = {"areas", "objects", "weather", "events"}
NEED_FIELDS = {"start", "decay", "label", "words", "requires_consume", "energy_weight",
               "effort_drain", "sleep_restore"}
NEED_FLAGS = {"requires_consume", "effort_drain", "sleep_restore"}   # boolean behaviour flags
EVENT_FIELDS = {"text", "chance", "area", "hours", "only_weather", "set_weather", "mood", "spawn", "needs"}


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check(settings: dict, character: dict, world: dict) -> tuple[list[str], list[str]]:
    """Every problem in the three files, as (errors, warnings). Never stops at the first."""
    errs: list[str] = []
    warns: list[str] = []

    def req(d: Any, keys: list[str], where: str) -> bool:
        if not isinstance(d, dict):
            errs.append(f"{where}: must be a mapping")
            return False
        missing = [k for k in keys if k not in d]
        for k in missing:
            errs.append(f"{where}: missing required field '{k}'")
        return not missing

    # ---- settings ----
    req(settings, REQUIRED_SETTINGS, "settings.yaml")
    st = settings
    if st.get("downtime_policy", "pause") not in ("pause", "catch_up"):
        errs.append(f"settings.yaml: downtime_policy must be 'pause' or 'catch_up', "
                    f"got {st['downtime_policy']!r}")
    if st.get("trace_level", "full") not in ("full", "summary"):
        errs.append(f"settings.yaml: trace_level must be 'full' or 'summary', got {st['trace_level']!r}")
    if _num(st.get("max_catch_up_hours")) and st["max_catch_up_hours"] <= 0:
        errs.append("settings.yaml: max_catch_up_hours must be > 0")
    if _num(st.get("sim_minutes_min")) and _num(st.get("sim_minutes_max")) \
            and st["sim_minutes_min"] > st["sim_minutes_max"]:
        errs.append("settings.yaml: sim_minutes_min > sim_minutes_max")
    if isinstance(st.get("gain_values"), dict):
        for b in ("0", "+", "++"):
            if b not in {str(k) for k in st["gain_values"]}:
                errs.append(f"settings.yaml gain_values: missing bucket '{b}'")
    ui = st.get("web_ui")
    if isinstance(ui, dict):
        pres = ui.get("presence")
        pkeys = ("hue_low_valence", "hue_high_valence", "saturation_awake", "saturation_dozing",
                 "lightness_awake", "lightness_asleep", "lightness_dozing",
                 "pulse_seconds_calm", "pulse_seconds_aroused")
        if not isinstance(pres, dict):
            errs.append("settings.yaml web_ui: missing required field 'presence'")
        else:
            for k in pkeys:
                if not _num(pres.get(k)):
                    errs.append(f"settings.yaml web_ui.presence.{k}: must be a number")
            for k in ("saturation_awake", "saturation_dozing", "lightness_awake",
                      "lightness_asleep", "lightness_dozing"):
                if _num(pres.get(k)) and not 0 <= pres[k] <= 100:
                    errs.append(f"settings.yaml web_ui.presence.{k}: must be 0..100")
            for k in ("pulse_seconds_calm", "pulse_seconds_aroused"):
                if _num(pres.get(k)) and pres[k] <= 0:
                    errs.append(f"settings.yaml web_ui.presence.{k}: must be > 0")
    sl = st.get("sleep")
    if isinstance(sl, dict):
        for k in ("max_self_image_lines", "self_image_min_confidence", "max_self_image_length"):
            if k not in sl:
                errs.append(f"settings.yaml sleep: missing required field '{k}'")
        for k in ("max_self_image_lines", "max_self_image_length"):
            n = sl.get(k, 1)
            if isinstance(n, bool) or not isinstance(n, int) or n < 1:
                errs.append(f"settings.yaml sleep.{k}: must be an integer >= 1")
        c = sl.get("self_image_min_confidence", 0)
        if not _num(c) or not 0 <= c <= 1:
            errs.append("settings.yaml sleep.self_image_min_confidence: must be 0..1")

    # ---- world: areas, objects, events ----
    areas: dict = {}
    if req(world, ["areas", "objects"], "world.yaml"):
        areas = world["areas"] if isinstance(world["areas"], dict) else {}
        if not areas:
            errs.append("world.yaml: needs at least one area")
        for aid, a in areas.items():
            if not req(a, ["name", "description", "exits"], f"world.yaml area '{aid}'"):
                continue
            for ex in a["exits"]:
                if ex not in areas:
                    errs.append(f"world.yaml area '{aid}': exit '{ex}' is not an area")
                elif aid not in (areas[ex].get("exits") or []):
                    errs.append(f"world.yaml: exit '{aid}' -> '{ex}' is not mirrored "
                                f"('{ex}' has no exit back)")
        ids = set()
        for o in world["objects"] or []:
            if not req(o, ["id", "name", "area", "tags"], "world.yaml object"):
                continue
            if o["id"] in ids:
                errs.append(f"world.yaml: duplicate object id '{o['id']}'")
            ids.add(o["id"])
            if o["area"] not in areas and o["area"] != "inventory":
                errs.append(f"world.yaml object '{o['id']}': unknown area '{o['area']}'")
    if isinstance(world, dict):
        for k in sorted(set(world) - WORLD_FIELDS):
            warns.append(f"world.yaml: unknown top-level field '{k}' (ignored)")

    need_ids = set(character.get("needs") or {}) if isinstance(character.get("needs"), dict) else set()
    weather = (world.get("weather") if isinstance(world, dict) else None) or ["clear"]
    for ev in (world.get("events") or []) if isinstance(world, dict) else []:
        if not req(ev, ["text", "chance"], "world.yaml event"):
            continue
        where = f"world.yaml event '{ev['text']}'"
        if not _num(ev["chance"]) or not 0 <= ev["chance"] <= 1:
            errs.append(f"{where}: chance must be 0..1")
        if "area" in ev and ev["area"] not in areas:
            errs.append(f"{where}: unknown area '{ev['area']}'")
        if "set_weather" in ev and ev["set_weather"] not in weather:
            errs.append(f"{where}: unknown weather")
        if "only_weather" in ev and ev["only_weather"] not in weather:
            errs.append(f"{where}: unknown only_weather '{ev['only_weather']}'")
        if "hours" in ev:
            h = ev["hours"]
            if not (isinstance(h, (list, tuple)) and len(h) == 2 and all(_num(x) and 0 <= x <= 24 for x in h)):
                errs.append(f"{where}: hours must be [from, to] with each 0..24")
        if "mood" in ev and (not _num(ev["mood"]) or not -1 <= ev["mood"] <= 1):
            errs.append(f"{where}: mood must be a number -1..1")
        if "spawn" in ev:
            req(ev["spawn"], ["name", "tags", "area"], f"{where} spawn")
            sp = ev["spawn"]
            if isinstance(sp, dict) and "area" in sp and sp["area"] not in areas and sp["area"] != "inventory":
                errs.append(f"{where} spawn: unknown area '{sp['area']}'")
        if "needs" in ev:    # effects on needs: every id must be a real need
            eff = ev["needs"]
            for nid in (eff if isinstance(eff, (dict, list)) else []):
                if nid not in need_ids:
                    errs.append(f"{where}: effect references unknown need '{nid}'")
        for k in sorted(set(ev) - EVENT_FIELDS):
            warns.append(f"{where}: unknown field '{k}' (ignored)")

    # ---- character ----
    if req(character, ["name", "core_traits", "tendencies", "needs", "start_area"], "character.yaml"):
        if character["start_area"] not in areas:
            errs.append(f"character.yaml: start_area '{character['start_area']}' is not an area")
        needs, tend = character["needs"], character["tendencies"]
        if not isinstance(needs, dict) or not needs:
            errs.append("character.yaml: needs must not be empty")
            needs = {}
        if "energy" in needs:
            errs.append("character.yaml: 'energy' is derived, not a need; "
                        "use energy_weight on needs (e.g. a 'rest' need)")
        for nid, n in needs.items():
            w = f"character.yaml need '{nid}'"
            if not req(n, ["start", "decay"], w):
                continue
            if not _num(n["start"]) or not 0 <= n["start"] <= 100:
                errs.append(f"{w}: start must be 0..100")
            if not _num(n["decay"]) or n["decay"] < 0:
                errs.append(f"{w}: decay must be a number >= 0")
            if "energy_weight" in n and (not _num(n["energy_weight"]) or n["energy_weight"] < 0):
                errs.append(f"{w}: energy_weight must be a number >= 0")
            for flag in NEED_FLAGS:
                if flag in n and not isinstance(n[flag], bool):
                    errs.append(f"{w}: {flag} must be true or false")
            for k in sorted(set(n) - NEED_FIELDS):
                errs.append(f"{w}: unknown behaviour flag or field '{k}'")
        for flag in ("effort_drain", "sleep_restore"):
            flagged = [k for k, n in needs.items() if isinstance(n, dict) and n.get(flag)]
            if len(flagged) != 1:
                errs.append(f"character.yaml: exactly one need must set {flag}: true (found {flagged})")
        if needs and not any(_num(n.get("energy_weight")) and n["energy_weight"] > 0
                             for n in needs.values() if isinstance(n, dict)):
            errs.append("character.yaml: at least one need needs energy_weight > 0 "
                        "(energy is derived from the needs)")
        if not isinstance(tend, dict):
            errs.append("character.yaml: tendencies must be a mapping")
            tend = {}
        for t, v in tend.items():
            if not _num(v) or not 0 <= v <= 1:
                errs.append(f"character.yaml tendency '{t}': must be 0..1")
        labels = character.get("tendency_labels") or {}
        if not isinstance(labels, dict):
            errs.append("character.yaml: tendency_labels must be a mapping")
            labels = {}
        for t, pair in labels.items():
            if t not in tend:
                errs.append(f"character.yaml tendency_labels: unknown tendency '{t}'")
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
                errs.append(f"character.yaml tendency_labels '{t}': must be [low_label, high_label]")
        if not isinstance(character["core_traits"], list) or not all(isinstance(x, str) for x in character["core_traits"]):
            errs.append("character.yaml: core_traits must be a list of text")
    if isinstance(character, dict):
        if "self_image" in character:
            si = character["self_image"]
            if not isinstance(si, dict) or set(si) - {"seed"}:
                errs.append("character.yaml: self_image must be a mapping with only 'seed'")
            elif not isinstance(si.get("seed", ""), str):
                errs.append("character.yaml self_image.seed: must be text only")
        for k in ("voice", "backstory"):
            if k in character and not isinstance(character[k], str):
                errs.append(f"character.yaml: {k} must be text")
        for k in sorted(set(character) - CHARACTER_FIELDS):
            warns.append(f"character.yaml: unknown top-level field '{k}' (ignored)")
    return errs, warns


def validate(settings: dict, character: dict, world: dict, prompts: dict | None = None) -> list[str]:
    """Raise one ConfigError listing every problem; return the warnings."""
    errs, warns = check(settings, character, world)
    if prompts is not None:
        from cat import prompts as P
        pe, pw = P.check(prompts)
        errs += pe
        warns += pw
    if errs:
        raise ConfigError(f"{len(errs)} config problem{'s' if len(errs) != 1 else ''}:\n  - "
                          + "\n  - ".join(errs))
    return warns


def sibling_overrides(db_path: Path | str) -> dict[str, str]:
    """Settings that keep every runtime file next to a given database: a run with `--db X` then
    writes <dir>/devdash.db, <dir>/logs/ and <dir>/engine.heartbeat beside X instead of the defaults."""
    base = Path(db_path).resolve().parent
    return {"db_path": str(Path(db_path).resolve()), "dev_db_path": str(base / "devdash.db"),
            "log_dir": str(base / "logs"), "heartbeat_file": str(base / "engine.heartbeat")}


def load_config(config_dir: Path | str | None = None, overrides: dict | None = None) -> Config:
    d = Path(config_dir or os.environ.get("CAT_CONFIG_DIR") or CONFIG_DIR)
    settings = _load_yaml(d / "settings.yaml")
    if overrides:
        settings.update(overrides)
    character = _load_yaml(d / "character.yaml")
    world = _load_yaml(d / "world.yaml")
    prompts = _load_yaml(d / "prompts.yaml")
    for w in validate(settings, character, world, prompts):
        logging.getLogger("cat.config").warning("%s", w)
    # YAML may parse bucket keys as strings already ("0"); normalise
    settings["gain_values"] = {str(k): v for k, v in settings["gain_values"].items()}
    return Config(settings, character, world, prompts, root=d.resolve().parent, config_dir=d.resolve(),
                  overrides=dict(overrides) if overrides else None)

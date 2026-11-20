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
    moves: dict[str, Any] = field(default_factory=dict)     # config/moves.yaml: the verbs the world offers
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
    "sleep", "recall", "notable", "energy_words",
    "events_max_per_tick", "max_changes_per_tick", "inventory_max", "name_max_length",
    "consume_min_gain", "need_bands", "dislike_drain", "projects", "habits", "anxiety", "director", "moves", "activity", "max_pressing_needs", "stale_after_ticks", "sustenance_need", "energy_start", "llm_temperature", "journal", "llm_num_ctx", "llm_retries", "error_backoff_seconds", "web_ui", "heartbeat_file", "healthcheck", "health_require_models",
    "pull_timeout_seconds", "backup_keep", "downtime_policy", "max_catch_up_hours",
    "catch_up_min_gap_ticks", "catch_up_decay_multiplier", "trace_enabled", "trace_level",
    "trace_retention_days", "trace_recall_candidates", "log_dir", "log_level", "log_max_bytes",
    "log_backup_count", "dev_db_path", "dev_commands_enabled", "dev_poll_seconds",
    "paused_alive_seconds", "devdash", "tendency_words", "max_objects_per_area",
]


CHARACTER_FIELDS = {"name", "pronoun", "core_traits", "tendencies", "tendency_labels", "needs",
                    "start_area", "voice", "backstory", "self_image", "stale_here"}
WORLD_FIELDS = {"areas", "objects", "weather", "events", "tag_facts"}
NEED_FIELDS = {"start", "decay_per_hour", "meaning", "expressions", "words"}
BANDS = ("empty", "low", "okay", "full")            # ascending; the band names the brain's prompt uses
REQUIRED_BANDS = ("empty", "low", "full")           # `okay` may be null
EVENT_FIELDS = {"text", "chance", "area", "hours", "only_weather", "set_weather", "mood", "spawn", "drains"}


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
    jr = st.get("journal")
    if not isinstance(jr, dict):
        errs.append("settings.yaml: journal must be a mapping with enabled and max_length")
    else:
        if not isinstance(jr.get("enabled"), bool):
            errs.append("settings.yaml journal.enabled: must be true or false")
        ml = jr.get("max_length")
        if isinstance(ml, bool) or not isinstance(ml, int) or ml < 20:
            errs.append("settings.yaml journal.max_length: must be an integer >= 20")
    life = st.get("object_lifetime_hours", {})
    if not isinstance(life, dict):
        errs.append("settings.yaml: object_lifetime_hours must be a mapping of who made it -> sim hours")
    else:
        for who, h in life.items():
            if who not in ("event", "director", "creature", "source"):
                errs.append(f"settings.yaml: object_lifetime_hours: unknown maker '{who}' (event, director or creature)")
            elif not _num(h) or h < 0:
                errs.append(f"settings.yaml: object_lifetime_hours.{who} must be a number of sim hours >= 0")
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
    nb = st.get("need_bands")
    if isinstance(nb, dict):
        keys = ("empty_below", "low_below", "okay_below")
        if set(nb) != set(keys) or not all(_num(nb.get(k)) for k in keys):
            errs.append("settings.yaml need_bands: must be numbers for empty_below, low_below, okay_below")
        else:
            vals = [nb[k] for k in keys]
            if not all(0 <= v <= 100 for v in vals):
                errs.append("settings.yaml need_bands: thresholds must be 0..100")
            if not (vals[0] < vals[1] < vals[2]):
                errs.append("settings.yaml need_bands: thresholds must ascend "
                            "(empty_below < low_below < okay_below)")
    elif "need_bands" in st:
        errs.append("settings.yaml need_bands: must be a mapping")
    for k, lo in (("max_pressing_needs", 1), ("stale_after_ticks", 0)):
        if k in st and (isinstance(st[k], bool) or not isinstance(st[k], int) or st[k] < lo):
            errs.append(f"settings.yaml {k}: must be an integer >= {lo}")
    if "energy_start" in st and (not _num(st["energy_start"]) or not -50 <= st["energy_start"] <= 50):
        errs.append("settings.yaml energy_start: must be a number -50..50")
    ui = st.get("web_ui")
    if isinstance(ui, dict):
        pres = ui.get("presence")
        pkeys = ("hue_low_valence", "hue_high_valence", "saturation_awake", "saturation_dozing",
                 "lightness_awake", "lightness_asleep", "lightness_dozing",
                 "pulse_seconds_tired", "pulse_seconds_energetic")
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
            for k in ("pulse_seconds_tired", "pulse_seconds_energetic"):
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
        for k in ("max_tendencies", "max_new_tendencies_per_night", "tendency_name_max_length"):
            n = sl.get(k, 1)
            if isinstance(n, bool) or not isinstance(n, int) or n < 1:
                errs.append(f"settings.yaml sleep.{k}: must be an integer >= 1")
        for k in ("tendency_start", "tendency_forget_below"):
            n = sl.get(k, 0.3)
            if not _num(n) or not 0 <= n <= 1:
                errs.append(f"settings.yaml sleep.{k}: must be 0..1")
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
            for k in sorted(set(o) - {"id", "name", "area", "tags", "state", "purpose", "produces"}):
                errs.append(f"world.yaml object '{o['id']}': unknown field '{k}'")
            if "edible" in (o.get("tags") or []):
                warns.append(f"world.yaml object '{o['id']}': a permanent object is edible; food should come from a source (produces)")
            pr = o.get("produces")
            if pr is not None:
                w = f"world.yaml object '{o['id']}' produces"
                if not isinstance(pr, dict) or not isinstance(pr.get("name"), str) or not isinstance(pr.get("tags"), list):
                    errs.append(f"{w}: needs name and tags")
                elif ("at_hour" in pr) == ("every_hours" in pr):
                    errs.append(f"{w}: give exactly one of at_hour (0-23) or every_hours")
                elif "at_hour" in pr and not (isinstance(pr["at_hour"], int) and 0 <= pr["at_hour"] <= 23):
                    errs.append(f"{w}.at_hour: must be a whole hour 0-23")
                elif "every_hours" in pr and not (_num(pr["every_hours"]) and pr["every_hours"] > 0):
                    errs.append(f"{w}.every_hours: must be a number > 0")
                elif not isinstance(pr.get("max", 1), int) or pr.get("max", 1) < 1:
                    errs.append(f"{w}.max: must be a whole number >= 1")
    if isinstance(world, dict):
        for k in sorted(set(world) - WORLD_FIELDS):
            warns.append(f"world.yaml: unknown top-level field '{k}' (ignored)")
        tf = world.get("tag_facts", {})
        if not isinstance(tf, dict):
            errs.append("world.yaml: tag_facts must be a mapping of tag -> what it means, e.g. soft: is soft")
        else:
            for tag, text in tf.items():
                if tag not in (settings.get("allowed_tags") or []):
                    errs.append(f"world.yaml tag_facts: '{tag}' is not in settings allowed_tags")
                if not isinstance(text, str) or not text.strip():
                    errs.append(f"world.yaml tag_facts.{tag}: must be non-empty text")

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
        if "drains" in ev:   # points taken from needs when the event fires: every id must be a real need
            dr = ev["drains"]
            if not isinstance(dr, dict):
                errs.append(f"{where}: drains must be a mapping of need id -> points")
            else:
                for nid, pts in dr.items():
                    if nid not in need_ids:
                        errs.append(f"{where}: drains references unknown need '{nid}'")
                    if not _num(pts) or pts < 0:
                        errs.append(f"{where}: drains '{nid}' must be a number >= 0")
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
            errs.append("character.yaml: 'energy' is not a need: it is a separate resource "
                        "(effort spends it, sleep restores it)")
        for nid, n in needs.items():
            w = f"character.yaml need '{nid}'"
            if not req(n, ["start", "decay_per_hour", "meaning", "expressions"], w):
                continue
            if not _num(n["start"]) or not 0 <= n["start"] <= 100:
                errs.append(f"{w}: start must be 0..100")
            if not _num(n["decay_per_hour"]) or n["decay_per_hour"] < 0:
                errs.append(f"{w}: decay_per_hour must be a number >= 0")
            if not isinstance(n["meaning"], str) or not n["meaning"].strip():
                errs.append(f"{w}: meaning must be non-empty text")
            ex = n["expressions"]
            if not isinstance(ex, dict):
                errs.append(f"{w}: expressions must be a mapping of band -> text")
                ex = {}
            for band in REQUIRED_BANDS:
                if not isinstance(ex.get(band), str) or not ex[band].strip():
                    errs.append(f"{w}: expressions.{band} is required and must be non-empty text")
            if "okay" in ex and ex["okay"] is not None and (not isinstance(ex["okay"], str) or not ex["okay"].strip()):
                errs.append(f"{w}: expressions.okay must be null or non-empty text")
            for band in sorted(set(ex) - set(BANDS)):
                errs.append(f"{w}: unknown band '{band}' in expressions (use {', '.join(BANDS)})")
            for k in sorted(set(n) - NEED_FIELDS):
                errs.append(f"{w}: unknown field '{k}'")
        for key, where in (("sustenance_need", "settings.yaml sustenance_need"),):
            if needs and st.get(key) not in needs:
                errs.append(f"{where}: '{st.get(key)}' is not a need in character.yaml")
        rp = st.get("repetition_penalty")
        if isinstance(rp, dict) and needs:
            rn = rp.get("needs")
            if not isinstance(rn, list) or not all(isinstance(x, str) for x in rn):
                errs.append("settings.yaml repetition_penalty.needs: must be a list of need ids")
            else:
                for x in rn:
                    if x not in needs:
                        errs.append(f"settings.yaml repetition_penalty.needs: '{x}' is not a need")
        md = st.get("mood")
        if isinstance(md, dict):
            vb = md.get("bands")
            if not isinstance(vb, dict):
                errs.append("settings.yaml mood.bands: must be a mapping of need id -> {band: points}")
            else:
                for nid, table in vb.items():
                    if needs and nid not in needs:
                        errs.append(f"settings.yaml mood.bands: '{nid}' is not a need")
                    if not isinstance(table, dict):
                        errs.append(f"settings.yaml mood.bands.{nid}: must be a mapping of band -> points")
                        continue
                    for band, pts in table.items():
                        if band not in BANDS:
                            errs.append(f"settings.yaml mood.bands.{nid}: unknown band '{band}'")
                        if not _num(pts) or not -50 <= pts <= 50:
                            errs.append(f"settings.yaml mood.bands.{nid}.{band}: must be a number -50..50")
            if not _num(md.get("event_points")):
                errs.append("settings.yaml mood.event_points: must be a number")
            if not _num(md.get("event_decay")) or not 0 <= md["event_decay"] <= 1:
                errs.append("settings.yaml mood.event_decay: must be a number 0..1")
            if not _num(md.get("word_threshold")) or not 0 <= md["word_threshold"] <= 50:
                errs.append("settings.yaml mood.word_threshold: must be a number 0..50")
            fv = md.get("feeling_values")
            if not isinstance(fv, dict) or not all(_num(v) and -50 <= v <= 50 for v in fv.values()):
                errs.append("settings.yaml mood.feeling_values: must map feelings to numbers -50..50")
        ac_ = st.get("activity")
        if isinstance(ac_, dict):
            for k in ("enabled", "interrupt_on_events", "interrupt_on_empty_need"):
                if not isinstance(ac_.get(k), bool):
                    errs.append(f"settings.yaml activity.{k}: must be true or false")
            if isinstance(ac_.get("step_minutes"), bool) or not isinstance(ac_.get("step_minutes"), int) \
                    or ac_["step_minutes"] < 1:
                errs.append("settings.yaml activity.step_minutes: must be an integer >= 1")
            if not _num(ac_.get("carry_interval_seconds")) or ac_["carry_interval_seconds"] < 0:
                errs.append("settings.yaml activity.carry_interval_seconds: must be a number >= 0")
        elif "activity" in st:
            errs.append("settings.yaml activity: must be a mapping")
        mv_ = st.get("moves")
        if isinstance(mv_, dict):
            for k, lo in (("max_offered", 1),):
                v = mv_.get(k)
                if isinstance(v, bool) or not isinstance(v, int) or v < lo:
                    errs.append(f"settings.yaml moves.{k}: must be an integer >= {lo}")
            for k_ in ("reference_minutes", "min_scale", "max_scale"):
                if not _num(mv_.get(k_)) or mv_[k_] <= 0:
                    errs.append(f"settings.yaml moves.{k_}: must be a number > 0")
            if _num(mv_.get("min_scale")) and _num(mv_.get("max_scale")) and mv_["min_scale"] > mv_["max_scale"]:
                errs.append("settings.yaml moves.min_scale: must not exceed max_scale")
            if not _num(mv_.get("novelty_hours")) or mv_["novelty_hours"] < 0:
                errs.append("settings.yaml moves.novelty_hours: must be a number >= 0")
        elif "moves" in st:
            errs.append("settings.yaml moves: must be a mapping")
        dr_ = st.get("director")
        if isinstance(dr_, dict):
            if not isinstance(dr_.get("enabled"), bool):
                errs.append("settings.yaml director.enabled: must be true or false")
            for k, lo in (("every_ticks", 1), ("stale_every_ticks", 1), ("text_max_length", 1),
                          ("max_drain_needs", 1), ("avoid_repeat", 0), ("recent_actions_shown", 0)):
                v = dr_.get(k)
                if isinstance(v, bool) or not isinstance(v, int) or v < lo:
                    errs.append(f"settings.yaml director.{k}: must be an integer >= {lo}")
            if not _num(dr_.get("max_mood")) or not 0 <= dr_["max_mood"] <= 1:
                errs.append("settings.yaml director.max_mood: must be a number 0..1")
            if not _num(dr_.get("max_drain_points")) or dr_["max_drain_points"] < 0:
                errs.append("settings.yaml director.max_drain_points: must be a number >= 0")
            if isinstance(dr_.get("every_ticks"), int) and isinstance(dr_.get("stale_every_ticks"), int) \
                    and dr_["stale_every_ticks"] > dr_["every_ticks"]:
                errs.append("settings.yaml director.stale_every_ticks: must not exceed every_ticks")
        elif "director" in st:
            errs.append("settings.yaml director: must be a mapping")
        an = st.get("anxiety")
        if isinstance(an, dict):
            if not isinstance(an.get("enabled"), bool):
                errs.append("settings.yaml anxiety.enabled: must be true or false")
            for k in ("start", "rise_per_hour", "fall_per_hour", "sleep_fall_multiplier", "mood_weight",
                      "decay_pressure", "sleep_restore_penalty", "impulse_above", "impulse_max_chance"):
                if not _num(an.get(k)) or an[k] < 0:
                    errs.append(f"settings.yaml anxiety.{k}: must be a number >= 0")
            if _num(an.get("impulse_max_chance")) and an["impulse_max_chance"] > 1:
                errs.append("settings.yaml anxiety.impulse_max_chance: must be 0..1")
            if _num(an.get("impulse_above")) and an["impulse_above"] >= 100:
                errs.append("settings.yaml anxiety.impulse_above: must be below 100")
            for k in ("band_points", "energy_points", "words", "impulses"):
                if not isinstance(an.get(k), dict):
                    errs.append(f"settings.yaml anxiety.{k}: must be a mapping")
            for name, spec in (an.get("impulses") or {}).items() if isinstance(an.get("impulses"), dict) else []:
                if not (isinstance(spec, dict) and _num(spec.get("weight")) and spec["weight"] > 0
                        and isinstance(spec.get("verbs"), list) and spec["verbs"]):
                    errs.append(f"settings.yaml anxiety.impulses.{name}: needs a weight > 0 and a list of verbs")
        elif "anxiety" in st:
            errs.append("settings.yaml anxiety: must be a mapping")
        hb = st.get("habits")
        if isinstance(hb, dict):
            for k in ("window_ticks", "min_count", "max_stored", "max_shown", "text_max_length"):
                v = hb.get(k)
                if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                    errs.append(f"settings.yaml habits.{k}: must be an integer >= 1")
            ms = hb.get("min_share")
            if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not 0 < ms <= 1:
                errs.append("settings.yaml habits.min_share: must be a number above 0 and up to 1")
            if not isinstance(hb.get("enabled"), bool):
                errs.append("settings.yaml habits.enabled: must be true or false")
        elif "habits" in st:
            errs.append("settings.yaml habits: must be a mapping")
        pj = st.get("projects")
        if isinstance(pj, dict):
            for k, lo in (("max_active", 1), ("milestone_every", 1), ("title_max_length", 1), ("why_max_length", 1),
                          ("note_max_length", 1), ("artifact_max_length", 1), ("artifacts_shown", 1),
                          ("pause_after_idle_nights", 0)):
                v = pj.get(k)
                if isinstance(v, bool) or not isinstance(v, int) or v < lo:
                    errs.append(f"settings.yaml projects.{k}: must be an integer >= {lo}")
            if not _num(pj.get("idle_hours_note")) or pj["idle_hours_note"] < 0:
                errs.append("settings.yaml projects.idle_hours_note: must be a number >= 0")
            if not _num(pj.get("taper")) or not 0 <= pj["taper"] <= 1:
                errs.append("settings.yaml projects.taper: must be a number 0..1")
            if not isinstance(pj.get("artifacts_enabled"), bool):
                errs.append("settings.yaml projects.artifacts_enabled: must be true or false")
            if pj.get("work_bucket") not in (pj.get("progress_values") or {}):
                errs.append("settings.yaml projects.work_bucket: must be a key of progress_values")
            pv = pj.get("progress_values")
            if not isinstance(pv, dict) or not pv or not all(_num(v) and 0 < v <= 100 for v in pv.values()):
                errs.append("settings.yaml projects.progress_values: must map buckets to numbers 0..100 (> 0)")
            elif isinstance(st.get("gain_values"), dict):
                for b in pv:
                    if str(b) not in {str(k) for k in st["gain_values"]}:
                        errs.append(f"settings.yaml projects.progress_values: bucket '{b}' is not in gain_values")
            cg = pj.get("completion_gains")
            if not isinstance(cg, dict):
                errs.append("settings.yaml projects.completion_gains: must be a mapping of need id -> points")
            else:
                for nid, pts in cg.items():
                    if needs and nid not in needs:
                        errs.append(f"settings.yaml projects.completion_gains: '{nid}' is not a need")
                    if not _num(pts) or pts < 0:
                        errs.append(f"settings.yaml projects.completion_gains.{nid}: must be a number >= 0")
            stg = pj.get("stages")
            if not isinstance(stg, dict) or not stg or not all(_num(k) for k in stg) \
                    or not all(isinstance(v, str) and v.strip() for v in stg.values()):
                errs.append("settings.yaml projects.stages: must map ascending progress bounds to non-empty words")
            elif max(stg) < 100.0001:
                errs.append("settings.yaml projects.stages: needs a bound above 100 so every progress has a word")
        elif "projects" in st:
            errs.append("settings.yaml projects: must be a mapping")
        dd = st.get("dislike_drain")
        if isinstance(dd, dict):
            dn = dd.get("needs")
            if not isinstance(dn, list) or not all(isinstance(x, str) for x in dn):
                errs.append("settings.yaml dislike_drain.needs: must be a list of need ids")
            else:
                for x in dn:
                    if needs and x not in needs:
                        errs.append(f"settings.yaml dislike_drain.needs: '{x}' is not a need")
            if not _num(dd.get("points_per_hour")) or dd["points_per_hour"] < 0:
                errs.append("settings.yaml dislike_drain.points_per_hour: must be a number >= 0")
            mm = dd.get("max_matches")
            if isinstance(mm, bool) or not isinstance(mm, int) or mm < 1:
                errs.append("settings.yaml dislike_drain.max_matches: must be an integer >= 1")
        elif "dislike_drain" in st:
            errs.append("settings.yaml dislike_drain: must be a mapping")
        if "stale_here" in character and (not isinstance(character["stale_here"], str)
                                          or not character["stale_here"].strip()):
            errs.append("character.yaml: stale_here must be non-empty text")
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


def validate(settings: dict, character: dict, world: dict, prompts: dict | None = None,
             moves: dict | None = None) -> list[str]:
    """Raise one ConfigError listing every problem; return the warnings."""
    errs, warns = check(settings, character, world)
    if moves is not None:
        from cat import moves as M
        me, mw = M.check(moves, character, settings)
        errs += me
        warns += mw
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
    moves = _load_yaml(d / "moves.yaml")
    for w in validate(settings, character, world, prompts, moves):
        logging.getLogger("cat.config").warning("%s", w)
    # YAML may parse bucket keys as strings already ("0"); normalise
    settings["gain_values"] = {str(k): v for k, v in settings["gain_values"].items()}
    return Config(settings, character, world, prompts, moves, root=d.resolve().parent, config_dir=d.resolve(),
                  overrides=dict(overrides) if overrides else None)

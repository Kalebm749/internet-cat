"""Engine tuning for the rewrite: the `nit:` block of config/settings.yaml (docs/rewrite-contracts.md).

Every number the new engine uses lives there, never in code. `load` validates the block and reports every
problem at once. Steps of the rewrite add their own sections (body now; decisions, sleep, ... later).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from cat.config import CONFIG_DIR, ROOT


class TuningError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("settings.yaml nit: invalid:\n  " + "\n  ".join(problems))


# section -> key -> (low, high); None means no bound on that side
BODY = {
    "step_minutes": (1, 60),
    "mood_baseline": (-1, 1),
    "mood_drift_per_hour": (0, 1),
    "hunger_per_hour": (0, 1),
    "hunger_sleep_factor": (0, 1),
    "hunger_per_effort": (0, 10),
    "hunger_mood_threshold": (0, 1),
    "hunger_mood_drag": (0, 10),
    "awake_energy_per_hour": (0, 1),
    "sleep_energy_per_hour": (0, 1),
    "hungry_sleep_penalty": (0, 1),
}
START = {"mood": (-1, 1), "energy": (0, 1), "hunger": (0, 1)}
# numeric sections: key -> (low, high)
SECTIONS = {
    "decisions": {
        "interval_seconds": (1, 3600),
        "check_in_minutes": (1, 24 * 60),
        "event_salience_min": (0, 1),
        "retries": (0, 5),
        "thought_max_chars": (40, 4000),
        "reasoning_max_chars": (40, 4000),
        "recent_activities": (0, 50),
        "prompt_tokens": (300, 32000),
    },
    "loop": {
        "step_seconds": (0.05, 60),
        "doze_seconds": (1, 3600),
        "max_step_minutes": (1, 24 * 60),
    },
    "memory": {
        "recall_k": (0, 50), "notable_k": (0, 50), "notable_min_importance": (0, 1), "w_recency": (0, 10),
        "w_importance": (0, 10), "w_relevance": (0, 10), "recency_half_life_days": (0.01, 365),
        "recall_boost": (0, 10), "forget_below": (0, 1), "dup_threshold": (0.5, 1),
        "recall_rest_minutes": (0, 24 * 60 * 7), "recall_rest_factor": (0, 1), "wander_slots": (0, 10),
        "wander_after_minutes": (1, 24 * 60 * 30), "learned_k": (0, 10), "learned_days": (0, 365),
        "learned_chars": (40, 4000), "long_cap": (5, 1000),
    },
    "loops": {"max_open": (1, 20), "per_night": (0, 5), "expire_days": (1, 90), "on_mind": (0, 10),
              "text_chars": (20, 400)},
    "repetition": {
        "half_life_minutes": (1, 10000), "w_action": (0, 2), "w_family": (0, 2), "w_target": (0, 2),
        "w_aimless": (0, 2), "freshness_min": (0, 1), "window_hours": (1, 24 * 30),
        "restart_window_minutes": (0, 24 * 60), "w_restart": (0, 2), "w_place": (0, 2), "note_below": (0, 1), "recent": (1, 50), "thought_window": (0, 100),
        "thought_dup_threshold": (0.5, 1), "thought_dup_mood": (-1, 0), "thought_dup_cooldown_minutes": (0, 24 * 60), "reflection_window": (0, 100),
        "reflection_dup_threshold": (0.5, 1), "reflection_dup_mood": (-1, 0), "subject_floor": (0.3, 1),
        "subject_weight": (0, 2), "subject_half_life_minutes": (1, 24 * 60 * 30), "subject_window": (1, 200),
        "dwell_min": (2, 50),
    },
    "mail": {
        "on_mind": (0, 20),
        "on_mind_chars": (40, 4000),
        "on_mind_days": (0, 365),
    },
}
SLEEP_NUMBERS = {
    "min_hours": (0.5, 24), "wake_energy": (0.05, 1), "max_hours": (0.5, 24), "retries": (0, 5),
    "day_log_max": (5, 500), "consolidate_max": (1, 100), "core_per_night": (0, 10), "long_term_per_night": (0, 50),
    "pref_min_samples": (1, 100), "pref_window_days": (1, 60), "pref_threshold": (0, 1), "pref_step": (0, 1),
    "pref_decay": (0, 1), "pref_weight": (0, 2), "pref_note_min": (0, 1), "dev_min_evidence": (1, 100),
    "dev_min_days": (1, 60), "dev_max_step": (0, 1), "dev_new_strength": (0, 1), "dev_cooldown_days": (0, 60),
}
SLEEP_OTHER = {"night_hours", "threaded", "dev_forbidden_words"}
INTEGER_KEYS = {"long_cap", "max_open", "per_night", "expire_days", "on_mind", "text_chars", "learned_k", "learned_chars", "subject_window", "dwell_min", "wander_slots", "canvas", "max_points", "max_points_per_pass", "max_total", "max_tokens", "day_log_max", "consolidate_max", "core_per_night", "long_term_per_night", "pref_min_samples",
                "dev_min_evidence", "dev_min_days", "recall_k", "notable_k", "recent", "thought_window",
                "reflection_window", "retries", "recent_activities", "prompt_tokens", "thought_max_chars", "reasoning_max_chars",
                "on_mind", "on_mind_chars"}
MODEL_KEYS = ("decision_model", "sleep_model", "embed_model", "art_model", "vision_model")
TOP = {"db_path", "content_dir", "time_scale", "body", "models", "sleep", "art"} | set(SECTIONS)
ART_NUMBERS = {"canvas": (128, 2048), "max_points": (2, 1000), "max_points_per_pass": (10, 100000),
               "max_total": (1, 10000), "max_tokens": (200, 32000)}


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_numbers(name: str, sec: Any, spec: dict, probs: list[str]) -> None:
    if not isinstance(sec, dict):
        probs.append(f"{name} must be a mapping")
        return
    for k in sorted(set(spec) - set(sec)):
        probs.append(f"{name}: missing '{k}'")
    for k in sorted(set(sec) - set(spec)):
        probs.append(f"{name}: unknown key '{k}'")
    for k, (lo, hi) in spec.items():
        v = sec.get(k)
        if k in sec and not (_num(v) and lo <= v <= hi and (k not in INTEGER_KEYS or isinstance(v, int))):
            kind = "a whole number" if k in INTEGER_KEYS else "a number"
            probs.append(f"{name}.{k} must be {kind} between {lo} and {hi}")


def validate(nit: Any) -> list[str]:
    probs: list[str] = []
    if not isinstance(nit, dict):
        return ["the nit block is missing or not a mapping"]
    for k in sorted(TOP - set(nit)):
        probs.append(f"missing '{k}'")
    for k in sorted(set(nit) - TOP):
        probs.append(f"unknown key '{k}'")
    if "time_scale" in nit and not (_num(nit["time_scale"]) and nit["time_scale"] > 0):
        probs.append("time_scale must be a number > 0")
    for k in ("db_path", "content_dir"):
        if k in nit and not isinstance(nit[k], str):
            probs.append(f"{k} must be a path")
    models = nit.get("models")
    if "models" in nit:
        if not isinstance(models, dict) or set(models) != set(MODEL_KEYS) \
                or not all(isinstance(v, str) and v for v in models.values()):
            probs.append(f"models must name {', '.join(MODEL_KEYS)}")
    for name, spec in SECTIONS.items():
        if name in nit:
            _check_numbers(name, nit[name], spec, probs)
    art = nit.get("art")
    if "art" in nit:
        if not isinstance(art, dict):
            probs.append("art must be a mapping")
        else:
            _check_numbers("art", {k: v for k, v in art.items() if k != "effort_ops"}, ART_NUMBERS, probs)
            eo = art.get("effort_ops")
            if not (isinstance(eo, dict) and eo and all(isinstance(k, str) and isinstance(v, int) and 1 <= v <= 400
                                                        for k, v in eo.items())):
                probs.append("art.effort_ops must map effort names to 1..400 operations")
    sl = nit.get("sleep")
    if "sleep" in nit:
        if not isinstance(sl, dict):
            probs.append("sleep must be a mapping")
        else:
            _check_numbers("sleep", {k: v for k, v in sl.items() if k not in SLEEP_OTHER},
                           SLEEP_NUMBERS, probs)
            for k in sorted(SLEEP_OTHER - set(sl)):
                probs.append(f"sleep: missing '{k}'")
            if "min_hours" in sl and "max_hours" in sl and _num(sl["min_hours"]) and _num(sl["max_hours"]) \
                    and sl["min_hours"] > sl["max_hours"]:
                probs.append("sleep: min_hours is above max_hours")
            nh = sl.get("night_hours")
            if "night_hours" in sl and not (isinstance(nh, list) and len(nh) == 2 and all(_num(h) and 0 <= h <= 24 for h in nh)):
                probs.append("sleep.night_hours must be [from, to] hours")
            if "threaded" in sl and not isinstance(sl["threaded"], bool):
                probs.append("sleep.threaded must be true or false")
            fw = sl.get("dev_forbidden_words")
            if "dev_forbidden_words" in sl and not (isinstance(fw, list) and all(isinstance(w, str) for w in fw)):
                probs.append("sleep.dev_forbidden_words must be a list of words")
    body = nit.get("body")
    if not isinstance(body, dict):
        return probs + (["body must be a mapping"] if "body" in nit else [])
    for k in sorted((set(BODY) | {"start"}) - set(body)):
        probs.append(f"body: missing '{k}'")
    for k in sorted(set(body) - set(BODY) - {"start"}):
        probs.append(f"body: unknown key '{k}'")
    for k, (lo, hi) in BODY.items():
        v = body.get(k)
        if k in body and not (_num(v) and lo <= v <= hi):
            probs.append(f"body.{k} must be a number between {lo} and {hi}")
    start = body.get("start")
    if "start" in body:
        if not isinstance(start, dict) or set(start) != set(START):
            probs.append("body.start must give mood, energy and hunger")
        else:
            for k, (lo, hi) in START.items():
                if not (_num(start[k]) and lo <= start[k] <= hi):
                    probs.append(f"body.start.{k} must be between {lo} and {hi}")
    return probs


def load(settings_path: Path | str | None = None) -> dict:
    """Read and validate the `nit:` block. Raises TuningError listing every problem."""
    path = Path(settings_path) if settings_path else CONFIG_DIR / "settings.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    nit = data.get("nit")
    probs = validate(nit)
    if probs:
        raise TuningError(probs)
    return nit


def path(nit: dict, key: str) -> Path:
    p = Path(nit[key])
    return p if p.is_absolute() else ROOT / p

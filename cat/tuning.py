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
TOP = {"db_path", "content_dir", "time_scale", "body"}


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


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

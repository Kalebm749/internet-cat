"""Impulses: under enough anxiety a decision can be taken over before the creature has thought it through.

Code, seeded and deterministic (same time and place, same outcome), picks one of the configured impulses
(`anxiety.impulses`: a weight and the verbs it acts through) and one matching move from the CURRENT menu, so
an impulse can only ever do what the world offers. It never looks at which move would feed a need. Every
takeover is traced and the diary says so in the creature's own voice (prompts.yaml `impulse`)."""
from __future__ import annotations

import random

from cat import prompts as P
from cat.config import Config


def chance(cfg: Config, level: float) -> float:
    a = cfg.settings["anxiety"]
    if not a["enabled"] or level <= a["impulse_above"]:
        return 0.0
    return a["impulse_max_chance"] * min(1.0, (level - a["impulse_above"]) / max(1e-9, 100.0 - a["impulse_above"]))


def take_over(cfg: Config, level: float, menu: list, seed: str) -> dict | None:
    """None, or {"impulse": name, "move": a Move from `menu`, "chance": p, "entry": first-person text}."""
    p = chance(cfg, level)
    if p <= 0 or not menu:
        return None
    rng = random.Random(f"impulse|{seed}")
    if rng.random() >= p:
        return None
    options = []
    for name, spec in cfg.settings["anxiety"]["impulses"].items():
        moves = [m for m in menu if m.verb in spec["verbs"]]
        if moves:
            options.append((name, float(spec["weight"]), moves))
    if not options:
        return None
    name, _, moves = rng.choices(options, weights=[w for _, w, _ in options])[0]
    move = rng.choice(moves)
    key = f"impulse.{name}"
    entry = P.render(cfg, key if P.lookup(cfg.prompts, key) else "impulse.default")
    return {"impulse": name, "move": move, "chance": round(p, 3), "entry": entry}

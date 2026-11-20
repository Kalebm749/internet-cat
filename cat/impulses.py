"""Impulses: under enough anxiety a decision can be taken over before the creature has thought it through.

Code, seeded and deterministic (same time and place, same outcome), picks one of the configured impulses
(`anxiety.impulses`: a weight and the verbs it acts through) and one matching move from the CURRENT menu, so
an impulse can only ever do what the world offers. It never looks at which move would feed a need. Every
takeover is traced and the diary says so in the creature's own voice (prompts.yaml `impulse`)."""
from __future__ import annotations

import random

from cat import prompts as P
from cat.config import Config


def feeding_reflex(cfg: Config, needs: dict[str, float], menu: list, seed: str) -> dict | None:
    """The one innate reflex (settings `reflexes.feeding`): when the sustenance need is in the configured band
    (empty by default) and something to eat is on the menu, a seeded roll may take the decision over with one of
    the reflex's verbs, before the brain is asked. It is the only place code picks a move because of a need; it
    is traced like an impulse and the diary says so. None when it does not fire."""
    from cat import character
    r = (cfg.settings.get("reflexes") or {}).get("feeding")
    if not r or not r.get("enabled") or not menu:
        return None
    sus = cfg.settings["sustenance_need"]
    if sus not in needs or character.need_band(float(needs[sus]), cfg) not in r["bands"]:
        return None
    moves = [m for m in menu if m.verb in r["verbs"]]
    if not moves:
        return None
    rng = random.Random(f"reflex|{seed}")
    if rng.random() >= r["chance"]:
        return None
    key = "impulse.feeding"
    entry = P.render(cfg, key if P.lookup(cfg.prompts, key) else "impulse.default")
    return {"impulse": "feeding", "move": rng.choice(moves), "chance": float(r["chance"]), "entry": entry,
            "reflex": True}


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

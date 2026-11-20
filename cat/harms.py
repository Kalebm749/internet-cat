"""The world can hurt. Code rules, traced; the outcome text says what happened (settings `harms`, moves.yaml `hurts`).

- A move can go wrong (moves.yaml `hurts`): eating food left too long (stale), ringing the bell while on edge,
  splashing in the cold puddle, climbing while tired (a slip). Each hurt has a condition, an optional chance and
  target tags, drains, an energy cost, a gain factor (0 = the move gave nothing) and the words for what happened.
- Exposure: in rain somewhere not `sheltered` (world.yaml areas), it is soaked (drains, energy); a `noisy` area
  wears on it during its hours. Awake or asleep.
Nothing here picks a move."""
from __future__ import annotations

import random

from cat import character, clock
from cat.config import Config


def _stale(cfg: Config, obj: dict | None, now: str) -> bool:
    if not obj or obj.get("created_by") not in ("source", "event") or not obj.get("created_at"):
        return False
    hours = (clock.parse(now) - clock.parse(obj["created_at"])).total_seconds() / 3600
    return hours >= float(cfg.settings["harms"]["stale_after_hours"])


def move_hurt(cfg: Config, verb: str, target: dict | None, anxiety: float, energy: float, now: str,
              seed: str) -> dict | None:
    """The first of the verb's hurts whose condition holds (and whose chance comes up), or None."""
    h = cfg.settings["harms"]
    for i, hurt in enumerate(cfg.moves["verbs"][verb].get("hurts") or []):
        cond = hurt["if"]
        ok = (cond == "always"
              or (cond == "tired" and character.energy_word(energy, cfg) in h["tired_words"])
              or (cond == "anxious" and character.anxiety_word(anxiety, cfg) in h["anxious_words"])
              or (cond == "stale" and _stale(cfg, target, now)))
        if ok and hurt.get("target_tags"):
            ok = bool(target) and set(hurt["target_tags"]) <= set(target.get("tags") or [])
        if not ok:
            continue
        chance = float(hurt.get("chance", 1.0))
        roll = random.Random(f"hurt|{seed}|{i}").random()
        if roll >= chance:
            continue
        return {"if": cond, "chance": chance, "drains": dict(hurt.get("drains") or {}),
                "energy": float(hurt.get("energy", 0)), "gain_factor": float(hurt.get("gain_factor", 1.0)),
                "outcome": hurt["outcome"]}
    return None


def exposure(cfg: Config, area_id: str, weather: str, sim_time: str, minutes: float) -> dict:
    """What the place and the weather take from it over `minutes`: {drains, energy, texts}."""
    a = cfg.world["areas"][area_id]
    drains: dict[str, float] = {}
    energy, texts = 0.0, []
    hours = float(minutes) / 60
    rain = cfg.settings["harms"].get("rain") or {}
    if weather == "rain" and not a.get("sheltered") and rain:
        for n, pts in (rain.get("per_hour") or {}).items():
            drains[n] = drains.get(n, 0.0) + float(pts) * hours
        energy += float(rain.get("energy_per_hour", 0)) * hours
        texts.append(rain.get("text", ""))
    noisy = a.get("noisy")
    if noisy:
        lo, hi = noisy.get("hours", [0, 24])
        if clock.hour_in_range(clock.parse(sim_time), lo, hi):
            for n, pts in (noisy.get("per_hour") or {}).items():
                drains[n] = drains.get(n, 0.0) + float(pts) * hours
            texts.append(noisy.get("text", ""))
    return {"drains": {k: round(v, 3) for k, v in drains.items()}, "energy": round(energy, 3),
            "texts": [t for t in texts if t]}


def merge(*drain_dicts: dict | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for d in drain_dicts:
        for k, v in (d or {}).items():
            out[k] = out.get(k, 0.0) + float(v)
    return out

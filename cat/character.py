"""Needs, decay, derived energy, mood. Pure functions over dicts; all numbers from settings."""
from __future__ import annotations

from typing import Any

from cat.config import Config


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def need_with_flag(cfg: Config, flag: str) -> str:
    return next(k for k, n in cfg.character["needs"].items() if n.get(flag))


def initial_needs(cfg: Config) -> dict[str, float]:
    return {k: float(n["start"]) for k, n in cfg.character["needs"].items()}


def decay(needs: dict[str, float], sim_minutes: float, cfg: Config) -> dict[str, float]:
    hours = sim_minutes / 60
    return {k: clamp(v - cfg.character["needs"][k]["decay"] * hours) for k, v in needs.items()}


def energy(needs: dict[str, float], cfg: Config) -> float:
    """Derived: weighted average of the needs that carry an energy_weight. Never set directly."""
    num = den = 0.0
    for k, n in cfg.character["needs"].items():
        w = n.get("energy_weight", 0)
        num += w * needs[k]
        den += w
    return round(num / den, 1)


def _word_from(thresholds: dict, value: float) -> str:
    for bound in sorted(thresholds):
        if value < bound:
            return thresholds[bound]
    return thresholds[max(thresholds)]


def need_word(need_id: str, value: float, cfg: Config) -> str:
    words = cfg.character["needs"][need_id].get("words") or cfg.settings["default_need_words"]
    return _word_from(words, value)


def energy_word(value: float, cfg: Config) -> str:
    return _word_from(cfg.settings["energy_words"], value)


def need_label(need_id: str, cfg: Config) -> str:
    return cfg.character["needs"][need_id].get("label", need_id)


def apply_gains(needs: dict[str, float], gains: dict[str, str], effort: str, cfg: Config,
                consumed: bool = False, multiplier: float = 1.0) -> dict[str, float]:
    """Buckets -> numbers, effort cost, clamp. Needs flagged requires_consume only gain when
    something edible was consumed this tick. Unknown need ids / buckets are ignored."""
    out = dict(needs)
    values = cfg.settings["gain_values"]
    for nid, bucket in (gains or {}).items():
        spec = cfg.character["needs"].get(nid)
        if spec is None or str(bucket) not in values:
            continue
        if spec.get("requires_consume"):
            if not consumed:
                continue
            out[nid] = clamp(out[nid] + values[str(bucket)])   # physical: never repetition-penalised
        else:
            out[nid] = clamp(out[nid] + values[str(bucket)] * multiplier)
    if consumed:  # eating guarantees a minimum gain even if the narrator forgot to score it
        floor = values[str(cfg.settings["consume_min_gain"])]
        for nid, spec in cfg.character["needs"].items():
            if spec.get("requires_consume") and (gains or {}).get(nid) is None:
                out[nid] = clamp(out[nid] + floor)
    drain = need_with_flag(cfg, "effort_drain")
    out[drain] = clamp(out[drain] - cfg.settings["effort_energy_cost"].get(effort, 0))
    return out


def restore(needs: dict[str, float], amount: float, cfg: Config) -> dict[str, float]:
    out = dict(needs)
    k = need_with_flag(cfg, "sleep_restore")
    out[k] = clamp(out[k] + amount)
    return out


def compute_mood(needs: dict[str, float], before: dict[str, float], event_nudge: float,
                 feeling: str | None, cfg: Config) -> dict[str, Any]:
    """Mood from need levels, need trends (weigh more), recent events, and the brain's own
    `feeling` (blended lightly). Returns valence -1..1, arousal 0..1, a word and a description."""
    m = cfg.settings["mood"]
    level = (sum(needs.values()) / len(needs) - 50) / 50
    trend = clamp(sum(needs[k] - before.get(k, needs[k]) for k in needs) / len(needs)
                  / m["trend_scale"], -1, 1)
    feel = m["feeling_values"].get(feeling or "neutral", 0.0)
    valence = clamp(m["w_level"] * level + m["w_trend"] * trend + m["w_event"] * event_nudge
                    + m["w_reaction"] * feel, -1, 1)
    en = energy(needs, cfg)
    arousal = clamp(0.2 + 0.4 * en / 100 + 0.4 * abs(trend), 0, 1)

    hi = arousal >= m["arousal_high"]
    if valence > m["word_threshold"]:
        word = "excited" if hi else "content"
    elif valence < -m["word_threshold"]:
        word = "tense" if hi else "low"
    else:
        word = "restless" if hi else "calm"

    lowest = min(needs, key=lambda k: needs[k])
    if needs[lowest] < m["low_need_below"]:
        desc = f"{word}, though {need_label(lowest, cfg)} is {need_word(lowest, needs[lowest], cfg)}"
    else:
        desc = f"{word} and {energy_word(en, cfg)}"
    return {"valence": round(valence, 3), "arousal": round(arousal, 3), "word": word,
            "description": desc, "event_nudge": round(event_nudge, 3)}


def initial_mood(needs: dict[str, float], cfg: Config) -> dict[str, Any]:
    return compute_mood(needs, needs, 0.0, None, cfg)

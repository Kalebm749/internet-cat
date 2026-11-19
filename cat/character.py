"""Needs, decay, bands, the separate energy resource, mood. Pure functions over dicts; all numbers from settings."""
from __future__ import annotations

from typing import Any

from cat.config import Config


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


ENERGY_MIN, ENERGY_MAX = -50.0, 50.0


def clamp_energy(v: float) -> float:
    return clamp(v, ENERGY_MIN, ENERGY_MAX)


def initial_needs(cfg: Config) -> dict[str, float]:
    return {k: float(n["start"]) for k, n in cfg.character["needs"].items()}


def decay(needs: dict[str, float], sim_minutes: float, cfg: Config) -> dict[str, float]:
    hours = sim_minutes / 60
    return {k: clamp(v - cfg.character["needs"][k]["decay_per_hour"] * hours) for k, v in needs.items()}


def spend_energy(energy: float, effort: str | None, cfg: Config) -> float:
    """Effort spends energy (a separate resource, -50..50)."""
    return clamp_energy(energy - cfg.settings["effort_energy_cost"].get(effort, 0))


def restore_energy(energy: float, amount: float) -> float:
    """Sleeping and dozing restore energy."""
    return clamp_energy(energy + amount)


def _word_from(thresholds: dict, value: float) -> str:
    for bound in sorted(thresholds):
        if value < bound:
            return thresholds[bound]
    return thresholds[max(thresholds)]


BANDS = ("empty", "low", "okay", "full")      # lowest to highest


def need_band(value: float, cfg: Config) -> str:
    """A level becomes a band, by settings.yaml need_bands. The brain sees the band's words, never the number."""
    nb = cfg.settings["need_bands"]
    if value < nb["empty_below"]:
        return "empty"
    if value < nb["low_below"]:
        return "low"
    if value < nb["okay_below"]:
        return "okay"
    return "full"


def need_word(need_id: str, value: float, cfg: Config) -> str:
    return need_band(value, cfg)


def energy_word(value: float, cfg: Config) -> str:
    return _word_from(cfg.settings["energy_words"], value)


def need_label(need_id: str, cfg: Config) -> str:
    return need_id


def apply_gains(needs: dict[str, float], gains: dict[str, str], cfg: Config,
                consumed: bool = False, multiplier: float = 1.0) -> dict[str, float]:
    """The narrator's buckets -> numbers, clamped. The narrator only adds. The sustenance need only
    gains when something edible was consumed this tick; the repetition multiplier shrinks only the
    needs listed in settings repetition_penalty.needs. Unknown need ids / buckets are ignored."""
    out = dict(needs)
    values = cfg.settings["gain_values"]
    sus = cfg.settings["sustenance_need"]
    slowed = set(cfg.settings["repetition_penalty"].get("needs", []))
    for nid, bucket in (gains or {}).items():
        if nid not in cfg.character["needs"] or str(bucket) not in values:
            continue
        gain = values[str(bucket)]
        if nid == sus:
            if not consumed:
                continue
        elif nid in slowed:
            gain *= multiplier
        out[nid] = clamp(out[nid] + max(0.0, gain))
    if consumed and (gains or {}).get(sus) is None:   # eating guarantees a minimum gain
        out[sus] = clamp(out[sus] + values[str(cfg.settings["consume_min_gain"])])
    return out


def explain_needs(before: dict[str, float], minutes: float, gains: dict[str, str] | None,
                  cfg: Config, after: dict[str, float], consumed: bool = False,
                  multiplier: float = 1.0, decay_multiplier: float = 1.0) -> list[dict]:
    """Per-need breakdown of one tick's arithmetic, one row per need, for the dev dashboard. It only
    *describes*; the real numbers come from `after`.
    Identity: after = before - decay + gain_raw * repetition_mult + clamp_adjust."""
    values = cfg.settings["gain_values"]
    sus = cfg.settings["sustenance_need"]
    slowed = set(cfg.settings["repetition_penalty"].get("needs", []))
    gains = gains or {}
    rows = []
    for nid, spec in cfg.character["needs"].items():
        b = float(before[nid])
        decay = spec["decay_per_hour"] * minutes / 60 * decay_multiplier
        bucket = gains.get(nid)
        bucket = str(bucket) if bucket is not None else None
        raw, mult, shown = 0.0, 1.0, bucket
        if bucket in values:
            if nid == sus:
                if consumed:
                    raw = float(values[bucket])
                else:
                    shown = f"{bucket} (ignored: only consuming something edible raises this)"
            else:
                raw = float(values[bucket])
                mult = multiplier if nid in slowed else 1.0
        elif bucket is not None:
            shown = f"{bucket} (invalid bucket, ignored)"
        if consumed and nid == sus and bucket is None:
            shown = str(cfg.settings["consume_min_gain"])
            raw = float(values[shown])                     # eating guarantees a minimum gain
        nominal = b - decay + raw * mult
        a = float(after[nid])
        rows.append({"need": nid, "before": round(b, 3), "decay": round(decay, 3), "bucket": shown,
                     "gain_raw": round(raw, 3), "repetition_mult": round(mult, 3),
                     "effort_cost": 0.0, "clamp_adjust": round(a - nominal, 3),
                     "after": round(a, 3)})
    return rows


def compute_mood(needs: dict[str, float], before: dict[str, float], event_nudge: float,
                 feeling: str | None, energy: float, cfg: Config, trace: dict | None = None) -> dict[str, Any]:
    """Mood from need levels, need trends (weigh more), recent events, and the brain's own
    `feeling` (blended lightly). Returns valence -1..1, arousal 0..1, a word and a description."""
    m = cfg.settings["mood"]
    level = (sum(needs.values()) / len(needs) - 50) / 50
    trend = clamp(sum(needs[k] - before.get(k, needs[k]) for k in needs) / len(needs)
                  / m["trend_scale"], -1, 1)
    feel = m["feeling_values"].get(feeling or "neutral", 0.0)
    valence = clamp(m["w_level"] * level + m["w_trend"] * trend + m["w_event"] * event_nudge
                    + m["w_reaction"] * feel, -1, 1)
    en = energy
    arousal = clamp(0.2 + 0.4 * (en - ENERGY_MIN) / 100 + 0.4 * abs(trend), 0, 1)

    if trace is not None:   # dev dashboard: every input and intermediate value
        trace.update({
            "level": round(level, 4), "mean_need": round(sum(needs.values()) / len(needs), 3),
            "trend": round(trend, 4), "event_nudge": round(event_nudge, 4), "feeling": feeling or "neutral",
            "feeling_value": feel,
            "weights": {k: m[k] for k in ("w_level", "w_trend", "w_event", "w_reaction")},
            "terms": {"level": round(m["w_level"] * level, 4), "trend": round(m["w_trend"] * trend, 4),
                      "event": round(m["w_event"] * event_nudge, 4), "reaction": round(m["w_reaction"] * feel, 4)},
            "valence_unclamped": round(m["w_level"] * level + m["w_trend"] * trend
                                       + m["w_event"] * event_nudge + m["w_reaction"] * feel, 4),
            "energy": en, "arousal_formula": "0.2 + 0.4*(energy+50)/100 + 0.4*|trend|",
            "thresholds": {"word_threshold": m["word_threshold"], "arousal_high": m["arousal_high"]}})
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
    return compute_mood(needs, needs, 0.0, None, cfg.settings["energy_start"], cfg)

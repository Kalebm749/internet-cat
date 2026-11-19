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


def event_drains(fired: list[dict]) -> dict[str, float]:
    """Total points each need loses from the events that fired this tick (the world drains; the
    narrator never does)."""
    out: dict[str, float] = {}
    for e in fired or []:
        for nid, pts in (e.get("drains") or {}).items():
            out[nid] = out.get(nid, 0.0) + float(pts)
    return out


def dislike_drains(dislikes: list[dict], present: list[str], minutes: float,
                   cfg: Config) -> tuple[dict[str, float], list[dict]]:
    """The world drains needs while something disliked is present. `present` is the text of what is
    around (area, objects here, weather, the action just taken). A dislike matches when its subject
    appears in one of them (case-insensitive). Returns (points per need, the matches used)."""
    dd = cfg.settings["dislike_drain"]
    hay = [t.lower() for t in present if t]
    hits = [d for d in dislikes
            if d["valence"] < 0 and d["subject"].strip() and any(d["subject"].strip().lower() in h for h in hay)]
    hits.sort(key=lambda d: d["valence"])                       # strongest dislike first
    hits = hits[:dd["max_matches"]]
    out: dict[str, float] = {}
    for d in hits:
        pts = dd["points_per_hour"] * minutes / 60 * abs(d["valence"])
        for nid in dd["needs"]:
            out[nid] = out.get(nid, 0.0) + pts
    return out, [{"subject": d["subject"], "valence": d["valence"]} for d in hits]


def apply_drains(needs: dict[str, float], drains: dict[str, float] | None) -> dict[str, float]:
    return {k: clamp(v - (drains or {}).get(k, 0.0)) for k, v in needs.items()}


def add_points(needs: dict[str, float], points: dict[str, float]) -> dict[str, float]:
    """Code-side additions (e.g. finishing a project), clamped 0..100. Unknown need ids are ignored."""
    out = dict(needs)
    for nid, pts in (points or {}).items():
        if nid in out:
            out[nid] = clamp(out[nid] + max(0.0, float(pts)))
    return out


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
                  multiplier: float = 1.0, decay_multiplier: float = 1.0,
                  drains: dict[str, float] | None = None,
                  dislike_drains: dict[str, float] | None = None) -> list[dict]:
    """Per-need breakdown of one tick's arithmetic, one row per need, for the dev dashboard. It only
    *describes*; the real numbers come from `after`.
    Identity: after = before - decay - event_drain - dislike_drain + gain_raw * repetition_mult + clamp_adjust."""
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
        drain = float((drains or {}).get(nid, 0.0))
        dis = float((dislike_drains or {}).get(nid, 0.0))
        nominal = b - decay - drain - dis + raw * mult
        a = float(after[nid])
        rows.append({"need": nid, "before": round(b, 3), "decay": round(decay, 3), "bucket": shown,
                     "gain_raw": round(raw, 3), "repetition_mult": round(mult, 3),
                     "effort_cost": 0.0, "event_drain": round(drain, 3),
                     "dislike_drain": round(dis, 3), "clamp_adjust": round(a - nominal, 3),
                     "after": round(a, 3)})
    return rows


MOOD_MIN, MOOD_MAX = -50.0, 50.0


def clamp_mood(v: float) -> float:
    return clamp(v, MOOD_MIN, MOOD_MAX)


def compute_mood(needs: dict[str, float], event_nudge: float, feeling: str | None, energy: float,
                 cfg: Config, trace: dict | None = None) -> dict[str, Any]:
    """Mood is ONE number, a slider from -50 (negative) to 50 (positive). It is the sum of the
    points settings mood.bands gives each listed need for its BAND, the decaying event nudge, and
    the brain's own `feeling`. Raw levels never enter. `energy` is only used for the description."""
    m = cfg.settings["mood"]
    bands = {nid: need_band(float(v), cfg) for nid, v in needs.items()}
    terms = {nid: float(table.get(bands.get(nid), 0)) for nid, table in m["bands"].items() if nid in bands}
    event_pts = m["event_points"] * event_nudge
    feel = m["feeling_values"].get(feeling or "neutral", 0.0)
    raw = sum(terms.values()) + event_pts + feel
    value = clamp_mood(raw)

    if trace is not None:   # dev dashboard: every input and intermediate value
        trace.update({
            "range": [MOOD_MIN, MOOD_MAX], "bands": bands,
            "terms": {k: round(v, 3) for k, v in terms.items()},
            "event_nudge": round(event_nudge, 4), "event_points": round(event_pts, 3),
            "feeling": feeling or "neutral", "feeling_points": feel,
            "unclamped": round(raw, 3), "thresholds": {"word_threshold": m["word_threshold"]}})
    if value > m["word_threshold"]:
        word = "content"
    elif value < -m["word_threshold"]:
        word = "low"
    else:
        word = "calm"

    pressing = sorted((nid for nid, b in bands.items() if b in ("empty", "low")),
                      key=lambda nid: (BANDS.index(bands[nid]), list(needs).index(nid)))
    if pressing:
        desc = f"{word}, though {need_label(pressing[0], cfg)} is {bands[pressing[0]]}"
    else:
        desc = f"{word} and {energy_word(energy, cfg)}"
    return {"value": round(value, 1), "word": word, "description": desc,
            "event_nudge": round(event_nudge, 3)}


def initial_mood(needs: dict[str, float], cfg: Config) -> dict[str, Any]:
    return compute_mood(needs, 0.0, None, float(cfg.settings["energy_start"]), cfg)

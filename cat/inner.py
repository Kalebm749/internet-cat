"""Inner life (docs/individuality-plan.md §5): temperament, affect and needs.

A creature's temperament is a set of numbers in its character sheet (0..1, written by the author, never changed by
the creature). They are inputs to *code*: they scale how fast each need grows, how hard an outcome lands, how
much an affect reacts. Needs and affect are persistent state updated here, without any model call:

    needs    novelty, social, solitude, expression, accomplishment, belonging, security   0 (met) .. 1 (pressing)
             (rest is the body's energy and is not duplicated here)
    affect   stress, confidence, curiosity, loneliness, frustration, satisfaction          0 .. 1

They can contradict each other (curious and stressed, lonely and wanting solitude). They never remove, add or rank
an option and never choose anything; they become plain words in the prompt and notes on the options they bear on,
and they change how outcomes land (relief of a pressing need lifts mood). Every change carries its cause and is
recorded in `body_changes` next to the body's own, so the dashboard can explain any value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

NEEDS = ("novelty", "social", "solitude", "expression", "accomplishment", "belonging", "security")
AFFECT = ("stress", "confidence", "curiosity", "loneliness", "frustration", "satisfaction")
VARS = NEEDS + AFFECT
DIMENSIONS = ("novelty", "sociability", "solitude", "caution", "impulsiveness", "persistence", "volatility",
              "self_criticism", "expressiveness", "ambition")
# which temperament dimension scales how fast a need grows
NEED_DIMENSION = {"novelty": "novelty", "social": "sociability", "solitude": "solitude", "expression": "expressiveness",
                  "accomplishment": "ambition", "belonging": "sociability", "security": "caution"}


@dataclass
class Change:
    var: str
    delta: float
    cause: str


@dataclass
class Inner:
    v: dict[str, float]
    pending: dict[str, float] = field(default_factory=dict)       # changes not yet written to body_changes

    def get(self, var: str) -> float:
        return self.v[var]

    def as_dict(self) -> dict[str, float]:
        return {k: round(x, 5) for k, x in self.v.items()}

    @staticmethod
    def start(t: Mapping[str, Any]) -> "Inner":
        return Inner({var: float(t["start"][var]) for var in VARS})

    @staticmethod
    def load(data: Mapping[str, Any] | None, t: Mapping[str, Any]) -> "Inner":
        inner = Inner.start(t)
        for var, x in ((data or {}).get("inner") or {}).items():
            if var in inner.v:
                inner.v[var] = float(x)
        inner.pending = {k: float(x) for k, x in ((data or {}).get("inner_pending") or {}).items() if k in VARS}
        return inner

    def state(self) -> dict:
        return {"inner": self.as_dict(), "inner_pending": {k: round(x, 5) for k, x in self.pending.items() if x}}


def trait(temperament: Mapping[str, Any] | None, dim: str) -> float:
    v = (temperament or {}).get(dim, 0.5)
    return min(1.0, max(0.0, float(v)))


def scale(temperament: Mapping[str, Any] | None, dim: str) -> float:
    """0.5 (low) .. 1.5 (high): how strongly this creature reacts along a dimension."""
    return 0.5 + trait(temperament, dim)


def _add(inner: Inner, var: str, amount: float) -> float:
    before = inner.v[var]
    inner.v[var] = min(1.0, max(0.0, before + amount))
    done = inner.v[var] - before
    if done:
        inner.pending[var] = inner.pending.get(var, 0.0) + done
    return done


def _toward(inner: Inner, var: str, target: float, rate: float) -> None:
    gap = target - inner.v[var]
    _add(inner, var, max(-rate, min(rate, gap)))


# ---------------------------------------------------------------- time

def passage(inner: Inner, temperament: Mapping[str, Any] | None, t: Mapping[str, Any], minutes: float, *,
            asleep: bool, hunger: float, energy: float) -> float:
    """Let `minutes` pass. Needs grow (or fall in quiet) by their own rates scaled by temperament; affect moves toward
    what the body and the needs imply. Returns the mood change the inner state causes (stress and frustration drag
    it down, satisfaction lifts it); the caller applies it through the body."""
    h = minutes / 60.0
    awake = 0.4 if asleep else 1.0
    for var in NEEDS:
        rise = t["rise_per_hour"].get(var, 0.0) * scale(temperament, NEED_DIMENSION[var]) * awake
        fall = t["fall_per_hour"].get(var, 0.0)
        _add(inner, var, (rise - fall) * h)
    loud = t["loud"]
    pressing = sum(max(0.0, inner.v[n] - loud) for n in NEEDS)
    vol = scale(temperament, "volatility")
    stress_target = min(1.0, (0.8 * max(0.0, (hunger - 0.5) / 0.5) + 0.6 * max(0.0, (0.3 - energy) / 0.3)
                              + 0.3 * pressing) * vol)
    _toward(inner, "stress", stress_target, t["affect_rate_per_hour"] * h * (2.0 if stress_target > inner.v["stress"] else 1.0))
    _toward(inner, "frustration", 0.0, t["frustration_decay_per_hour"] * h)
    _toward(inner, "satisfaction", t["satisfaction_rest"], t["satisfaction_decay_per_hour"] * h)
    _toward(inner, "confidence", 0.5, t["confidence_drift_per_hour"] * h)
    _toward(inner, "curiosity", min(1.0, inner.v["novelty"] * scale(temperament, "novelty")),
            t["affect_rate_per_hour"] * h)
    _toward(inner, "loneliness", min(1.0, max(inner.v["social"], inner.v["belonging"]) * scale(temperament, "sociability")),
            t["affect_rate_per_hour"] * 0.5 * h)
    s, f, sat = inner.v["stress"], inner.v["frustration"], inner.v["satisfaction"]
    return h * (-t["mood_from_stress"] * max(0.0, s - 0.4) - t["mood_from_frustration"] * max(0.0, f - 0.4)
                + t["mood_from_satisfaction"] * max(0.0, sat - 0.5))


# ---------------------------------------------------------------- what happens

def finished(inner: Inner, temperament: Mapping[str, Any] | None, t: Mapping[str, Any],
             effects: Mapping[str, float] | None, freshness: float) -> float:
    """An activity ended well. Its declared inner effects land scaled by who is experiencing them (needs by how fresh
    the activity was, affect by volatility); a fresh activity also eases novelty and a stale one frustrates.
    Returns the mood this lifts for relieving a pressing need (appraisal)."""
    relief = 0.0
    for var, d in (effects or {}).items():
        if var not in inner.v:
            continue
        if var in NEEDS:
            amount = d * (0.3 + 0.7 * freshness)
            if d < 0 and inner.v[var] >= t["note_min"]:
                relief += -amount * (inner.v[var])
            _add(inner, var, amount * (1.0 if d < 0 else scale(temperament, NEED_DIMENSION[var])))
        else:
            _add(inner, var, d * scale(temperament, "volatility"))
    if freshness >= 0.8:
        eased = _add(inner, "novelty", -t["fresh_novelty_relief"])
        relief += -eased * 0.5
    elif freshness < t["stale_below"]:
        _add(inner, "frustration", t["stale_frustration"] * (1.5 - trait(temperament, "persistence")))
        _add(inner, "novelty", t["stale_novelty_rise"])
    return min(t["relief_mood_cap"], t["relief_mood"] * relief)


def rejected(inner: Inner, temperament: Mapping[str, Any] | None, t: Mapping[str, Any]) -> None:
    """An action could not happen. Frustration rises (less for a persistent creature), confidence dips (more for a
    self-critical one)."""
    _add(inner, "frustration", t["reject_frustration"] * (1.5 - trait(temperament, "persistence")))
    _add(inner, "confidence", -t["reject_confidence"] * scale(temperament, "self_criticism"))
    _add(inner, "stress", t["reject_stress"] * scale(temperament, "volatility"))


def flush(inner: Inner, minimum: float, cause: str = "inner") -> list[Change]:
    """The accumulated changes big enough to record, as Changes; the rest keep accumulating."""
    out = []
    for var in list(inner.pending):
        d = inner.pending[var]
        if abs(d) >= minimum:
            out.append(Change(var, round(d, 6), cause))
            inner.pending[var] = 0.0
    return out


# ---------------------------------------------------------------- what it feels like

def practice(times_done: int, t: Mapping[str, Any]) -> float:
    """How practised it is at something: 0 (new) .. 1, rising with every go and saturating."""
    import math
    return 1.0 - math.exp(-max(0, times_done) / t["practice_k"])


def hold_boost(holds: list | None, text: str, t: Mapping[str, Any]) -> float:
    """What it holds to makes the things that touch it more likely to reach it: an event naming one of its keywords
    is more salient by `hold_boost` x the strength of the value."""
    low = text.lower()
    best = 0.0
    for h in holds or []:
        if any(str(k).lower() in low for k in h.get("keywords", [])):
            best = max(best, float(h.get("strength", 0.5)))
    return t["hold_boost"] * best


def pressing(inner: Inner, t: Mapping[str, Any]) -> list[str]:
    """The needs at or above `loud`, most pressing first."""
    return [n for n in sorted(NEEDS, key=lambda n: -inner.v[n]) if inner.v[n] >= t["loud"]]


def describe(inner: Inner, words: Mapping[str, Any] | None, t: Mapping[str, Any]) -> list[str]:
    """Plain words (content/world.yaml `inner_words`) for what is strong right now: the most pressing first, at most
    `max_words`. A variable has `from` (the value from which it is felt) or `below` (felt when low, e.g. confidence)."""
    felt = []
    for var, spec in (words or {}).items():
        if var not in inner.v:
            continue
        v = inner.v[var]
        if "from" in spec and v >= spec["from"]:
            felt.append((v - spec["from"] + 1.0, spec["words"]))
        elif "below" in spec and v <= spec["below"]:
            felt.append((spec["below"] - v + 1.0, spec["words"]))
    felt.sort(key=lambda x: -x[0])
    return [w for _, w in felt[: t["max_words"]]]


def relief_notes(inner: Inner, words: Mapping[str, Any] | None, t: Mapping[str, Any],
                 effects: Mapping[str, float] | None, text: str) -> list[str]:
    """Option notes: what this action could ease among the needs that are pressing now."""
    out = []
    for var, d in (effects or {}).items():
        if var in NEEDS and d < 0 and inner.v[var] >= t["note_min"]:
            what = ((words or {}).get(var) or {}).get("short")
            if what:
                out.append(text.format(what=what))
    return out

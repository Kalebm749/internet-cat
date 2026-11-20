"""The three variables (docs/rewrite-contracts.md §2.2, §2.3): mood, energy and hunger.

    mood    -1..1   drifts toward the baseline; hunger above a threshold drags it down
    energy   0..1   spent by being awake and by effort; restored by sleep (less when hungry) and resting
    hunger   0..1   rises with time (slower asleep) and with effort; eating lowers it

Code owns every number here. Each change carries its cause, so the dashboard can explain any value.
`passage` advances time, `apply` applies an action's creature effects, `describe` turns the body into
words (body words by band, and the derived condition). Nothing here picks an action.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

RANGES = {"mood": (-1.0, 1.0), "energy": (0.0, 1.0), "hunger": (0.0, 1.0)}
VARS = tuple(RANGES)


@dataclass
class BodyState:
    mood: float
    energy: float
    hunger: float

    def get(self, var: str) -> float:
        return getattr(self, var)

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass
class Change:
    var: str
    delta: float      # what was actually applied (after clamping)
    cause: str        # e.g. "time", "sleep", "effort (inspect)", "drift", "hunger", "eat_offering"


@dataclass
class Description:
    words: list[str] = field(default_factory=list)   # body words that apply now, hunger first
    derived: str | None = None                       # name of the derived condition, if any
    derived_words: str | None = None


def _clamp(var: str, v: float) -> float:
    lo, hi = RANGES[var]
    return min(hi, max(lo, v))


class Body:
    def __init__(self, tuning_body: Mapping[str, Any], world: Mapping[str, Any]):
        self.t = tuning_body
        self.world = world

    @staticmethod
    def start(tuning_body: Mapping[str, Any]) -> BodyState:
        s = tuning_body["start"]
        return BodyState(mood=float(s["mood"]), energy=float(s["energy"]), hunger=float(s["hunger"]))

    # ---------------------------------------------------------------- changes

    def _add(self, st: BodyState, acc: dict, var: str, amount: float, cause: str) -> None:
        if amount == 0:
            return
        before = st.get(var)
        after = _clamp(var, before + amount)
        setattr(st, var, after)
        if after != before:
            acc[(var, cause)] = acc.get((var, cause), 0.0) + (after - before)

    @staticmethod
    def _changes(acc: dict) -> list[Change]:
        return [Change(var, round(d, 9), cause) for (var, cause), d in acc.items() if abs(d) > 1e-12]

    def apply(self, st: BodyState, effects: Mapping[str, float], cause: str) -> list[Change]:
        """An action's instant creature effects ({mood, energy, hunger} deltas), clamped. Mutates `st`."""
        acc: dict = {}
        for var in VARS:
            if var in effects:
                self._add(st, acc, var, float(effects[var]), cause)
        return self._changes(acc)

    def passage(self, st: BodyState, minutes: float, *, asleep: bool = False, effort_per_min: float = 0.0,
                per_minute: Mapping[str, float] | None = None, cause: str = "") -> list[Change]:
        """Advance the body by `minutes` sim minutes. Mutates `st` and returns the changes by cause.

        `effort_per_min` is the energy the current activity costs per minute (awake only); `per_minute` are the
        activity's own creature effects per minute (e.g. resting restores energy); `cause` names the activity."""
        t = self.t
        acc: dict = {}
        tag = f" ({cause})" if cause else ""
        left = float(minutes)
        while left > 1e-9:
            dt = min(left, float(t["step_minutes"]))
            left -= dt
            h = dt / 60.0
            if asleep:
                self._add(st, acc, "hunger", t["hunger_per_hour"] * t["hunger_sleep_factor"] * h, "sleep")
                restore = t["sleep_energy_per_hour"] * h * (1 - t["hungry_sleep_penalty"] * st.hunger)
                self._add(st, acc, "energy", restore, "sleep")
            else:
                self._add(st, acc, "hunger", t["hunger_per_hour"] * h, "time")
                self._add(st, acc, "energy", -t["awake_energy_per_hour"] * h, "time")
                if effort_per_min > 0:
                    spent = min(st.energy, effort_per_min * dt)
                    self._add(st, acc, "energy", -spent, "effort" + tag)
                    self._add(st, acc, "hunger", t["hunger_per_effort"] * spent, "effort" + tag)
            for var, rate in (per_minute or {}).items():
                self._add(st, acc, var, rate * dt, cause or "activity")
            gap = t["mood_baseline"] - st.mood
            step = t["mood_drift_per_hour"] * h
            self._add(st, acc, "mood", max(-step, min(step, gap)), "drift")
            if st.hunger > t["hunger_mood_threshold"]:
                self._add(st, acc, "mood", -t["hunger_mood_drag"] * (st.hunger - t["hunger_mood_threshold"]) * h,
                          "hunger")
        return self._changes(acc)

    # ---------------------------------------------------------------- words

    def describe(self, st: BodyState, repetition: float = 0.0) -> Description:
        """Body words by band (content/world.yaml body_words) and the first matching derived condition.
        `repetition` (0..1) comes from behaviour history (§6); until that exists it is 0."""
        d = Description()
        bw = self.world.get("body_words") or {}
        for var in ("hunger", "energy", "mood"):
            band = None
            for b in bw.get(var) or []:
                if st.get(var) >= b["from"]:
                    band = b
            if band and band["words"]:
                d.words.append(band["words"])
        values = {"mood": st.mood, "energy": st.energy, "repetition": repetition}
        for dc in self.world.get("derived_conditions") or []:
            if all(dc[k][0] <= values[k] <= dc[k][1] for k in ("mood", "energy", "repetition") if k in dc):
                d.derived, d.derived_words = dc["name"], dc["words"]
                break
        return d

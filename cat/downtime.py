"""Downtime policy: what to do about the real time the engine (or the whole host) was off.

  pause    sim time resumes exactly where it stopped; nothing decays during the gap (default).
  catch_up the gap is converted to sim time at the normal pace (sim_minutes_per_tick_default per
           tick_interval_seconds), capped by max_catch_up_hours, decay is applied, and ONE diary
           entry ("dozed through the night") is written before the loop resumes.

Pure functions here; the engine applies the result (Engine.startup_downtime)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from cat import clock
from cat.config import Config


@dataclass
class CatchUp:
    gap_seconds: float         # real downtime, after the normal between-ticks wait was subtracted
    raw_sim_minutes: float     # what the gap is worth at the normal pace
    sim_minutes: int           # after the cap (what is applied)
    capped: bool


def plan(cfg: Config, now: float, last_alive: float, clean_stop: bool) -> CatchUp | None:
    """None if there is nothing to catch up: a short gap (a normal restart), or no sim pace to
    convert with (tick_interval_seconds = 0, e.g. mock runs)."""
    s = cfg.settings
    interval = s["tick_interval_seconds"]
    if interval <= 0:
        return None
    # After a crash the last sign of life can be up to one tick interval older than the real
    # stop; after a clean stop it is exact.
    gap = max(0.0, now - last_alive - (0 if clean_stop else interval))
    if gap < s["catch_up_min_gap_ticks"] * interval:
        return None
    raw = gap / interval * s["sim_minutes_per_tick_default"]
    cap = s["max_catch_up_hours"] * 60
    minutes = int(min(raw, cap))
    if minutes < 1:
        return None
    return CatchUp(gap, raw, minutes, raw > cap)


def spans_night(start: datetime, minutes: float, phases: dict[str, int]) -> bool:
    """Does [start, start+minutes) include any night-phase time?"""
    t, end = start, start + timedelta(minutes=minutes)
    while t < end:
        if clock.is_night(t, phases):
            return True
        t += timedelta(minutes=30)
    return clock.is_night(end, phases)


def description(start_time: str, minutes: float, phases: dict[str, int]) -> str:
    """'dozed through the night' / 'dozed through the afternoon' ..."""
    start = clock.parse(start_time)
    if spans_night(start, minutes, phases):
        return "dozed through the night"
    return f"dozed through the {clock.part_of_day(start)}"

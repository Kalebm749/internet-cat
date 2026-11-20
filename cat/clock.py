"""Simulated time. The sim clock is a naive local datetime stored as 'YYYY-MM-DDTHH:MM'."""
from __future__ import annotations

from datetime import datetime, timedelta

FMT = "%Y-%m-%dT%H:%M"


def parse(s: str) -> datetime:
    return datetime.strptime(s, FMT)


def fmt(dt: datetime) -> str:
    return dt.strftime(FMT)


def advance(s: str, minutes: float) -> str:
    return fmt(parse(s) + timedelta(minutes=minutes))


def hour_float(dt: datetime) -> float:
    return dt.hour + dt.minute / 60


def phase(dt: datetime, phases: dict[str, int]) -> str:
    """dawn | day | dusk | night, from the start hours in settings. Wraps around midnight."""
    h = hour_float(dt)
    ordered = sorted(phases.items(), key=lambda kv: kv[1])
    current = ordered[-1][0]  # before the earliest start => still in the last phase of yesterday
    for name, start in ordered:
        if h >= start:
            current = name
    return current


def is_night(dt: datetime, phases: dict[str, int]) -> bool:
    return phase(dt, phases) == "night"


def hour_in_range(dt: datetime, lo: float, hi: float) -> bool:
    """True if the hour is in [lo, hi). Handles ranges that wrap midnight (e.g. [22, 4))."""
    h = hour_float(dt)
    return lo <= h < hi if lo <= hi else (h >= lo or h < hi)


def day_number(first: datetime, now: datetime) -> int:
    return (now.date() - first.date()).days + 1


def time_label(dt: datetime) -> str:
    h12 = dt.hour % 12 or 12
    return f"{h12}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'}"


def day_label(first: datetime, now: datetime) -> str:
    return f"{now.strftime('%A')}, Day {day_number(first, now)}"


def part_of_day(dt: datetime) -> str:
    h = dt.hour
    if h < 5:
        return "night"
    if h < 12:
        return "morning"
    if h < 17:
        return "afternoon"
    if h < 21:
        return "evening"
    return "night"


def now_real() -> str:
    """The real wall-clock time, to the second (for real_time columns and logs)."""
    from datetime import datetime as _dt
    return _dt.now().isoformat(timespec="seconds")

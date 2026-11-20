"""When to decide (docs/rewrite-contracts.md §3.1, §3.2).

    activity_finished   the current activity ended                         -> full menu
    event / mail / body something happened (event salience >= minimum)     -> interrupt (continue, or the menu);
                                                                              full menu when nothing is running
    interval            nothing is running and `interval_seconds` (REAL) passed since the last decision started
    check_in            an activity has run `check_in_minutes` (SIM) since it started or since the last decision

One decision at a time: triggers that arrive while a decision is in flight are queued and merged into the
next one. The interval is a target: it never interrupts anything. While an activity runs and nothing
happens, there are no decisions except check-ins; an activity that is not `continuous` (moving, inspecting,
sleeping) cannot be interrupted, so triggers wait until it ends. Pure logic: the caller passes the clocks in.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from cat import clock

TRIGGERS = ("activity_finished", "event", "mail", "body", "interval", "check_in")


@dataclass
class Trigger:
    kind: str
    reason: str = ""


@dataclass
class Due:
    kind: str                                   # decision kind: full | interrupt | check_in
    triggers: list[Trigger] = field(default_factory=list)


class Scheduler:
    def __init__(self, decisions: dict):
        self.interval = float(decisions["interval_seconds"])
        self.check_in = float(decisions["check_in_minutes"])
        self.salience_min = float(decisions["event_salience_min"])
        self.pending: list[Trigger] = []
        self.in_flight = False
        self.last_real: float | None = None     # real time the last decision started
        self.last_sim: str | None = None        # sim time the last decision started

    def note(self, kind: str, reason: str = "", salience: float = 1.0) -> bool:
        """Something happened. Returns False when it is not worth a decision (a faint event)."""
        if kind not in TRIGGERS or kind in ("interval", "check_in"):
            raise ValueError(f"cannot note a {kind!r} trigger")
        if kind == "event" and salience < self.salience_min:
            return False
        self.pending.append(Trigger(kind, reason))
        return True

    def due(self, now_real: float, sim_time: str, activity: dict | None) -> Due | None:
        """The decision to make now, if any. `activity` is None or has `started` (sim time)."""
        if self.in_flight:
            return None
        if activity is not None and not activity.get("continuous", True):
            return None                     # moving, inspecting, sleeping: triggers wait until it ends
        if self.pending:
            finished = any(t.kind == "activity_finished" for t in self.pending)
            return Due("full" if finished or activity is None else "interrupt", list(self.pending))
        if activity is None:
            if self.last_real is None or now_real - self.last_real >= self.interval:
                return Due("full", [Trigger("interval")])
            return None
        since = max(activity["started"], self.last_sim or activity["started"])
        minutes = (clock.parse(sim_time) - clock.parse(since)).total_seconds() / 60
        if minutes >= self.check_in:
            return Due("check_in", [Trigger("check_in", f"{round(minutes)} minutes")])
        return None

    def started(self, due: Due, now_real: float, sim_time: str) -> None:
        """A decision for `due` began: its triggers are consumed; new ones wait for the next decision."""
        self.in_flight = True
        consumed = {id(t) for t in due.triggers}
        self.pending = [t for t in self.pending if id(t) not in consumed]
        self.last_real, self.last_sim = now_real, sim_time

    def finished(self) -> None:
        self.in_flight = False

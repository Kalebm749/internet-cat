"""Behaviour history and freshness (docs/rewrite-contracts.md §6).

Each activity row records its family, a normalised signature of its inputs (its target), a signature of the facts
named in its action's `revisit_key`, and the finished instance it restarted (same action and target started again
within `restart_window_minutes`). Continuing an activity is one row: it never counts as a repeat.

    freshness = clamp(1 - w_action x same action - w_family x same family - w_target x same target
                        - w_aimless x an aimless return, freshness_min, 1)

Each count is a sum over past instances of 0.5 ^ (age / half_life). When an action has a `revisit_key` and those
facts changed since its last instance, the same-action and same-target counts are zero: a meaningful return.
An aimless return is going back to a place where, last time, it did nothing but move and inspect.
Freshness multiplies the positive mood effects of the action and puts a note on the menu; it never removes
an option.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from cat import clock
from cat.content import ActionSpec, Content

DONE = ("finished", "interrupted")


def input_signature(inputs: dict | None) -> str:
    def norm(v: Any) -> Any:
        if isinstance(v, str):
            return re.sub(r"\s+", " ", v.strip().lower())
        return v
    return json.dumps({k: norm(v) for k, v in sorted((inputs or {}).items())}, ensure_ascii=False)


def revisit_signature(action: ActionSpec, facts: dict) -> str | None:
    if not action.revisit_key:
        return None
    blob = json.dumps({k: facts.get(k) for k in action.revisit_key}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


@dataclass
class Freshness:
    value: float
    parts: dict = field(default_factory=dict)     # action, family, target, aimless (the decayed counts)
    revisit_changed: bool = False
    restart_of: int | None = None


def _decay(age_minutes: float, half_life: float) -> float:
    return 0.5 ** (max(0.0, age_minutes) / half_life)


def _minutes(a: str, b: str) -> float:
    return (clock.parse(b) - clock.parse(a)).total_seconds() / 60


def _history(conn: sqlite3.Connection, now: str, hours: float) -> list[sqlite3.Row]:
    since = clock.fmt(clock.parse(now) - timedelta(hours=hours))
    return conn.execute("SELECT * FROM activities WHERE status IN ('finished', 'interrupted') AND started_at >= ? "
                        "ORDER BY id", (since,)).fetchall()


def _aimless(rows: list[sqlite3.Row], content: Content, dest: str, now: str, half_life: float) -> float:
    """1 (decayed) when the last stay at `dest` held nothing but moving and inspecting."""
    block: list[sqlite3.Row] = []
    for r in reversed(rows):
        if r["location"] == dest:
            block.append(r)
        elif block:
            break
    if not block:
        return 0.0
    builtin = {a.id for a in content.actions.values() if a.builtin in ("move", "inspect")}
    if all(r["action_id"] in builtin for r in block):
        return _decay(_minutes(block[0]["ended_at"] or block[0]["started_at"], now), half_life)
    return 0.0


def freshness(conn: sqlite3.Connection, content: Content, tuning: dict, action: ActionSpec, inputs: dict | None,
              now: str, facts: dict) -> Freshness:
    """How fresh doing this would be now. `inputs` None: judged without its target (for the menu)."""
    rp = tuning["repetition"]
    hl = float(rp["half_life_minutes"])
    rows = _history(conn, now, rp["window_hours"])
    sig = input_signature(inputs) if inputs is not None else None
    rsig = revisit_signature(action, facts)
    same_action = [r for r in rows if r["action_id"] == action.id]
    changed = bool(rsig and same_action and same_action[-1]["revisit_sig"] != rsig)
    parts = {"action": 0.0, "family": 0.0, "target": 0.0, "aimless": 0.0}
    for r in rows:
        w = _decay(_minutes(r["ended_at"] or r["started_at"], now), hl)
        if r["action_id"] == action.id:
            if not changed:
                parts["action"] += w
                if sig is not None and r["input_sig"] == sig:
                    parts["target"] += w
        elif r["family"] == action.family:
            parts["family"] += w
    if action.builtin == "move" and inputs and "to" in inputs:
        parts["aimless"] = _aimless(rows, content, inputs["to"], now, hl)
    value = 1 - (rp["w_action"] * parts["action"] + rp["w_family"] * parts["family"]
                 + rp["w_target"] * parts["target"] + rp["w_aimless"] * parts["aimless"])
    restart = None
    if sig is not None:
        for r in reversed(same_action):
            if r["status"] == "finished" and r["input_sig"] == sig and r["ended_at"] \
                    and _minutes(r["ended_at"], now) <= rp["restart_window_minutes"]:
                restart = r["id"]
                break
    return Freshness(value=round(max(rp["freshness_min"], min(1.0, value)), 4),
                     parts={k: round(v, 4) for k, v in parts.items()}, revisit_changed=changed, restart_of=restart)


def repetition_score(conn: sqlite3.Connection, n: int) -> float:
    """0 (all fresh) .. 1 (all stale): 1 minus the mean freshness of the last n activities."""
    rows = conn.execute("SELECT freshness FROM activities WHERE status IN ('finished', 'interrupted') "
                        "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        return 0.0
    return round(1 - sum(r[0] for r in rows) / len(rows), 4)

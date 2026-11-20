"""What the creature knows it can do (docs/rewrite-contracts.md §5.4).

The menu is built from knowledge ∩ world: an action is offered only if the creature knows it. Innate actions
are known from birth; `known_on_arrival` actions become known on the first arrival at their location; hidden
ones through `inspect` or another action's `discover`. A discovery is usable at once and stays provisional
until sleep confirms it (step 5). If an action leaves the content its row stays but it is not offered.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from cat import clock
from cat.content import Content

HOW = ("innate", "arrival", "inspect", "effect")


@dataclass
class Known:
    action_id: str
    location: str
    discovered_at: str
    discovered_by: str
    provisional: bool
    own_words: str | None
    times_done: int
    last_done_at: str | None
    last_outcome: str | None


def learn(conn: sqlite3.Connection, action_id: str, location: str, how: str, sim_time: str) -> bool:
    """Record a discovery. Returns False if it was already known (nothing changes)."""
    if how not in HOW:
        raise ValueError(f"unknown way of learning {how!r}")
    cur = conn.execute("INSERT OR IGNORE INTO knowledge(action_id, location, discovered_at, discovered_by, provisional) "
                       "VALUES (?, ?, ?, ?, ?)", (action_id, location, sim_time, how, 0 if how == "innate" else 1))
    return cur.rowcount == 1


def ensure_innate(conn: sqlite3.Connection, content: Content, sim_time: str) -> list[str]:
    """Know every innate action (also ones added to the content later). Returns the newly known ids."""
    return [a for a in content.character["innate_knowledge"]
            if learn(conn, a, content.actions[a].location or "", "innate", sim_time)]


def arrive(conn: sqlite3.Connection, content: Content, location: str, sim_time: str) -> list[str]:
    """Arriving somewhere: its `known_on_arrival` actions become known. Returns the newly known ids."""
    return [a for a in content.locations[location].actions
            if content.actions[a].discovery == "known_on_arrival" and learn(conn, a, location, "arrival", sim_time)]


def done(conn: sqlite3.Connection, action_id: str, sim_time: str, outcome: str | None) -> None:
    conn.execute("UPDATE knowledge SET times_done = times_done + 1, last_done_at = ?, last_outcome = ? "
                 "WHERE action_id = ?", (sim_time, outcome, action_id))


def _row(r: sqlite3.Row) -> Known:
    return Known(r["action_id"], r["location"], r["discovered_at"], r["discovered_by"], bool(r["provisional"]),
                 r["own_words"], r["times_done"], r["last_done_at"], r["last_outcome"])


def all_known(conn: sqlite3.Connection) -> dict[str, Known]:
    return {r["action_id"]: _row(r) for r in conn.execute("SELECT * FROM knowledge ORDER BY discovered_at, action_id")}


def known_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT action_id FROM knowledge")}


def minutes_since_done(conn: sqlite3.Connection, now: str) -> dict[str, float]:
    """action id -> sim minutes since it was last done (only actions done at least once)."""
    t = clock.parse(now)
    return {r[0]: (t - clock.parse(r[1])).total_seconds() / 60
            for r in conn.execute("SELECT action_id, last_done_at FROM knowledge WHERE last_done_at IS NOT NULL")}

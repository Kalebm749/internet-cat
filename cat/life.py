"""Opening the life: spawn the creature the first time, load it afterwards (docs/rewrite-contracts.md §2, §4.2, §9).

`open_life` migrates `nit.db`, then either spawns (sim clock at world.yaml `sim_start`, at the character's
start location, with the tuning's start body, a fresh world and character version 1) or loads the stored
life and brings it in step with the content (world facts, the author's part of the sheet, a location that
no longer exists). Everything it writes happens in one transaction.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field

from cat import identity, knowledge, store
from cat.body import Body, BodyState, Change
from cat.inner import Inner, VARS as INNER_VARS
from cat.content import Content
from cat.worldstate import SyncReport as WorldSync
from cat.worldstate import WorldState

log = logging.getLogger("cat.life")


@dataclass
class Life:
    content: Content
    body: Body
    bodystate: BodyState
    world: WorldState
    character: identity.Version
    sim_time: str
    location: str
    asleep: bool = False
    spawned: bool = False                               # True when this call created the life
    notes: list[str] = field(default_factory=list)      # what the sync changed, for the log and dashboard
    exit_notes: dict = field(default_factory=dict)      # place id -> a note from its own history (set before deciding)
    stay_minutes: float = 0.0                           # sim minutes at this place since arriving
    inner: Inner | None = None                          # needs and affect (cat/inner.py)

    def state_data(self) -> dict:
        return {"sim_time": self.sim_time, "location": self.location, "asleep": self.asleep,
                **self.bodystate.as_dict(), **(self.inner.state() if self.inner else {})}


def _save_state(conn: sqlite3.Connection, life: Life) -> None:
    conn.execute("INSERT INTO state(id, data, updated_at) VALUES (1, ?, ?) "
                 "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                 (json.dumps(life.state_data()), life.sim_time))


def save(conn: sqlite3.Connection, life: Life) -> None:
    """Store the creature's state and the changed world facts (inside the caller's transaction)."""
    _save_state(conn, life)
    life.world.save(conn)


def record_body(conn: sqlite3.Connection, life: Life, changes: list[Change]) -> None:
    """Store body changes with their causes (inside the caller's transaction)."""
    conn.executemany("INSERT INTO body_changes(sim_time, var, delta, value, cause) VALUES (?, ?, ?, ?, ?)",
                     [(life.sim_time, c.var, c.delta,
                       life.inner.get(c.var) if c.var in INNER_VARS and life.inner else life.bodystate.get(c.var), c.cause)
                      for c in changes])


def _world_notes(rep: WorldSync) -> list[str]:
    notes = []
    for label, keys in (("new facts", rep.added), ("removed facts", rep.removed),
                        ("facts following a new default", rep.updated), ("facts reset (type changed)", rep.reset)):
        if keys:
            notes.append(f"{label}: {', '.join(keys)}")
    return notes


def open_life(conn: sqlite3.Connection, content: Content, tuning: dict) -> Life:
    store.migrate(conn)
    body = Body(tuning["body"], content.world)
    with store.transaction(conn):
        row = conn.execute("SELECT data FROM state WHERE id = 1").fetchone()
        if row is None:
            sim_time = str(content.world.get("sim_start") or "2026-11-21T07:00")
            world = WorldState.fresh(content)
            world.save(conn, all=True)
            ver_rep = identity.ensure(conn, content, sim_time)
            life = Life(content, body, Body.start(tuning["body"]), world, identity.current(conn), sim_time,
                        content.character["start_location"], spawned=True, inner=Inner.start(tuning["inner"]))
            knowledge.ensure_innate(conn, content, sim_time)
            knowledge.arrive(conn, content, life.location, sim_time)
            life.notes.append(f"spawned at {life.location}, {sim_time}, character version {ver_rep.version}")
        else:
            data = json.loads(row["data"])
            world, wrep = WorldState.load(conn, content)
            world.save(conn)
            ver_rep = identity.ensure(conn, content, data["sim_time"])
            life = Life(content, body, BodyState(data["mood"], data["energy"], data["hunger"]), world,
                        identity.current(conn), data["sim_time"], data["location"], bool(data.get("asleep")),
                        inner=Inner.load(data, tuning["inner"]))
            life.notes += _world_notes(wrep)
            if ver_rep.author_changes:
                life.notes.append(f"author edited {', '.join(ver_rep.author_changes)}: character version {ver_rep.version}")
            if ver_rep.ignored_seed_changes:
                life.notes.append("character.yaml edits to " + ", ".join(ver_rep.ignored_seed_changes)
                                  + " are ignored: after spawn only the creature changes these")
            if life.location not in content.locations:
                life.notes.append(f"location '{life.location}' no longer exists: moved to "
                                  f"{content.character['start_location']}")
                life.location = content.character["start_location"]
                knowledge.arrive(conn, content, life.location, life.sim_time)
            new = knowledge.ensure_innate(conn, content, life.sim_time)
            if new:
                life.notes.append("newly innate: " + ", ".join(new))
        _save_state(conn, life)
    for n in life.notes:
        log.info(n)
    return life

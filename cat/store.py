"""The new life's database, `data/nit.db` (docs/rewrite-contracts.md §9).

A fresh schema, separate from the old `cat.db`. Tables are added here step by step as the rewrite
grows; each change is a numbered entry in MIGRATIONS (a list of statements) so a running life is always
carried forward. Connections and transactions reuse `cat.db.connect` / `cat.db.transaction`.
"""
from __future__ import annotations

import sqlite3

from cat.db import connect, transaction  # noqa: F401  (re-exported for callers)

MIGRATIONS: list[list[str]] = [
    [  # 1: meta and world facts
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        # value: JSON. touched = 1 when the creature's actions changed it (a content reload keeps it).
        # base: JSON of the content default in force when this row was written (a changed default is noticed).
        # seq: the id counter for list items.
        """CREATE TABLE facts (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL,
            base    TEXT NOT NULL,
            touched INTEGER NOT NULL DEFAULT 0,
            seq     INTEGER NOT NULL DEFAULT 0
        )""",
    ],
    [  # 2: the creature's state, its body's changes, and its character versions
        # state: one row; data is JSON (sim_time, location, asleep, mood, energy, hunger, ...)
        "CREATE TABLE state (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL, updated_at TEXT NOT NULL)",
        # every change to mood/energy/hunger with its cause (§2.2)
        """CREATE TABLE body_changes (
            id       INTEGER PRIMARY KEY,
            sim_time TEXT NOT NULL,
            var      TEXT NOT NULL,
            delta    REAL NOT NULL,
            value    REAL NOT NULL,
            cause    TEXT NOT NULL
        )""",
        "CREATE INDEX idx_body_changes_time ON body_changes(sim_time)",
        # one row per version of the character sheet (§2.1, §5.6); version 1 is the spawn sheet
        """CREATE TABLE character_versions (
            version    INTEGER PRIMARY KEY,
            sheet      TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reason     TEXT NOT NULL,
            change     TEXT,
            evidence   TEXT NOT NULL DEFAULT '[]'
        )""",
    ],
    [  # 3: what the creature knows it can do (§5.4)
        # discovered_by: innate | arrival | inspect | effect. provisional = 1 until sleep confirms it and writes
        # own_words. location: where it was learned ('' for universal actions).
        """CREATE TABLE knowledge (
            action_id     TEXT PRIMARY KEY,
            location      TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            discovered_by TEXT NOT NULL,
            provisional   INTEGER NOT NULL DEFAULT 1,
            own_words     TEXT,
            times_done    INTEGER NOT NULL DEFAULT 0,
            last_done_at  TEXT,
            last_outcome  TEXT
        )""",
    ],
]
SCHEMA_VERSION = len(MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration the database has not seen yet, each in one transaction. Returns their numbers."""
    done = []
    for n in range(schema_version(conn), SCHEMA_VERSION):
        with transaction(conn):
            for stmt in MIGRATIONS[n]:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {n + 1}")
        done.append(n + 1)
    return done

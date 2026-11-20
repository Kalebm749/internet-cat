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

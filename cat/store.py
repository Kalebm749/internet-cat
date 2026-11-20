"""The new life's database, `data/nit.db` (docs/rewrite-contracts.md §9).

A fresh schema, separate from the old `cat.db`. Tables are added here step by step as the rewrite
grows; each change is a numbered entry in MIGRATIONS (a list of statements) so a running life is always
carried forward. Connections and transactions reuse `cat.db.connect` / `cat.db.transaction`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from cat.db import backup, connect, transaction  # noqa: F401  (re-exported for callers)

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
    [  # 4: decisions, activities, experiences and capability use (§3, §4.7, §5.1)
        # one row per decision: what it saw (mind), what it was asked (messages), every attempt, what it chose
        """CREATE TABLE decisions (
            id        INTEGER PRIMARY KEY,
            sim_time  TEXT NOT NULL,
            real_time TEXT NOT NULL,
            kind      TEXT NOT NULL,
            triggers  TEXT NOT NULL,
            action    TEXT NOT NULL,
            inputs    TEXT NOT NULL,
            thought   TEXT NOT NULL,
            reasoning TEXT NOT NULL,
            preserve  INTEGER NOT NULL,
            letter    TEXT,
            fallback  INTEGER NOT NULL,
            notes     TEXT NOT NULL,
            attempts  TEXT NOT NULL,
            messages  TEXT NOT NULL,
            mind      TEXT NOT NULL,
            dropped   TEXT NOT NULL,
            ms        INTEGER NOT NULL
        )""",
        "CREATE INDEX idx_decisions_time ON decisions(sim_time)",
        # one row per activity; status: running | finished | interrupted | rejected
        """CREATE TABLE activities (
            id              INTEGER PRIMARY KEY,
            decision_id     INTEGER,
            action_id       TEXT NOT NULL,
            location        TEXT NOT NULL,
            inputs          TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            planned_minutes REAL NOT NULL,
            ended_at        TEXT,
            status          TEXT NOT NULL,
            text            TEXT,
            changes         TEXT NOT NULL DEFAULT '[]',
            capability      TEXT
        )""",
        "CREATE INDEX idx_activities_status ON activities(status)",
        # what happened, as code's facts: kind activity | event | discovery | note
        """CREATE TABLE experiences (
            id          INTEGER PRIMARY KEY,
            sim_time    TEXT NOT NULL,
            kind        TEXT NOT NULL,
            action_id   TEXT,
            activity_id INTEGER,
            location    TEXT NOT NULL,
            text        TEXT NOT NULL,
            data        TEXT NOT NULL DEFAULT '{}',
            thought     TEXT,
            importance  REAL NOT NULL DEFAULT 0,
            remember    INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX idx_experiences_time ON experiences(sim_time)",
        "CREATE TABLE capability_usage (capability TEXT NOT NULL, day TEXT NOT NULL, uses INTEGER NOT NULL, "
        "PRIMARY KEY (capability, day))",
    ],
    [  # 5: memory and the sleep cycle (§5)
        # kind long | core. sources: experience ids it was made from. embedding: float32 blob (a search index).
        """CREATE TABLE memories (
            id            INTEGER PRIMARY KEY,
            kind          TEXT NOT NULL,
            text          TEXT NOT NULL,
            importance    REAL NOT NULL,
            embedding     BLOB,
            created_at    TEXT NOT NULL,
            sources       TEXT NOT NULL DEFAULT '[]',
            recall_count  INTEGER NOT NULL DEFAULT 0,
            last_recalled TEXT
        )""",
        "CREATE INDEX idx_memories_kind ON memories(kind)",
        # subject_kind action | family | location; valence -1..1; confidence 0..1
        """CREATE TABLE preferences (
            subject_kind TEXT NOT NULL,
            subject_id   TEXT NOT NULL,
            valence      REAL NOT NULL,
            confidence   REAL NOT NULL,
            words        TEXT,
            evidence     TEXT NOT NULL DEFAULT '[]',
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (subject_kind, subject_id)
        )""",
        # every character-development proposal, accepted or not, with the verdict of each check
        """CREATE TABLE dev_proposals (
            id        INTEGER PRIMARY KEY,
            sim_time  TEXT NOT NULL,
            proposal  TEXT NOT NULL,
            verdicts  TEXT NOT NULL,
            accepted  INTEGER NOT NULL,
            version   INTEGER
        )""",
        # one row per night: what each stage saw and produced
        """CREATE TABLE sleep_runs (
            id          INTEGER PRIMARY KEY,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            status      TEXT NOT NULL,
            stages      TEXT NOT NULL DEFAULT '{}',
            summary     TEXT
        )""",
    ],
    [  # 6: behaviour history (§6): signatures and freshness on activities, thoughts, repeated reflections
        "ALTER TABLE activities ADD COLUMN family TEXT",
        "ALTER TABLE activities ADD COLUMN input_sig TEXT",           # normalised inputs: the target
        "ALTER TABLE activities ADD COLUMN revisit_sig TEXT",         # the revisit_key facts when it started
        "ALTER TABLE activities ADD COLUMN restart_of INTEGER",       # the finished instance it restarted
        "ALTER TABLE activities ADD COLUMN freshness REAL NOT NULL DEFAULT 1",
        "ALTER TABLE activities ADD COLUMN repeat_parts TEXT",        # what made it less fresh
        """CREATE TABLE thoughts (
            id          INTEGER PRIMARY KEY,
            decision_id INTEGER,
            sim_time    TEXT NOT NULL,
            text        TEXT NOT NULL,
            embedding   BLOB,
            repeated    INTEGER NOT NULL DEFAULT 0
        )""",
        "ALTER TABLE sleep_runs ADD COLUMN summary_embedding BLOB",
        "ALTER TABLE sleep_runs ADD COLUMN repeated INTEGER NOT NULL DEFAULT 0",
    ],
    [  # 7: visitor letters imported from mail.db (§4.8); mail.db itself is written only by the website
        "CREATE TABLE mail_imports (letter_id INTEGER PRIMARY KEY, sim_time TEXT NOT NULL, real_time TEXT NOT NULL)",
    ],
]
SCHEMA_VERSION = len(MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def open_readonly(path: Path | str) -> sqlite3.Connection | None:
    """For the website and the dashboard: the life, read-only, or None while it does not exist yet or the engine
    has not yet migrated it to this code's schema (readers never migrate)."""
    if not Path(path).exists():
        return None
    try:
        conn = connect(path, readonly=True)
        if schema_version(conn) != SCHEMA_VERSION:
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        return None


def migrate(conn: sqlite3.Connection, keep_backups: int = 14) -> list[int]:
    """Apply every migration the database has not seen yet, each in one transaction. Returns their numbers.
    A life that already exists is backed up first (data/backups/nit-<time>-pre-migration.db): an update must
    never risk it. If the backup fails, nothing is migrated."""
    current = schema_version(conn)
    if current < SCHEMA_VERSION and current > 0:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        if path:
            backup(conn, Path(path).parent / "backups", "pre-migration", keep_backups)
    done = []
    for n in range(current, SCHEMA_VERSION):
        with transaction(conn):
            for stmt in MIGRATIONS[n]:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {n + 1}")
        done.append(n + 1)
    return done

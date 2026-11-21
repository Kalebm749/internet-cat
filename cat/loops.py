"""Open loops (docs/rewrite-contracts.md §5.8): a few wants and questions on its mind that are not settled yet.

Sleep opens them from the creature's own experiences and closes them when those experiences settled them (every
citation checked against what really happened, `cat/sleepcycle.py`); an unsettled one fades after `expire_days` and
a full mind pushes the oldest out. They only appear in the Mind as things on its mind: code never picks a move
from them. Letters (external words) never open one.
"""
from __future__ import annotations

import sqlite3

from cat import clock

KINDS = ("want", "question", "promise", "worry", "idea", "problem")
LABELS = {"want": "something you want", "question": "something you wonder",
          "promise": "something you said you would do", "worry": "something that worries you",
          "idea": "an idea you want to try", "problem": "something you have not worked out"}


def open_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM loops WHERE status = 'open' ORDER BY id").fetchall()


def on_mind(conn: sqlite3.Connection, tuning: dict) -> list[str]:
    """The open loops in its own words, newest first, for the Mind."""
    k = tuning["loops"]["on_mind"]
    if k == 0:
        return []
    return [f"({LABELS[r['kind']]}) {r['text']}" for r in reversed(open_rows(conn))][:k]


def salience_now(r: sqlite3.Row, tuning: dict, now: str) -> float:
    """How loud a concern is now: it fades the longer it goes untouched, and comes back when something touches it."""
    age = (clock.parse(now) - clock.parse(r["last_touched"] or r["opened_at"])).total_seconds() / 86400
    return max(0.1, r["salience"] - tuning["loops"]["salience_decay_per_day"] * age)


def resurface(conn: sqlite3.Connection, tuning: dict, now: str, rng) -> sqlite3.Row | None:
    """Now and then an unsettled concern comes back to mind by itself (the louder, the likelier). Writes inside the
    caller's transaction; returns the concern or None."""
    lp = tuning["loops"]
    last = conn.execute("SELECT value FROM meta WHERE key = 'loops.resurfaced'").fetchone()
    if last and (clock.parse(now) - clock.parse(last[0])).total_seconds() / 3600 < lp["resurface_hours"]:
        return None
    conn.execute("INSERT INTO meta(key, value) VALUES ('loops.resurfaced', ?) ON CONFLICT(key) DO UPDATE SET "
                 "value = excluded.value", (now,))
    rows = open_rows(conn)
    if not rows or rng.random() >= lp["resurface_chance"]:
        return None
    weights = [salience_now(r, tuning, now) for r in rows]
    pick = rng.choices(rows, weights=weights)[0]
    conn.execute("UPDATE loops SET last_touched = ?, salience = MIN(1.0, ? + 0.1) WHERE id = ?",
                 (now, salience_now(pick, tuning, now), pick["id"]))
    return pick


def expired(rows: list[sqlite3.Row], tuning: dict, now: str) -> list[int]:
    days = tuning["loops"]["expire_days"]
    cutoff = clock.parse(now).timestamp() - days * 86400
    return [r["id"] for r in rows if clock.parse(r["opened_at"]).timestamp() < cutoff]

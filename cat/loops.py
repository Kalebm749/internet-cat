"""Open loops (docs/rewrite-contracts.md §5.8): a few wants and questions on its mind that are not settled yet.

Sleep opens them from the creature's own experiences and closes them when those experiences settled them (every
citation checked against what really happened, `cat/sleepcycle.py`); an unsettled one fades after `expire_days` and
a full mind pushes the oldest out. They only appear in the Mind as things on its mind: code never picks a move
from them. Letters (external words) never open one.
"""
from __future__ import annotations

import sqlite3

from cat import clock

KINDS = ("want", "question")


def open_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM loops WHERE status = 'open' ORDER BY id").fetchall()


def on_mind(conn: sqlite3.Connection, tuning: dict) -> list[str]:
    """The open loops in its own words, newest first, for the Mind."""
    k = tuning["loops"]["on_mind"]
    if k == 0:
        return []
    return [f"({'something you want' if r['kind'] == 'want' else 'something you wonder'}) {r['text']}"
            for r in reversed(open_rows(conn))][:k]


def expired(rows: list[sqlite3.Row], tuning: dict, now: str) -> list[int]:
    days = tuning["loops"]["expire_days"]
    cutoff = clock.parse(now).timestamp() - days * 86400
    return [r["id"] for r in rows if clock.parse(r["opened_at"]).timestamp() < cutoff]

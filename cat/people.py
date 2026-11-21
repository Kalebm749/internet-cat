"""People it knows (docs/individuality-plan.md §5): who has written to it, how familiar they are, how it feels about them.

Familiarity grows with every letter and reply; affinity moves with how each exchange left it (the mood effect of reading
or answering), so two creatures can come to feel differently about the same person. Words about them are the creature's
own letters' sender names only; what they wrote stays visitor data.
"""
from __future__ import annotations

import sqlite3


def touch(conn: sqlite3.Connection, name: str, now: str, familiarity: float = 0.1, mood: float = 0.0,
          interactions: int = 1) -> None:
    name = (name or "").strip()[:60]
    if not name:
        return
    conn.execute("INSERT INTO people(name, familiarity, affinity, interactions, first_seen, last_seen) "
                 "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                 "familiarity = MIN(1.0, familiarity + ?), affinity = MAX(-1.0, MIN(1.0, affinity + ?)), "
                 "interactions = interactions + ?, last_seen = ?",
                 (name, min(1.0, familiarity), max(-1.0, min(1.0, mood * 4)), interactions, now, now,
                  familiarity, mood * 4, interactions, now))


def on_mind(conn: sqlite3.Connection, prompts: dict, k: int = 3) -> list[str]:
    out = []
    for r in conn.execute("SELECT * FROM people ORDER BY familiarity DESC, last_seen DESC LIMIT ?", (k,)):
        feel = prompts["person_warm"] if r["affinity"] > 0.15 else prompts["person_uneasy"] if r["affinity"] < -0.15 \
            else prompts["person_unsure"]
        out.append(prompts["person_line"].format(name=r["name"], n=r["interactions"], feel=feel))
    return out

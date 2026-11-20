"""Wishes are granted only by the world's author (docs/rewrite-contracts.md §4.9).

The creature wishes at the fountain for a place it does not have or something it cannot yet do (at most a few
times a real day: content/locations/fountain.yaml). Nothing in the world grants a wish. The author builds what was
wished for (new content, or a capability in code) and lists the wish in content/world.yaml `granted_wishes`; at the
next start the engine marks it granted, the creature perceives that it came true, and it becomes a remembered moment.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from cat.life import Life

log = logging.getLogger("cat.wishes")


def grant(conn: sqlite3.Connection, life: Life, texts: dict[str, str]) -> list[str]:
    """Apply newly granted wishes (inside the caller's transaction). Returns what the creature perceives."""
    key = life.content.world.get("wishes")
    grants = life.content.world.get("granted_wishes") or []
    if not key or not grants:
        return []
    wishes = life.world.get(key)
    by_id = {str(w.get("id")): w for w in wishes if isinstance(w, dict)}
    told = []
    for g in grants:
        w = by_id.get(str(g["wish"]))
        if w is None:
            log.warning("granted_wishes: there is no wish %s (yet)", g["wish"])
            continue
        if w.get("status") == "granted":
            continue
        note = (g.get("note") or "").strip()
        life.world.update_item_raw(key, str(g["wish"]), {"status": "granted", "granted_at": life.sim_time,
                                                         "note": note})
        text = texts["wish_granted"].format(wish=w.get("text", ""), note=note).strip()
        told.append(text)
        conn.execute("INSERT INTO experiences(sim_time, kind, location, text, data, importance, remember) "
                     "VALUES (?, 'granted', ?, ?, ?, 0.9, 1)",
                     (life.sim_time, life.location, text, json.dumps({"wish": str(g["wish"]), "note": note})))
        log.info("wish %s granted: %s", g["wish"], w.get("text"))
    return told

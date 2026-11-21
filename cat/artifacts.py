"""Things the creature made, kept whole (docs/rewrite-contracts.md §4.10).

A capability returns an artifact in its result; the engine stores it here inside the activity's transaction (the
engine is the only writer). The activity and the world keep only a small reference (id, title, status, ...): the
image and the drawing operations stay in this table, one row per version, never overwritten.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

PUBLIC = ("id", "type", "title", "created_at", "subject", "description", "thoughts", "status", "revision_of",
          "root_id", "materials", "format")


def reference(artifact: dict) -> dict:
    """What the activity and the world keep: small metadata, no image and no operations."""
    return {k: artifact[k] for k in PUBLIC if k in artifact}


def save(conn: sqlite3.Connection, artifact: dict, activity_id: int | None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO artifacts(id, kind, created_at, activity_id, root_id, revision_of, title, description, "
        "intention, thoughts, materials, meta, mime, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (artifact["id"], artifact.get("type", "drawing"), artifact["created_at"], activity_id,
         artifact.get("root_id") or artifact["id"], artifact.get("revision_of"), artifact.get("title", ""),
         artifact.get("description", ""), artifact.get("subject", ""), artifact.get("thoughts", ""),
         json.dumps(artifact.get("materials", []), ensure_ascii=False),
         json.dumps(artifact.get("meta", {}), ensure_ascii=False), artifact.get("mime", "image/png"),
         artifact["data"]))


def load(conn: sqlite3.Connection, artifact_id: str, with_data: bool = True) -> dict[str, Any] | None:
    cols = "*" if with_data else ("id, kind, created_at, activity_id, root_id, revision_of, title, description, "
                                  "intention, thoughts, materials, meta, mime")
    r = conn.execute(f"SELECT {cols} FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
    if r is None:
        return None
    out = dict(r)
    out["materials"] = json.loads(out["materials"] or "[]")
    out["meta"] = json.loads(out["meta"] or "{}")
    return out


def versions(conn: sqlite3.Connection, root_id: str) -> list[dict]:
    """Every stored version of one drawing, oldest first (without images)."""
    rows = conn.execute("SELECT id, created_at, revision_of, title, description, intention, thoughts, materials "
                        "FROM artifacts WHERE root_id = ? ORDER BY created_at, rowid", (root_id,)).fetchall()
    return [{**dict(r), "materials": json.loads(r["materials"] or "[]")} for r in rows]

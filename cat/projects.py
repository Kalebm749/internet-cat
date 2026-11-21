"""Things it is making (docs/individuality-plan.md §5): a project keeps the intention, every attempt, its own subjective
evaluation of how it turned out, and how it ended (finished, abandoned, or still going).

Actions say in content which event they are (`project: made | reworked | studied | finished | abandoned`); this module
keeps the record. The evaluation is the creature's own and subjective (a self-critical creature rates the same drawing
lower); a project can be disliked, shelved or abandoned. What it is making, and how it felt about it, comes back into
the Mind, so a failed piece changes what it wants next.
"""
from __future__ import annotations

import json
import sqlite3

EVENTS = ("made", "reworked", "studied", "finished", "abandoned")


def _root(conn: sqlite3.Connection, artifact_id: str | None) -> str | None:
    if not artifact_id:
        return None
    r = conn.execute("SELECT root_id FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
    return r[0] if r else None


def record(conn: sqlite3.Connection, event: str, inputs: dict, cap: dict | None, now: str) -> None:
    """Keep the project record for an action that made, reworked, studied, finished or abandoned a drawing."""
    art = (cap or {}).get("artifact") or {}
    fields = (cap or {}).get("fields") or {}
    root = art.get("root_id") or art.get("id") or _root(conn, inputs.get("artwork"))
    if not root:
        return
    row = conn.execute("SELECT * FROM projects WHERE root_id = ?", (root,)).fetchone()
    entry = {"at": now, "event": event, "artifact": art.get("id") or inputs.get("artwork")}
    if event == "made" and row is None:
        conn.execute("INSERT INTO projects(kind, title, intention, root_id, history, created_at, updated_at) "
                     "VALUES ('drawing', ?, ?, ?, ?, ?, ?)",
                     (art.get("title") or "untitled", str(inputs.get("intention", ""))[:500], root,
                      json.dumps([{**entry, "note": str(inputs.get("thoughts", ""))[:200]}]), now, now))
        return
    if row is None:
        return
    history = json.loads(row["history"])
    status, title, rating, evaluation = row["status"], row["title"], row["rating"], row["evaluation"]
    if event in ("made", "reworked"):
        title = art.get("title") or title
        if status in ("shelved", "abandoned"):
            status = "active"
        entry["note"] = str(inputs.get("intention", ""))[:200]
    elif event == "studied" and fields.get("verdict"):
        rating, evaluation = float(fields.get("rating") or 0.5), str(fields.get("feeling") or "")
        entry.update(verdict=fields["verdict"], rating=rating, note=evaluation[:200])
    elif event == "finished":
        status = "finished"
    elif event == "abandoned":
        status = "abandoned"
    history.append(entry)
    conn.execute("UPDATE projects SET title = ?, status = ?, rating = ?, evaluation = ?, history = ?, updated_at = ? "
                 "WHERE id = ?", (title, status, rating, evaluation, json.dumps(history[-30:]), now, row["id"]))


def on_mind(conn: sqlite3.Connection, prompts: dict, k: int = 3) -> list[str]:
    """What it is making and how it feels about it: the projects still going, the latest first."""
    out = []
    for r in conn.execute("SELECT * FROM projects WHERE status = 'active' ORDER BY updated_at DESC, id DESC LIMIT ?", (k,)):
        rating = r["rating"]
        how = prompts["project_new"] if rating is None else prompts["project_pleased"] if rating >= 0.65 \
            else prompts["project_disappointed"] if rating <= 0.35 else prompts["project_unsure"]
        out.append(prompts["project_line"].format(title=r["title"], how=how))
    return out

"""Long-term projects: something the entity keeps coming back to across ticks and days.

A project is formed during sleep (the sleeping mind notices a liked activity it keeps returning to),
advanced tick by tick by the narrator's judgement (small, tapering steps), and leaves artifacts (short
saved pieces of what it made). Project work is exempt from the repetition penalty: progress replaces it.
Code owns every number here; the LLMs only describe. Nothing in this module picks or ranks actions."""
from __future__ import annotations

import logging
import math
from sqlite3 import Connection
from typing import Any

from cat import character
from cat.config import Config

log = logging.getLogger("cat.projects")

STATUSES = ("active", "paused", "done", "dropped")
OPEN = ("active", "paused")

OPS_SCHEMA = {
    "type": "object",
    "properties": {"projects": {"type": "array", "maxItems": 1, "items": {
        "type": "object",
        "properties": {"op": {"type": "string", "enum": ["start", "pause", "resume", "drop"]},
                       "id": {"type": "integer"}, "title": {"type": "string"},
                       "activity": {"type": "string"}, "why": {"type": "string"}},
        "required": ["op"]}}},
    "required": ["projects"],
}


# ---------- reading ----------
def _rows(conn: Connection, where: str, args: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM projects WHERE {where} ORDER BY id", args)]
    except Exception:                       # an old database without the table
        return []


def active(conn: Connection) -> list[dict]:
    return _rows(conn, "status='active'")


def open_projects(conn: Connection) -> list[dict]:
    return _rows(conn, "status IN ('active','paused')")


def get(conn: Connection, pid: int) -> dict | None:
    r = _rows(conn, "id=?", (pid,))
    return r[0] if r else None


def stage_word(progress: float, cfg: Config) -> str:
    """Progress becomes a word ("just begun", "underway", ...); the entity never sees the number."""
    return character._word_from(cfg.settings["projects"]["stages"], progress)


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit].strip()


# ---------- sleep: forming, pausing, resuming, dropping (at most one op a night) ----------
def decide_ops(cfg: Config, existing: list[dict], proposed: Any, sim_day: str) -> dict:
    """The sleeping mind's proposal -> an outcome the commit applies. `existing` is every open
    (active or paused) project. Always returns a dict with an `action`:
    none | rejected | start | pause | resume | drop."""
    pc = cfg.settings["projects"]
    items = proposed if isinstance(proposed, list) else []
    p = next((x for x in items if isinstance(x, dict)), None)
    if p is None:
        return {"action": "none", "reason": "no proposal"}
    op = p.get("op")
    n_active = sum(1 for e in existing if e["status"] == "active")
    if op == "start":
        title = _clean(p.get("title"), pc["title_max_length"])
        if not title:
            return {"action": "rejected", "op": op, "reason": "empty title"}
        if any(e["title"].lower() == title.lower() for e in existing):
            return {"action": "rejected", "op": op, "title": title, "reason": "already a project with that title"}
        if n_active >= pc["max_active"]:
            return {"action": "rejected", "op": op, "title": title,
                    "reason": f"already {n_active} active (max_active {pc['max_active']}): pause or drop one first"}
        return {"action": "start", "title": title,
                "activity": _clean(p.get("activity"), pc["title_max_length"]) or title,
                "why": _clean(p.get("why"), pc["why_max_length"]), "formed_day": sim_day}
    pid = p.get("id")
    target = next((e for e in existing if not isinstance(pid, bool) and e["id"] == pid), None)
    if op in ("pause", "resume", "drop"):
        if target is None:
            return {"action": "rejected", "op": op, "reason": f"no open project with id {pid!r}"}
        if op == "pause" and target["status"] != "active":
            return {"action": "rejected", "op": op, "id": pid, "reason": "not active"}
        if op == "resume":
            if target["status"] != "paused":
                return {"action": "rejected", "op": op, "id": pid, "reason": "not paused"}
            if n_active >= pc["max_active"]:
                return {"action": "rejected", "op": op, "id": pid, "reason": "max_active reached"}
        return {"action": op, "id": pid, "title": target["title"]}
    return {"action": "rejected", "op": op, "reason": f"unknown op {op!r}"}


def idle_upkeep(cfg: Config, existing: list[dict]) -> list[dict]:
    """Each night: an active project that was worked today resets its idle counter; one that was not
    counts a night idle, and after `pause_after_idle_nights` it is paused (it can be resumed)."""
    limit = cfg.settings["projects"]["pause_after_idle_nights"]
    out = []
    for e in existing:
        if e["status"] != "active":
            continue
        idle = 0 if e["worked_today"] else e["idle_nights"] + 1
        out.append({"id": e["id"], "title": e["title"], "worked": bool(e["worked_today"]),
                    "idle_nights": idle, "pause": limit > 0 and idle >= limit})
    return out


def commit_sleep(conn: Connection, upkeep: list[dict], op: dict, sim_time: str) -> None:
    """Apply tonight's upkeep and the (single) op. Call inside the sleep transaction."""
    for u in upkeep:
        conn.execute("UPDATE projects SET worked_today=0, idle_nights=?, status=?, updated_at=? WHERE id=?",
                     (u["idle_nights"], "paused" if u["pause"] else "active", sim_time, u["id"]))
    a = op["action"]
    if a == "start":
        conn.execute("INSERT INTO projects(title,activity,why,status,progress,note,formed_day,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (op["title"], op["activity"], op["why"], "active", 0.0, "", op["formed_day"], sim_time))
    elif a in ("pause", "resume", "drop"):
        status = {"pause": "paused", "resume": "active", "drop": "dropped"}[a]
        conn.execute("UPDATE projects SET status=?, idle_nights=0, updated_at=? WHERE id=?",
                     (status, sim_time, op["id"]))


# ---------- the narrator's view of progress ----------
def plan_advance(cfg: Config, projects: list[dict], raw: Any) -> dict | None:
    """Validate the narrator's optional `project` output against the ACTIVE projects and work out what
    changes. Returns None if the narrator reported nothing; otherwise a dict with `ok`. Pure: it writes
    nothing. A rejected report (unknown or inactive project) still comes back, with `reason`."""
    pc = cfg.settings["projects"]
    if not isinstance(raw, dict) or not raw:
        return None
    pid = raw.get("id")
    proj = next((p for p in projects if p["status"] == "active" and not isinstance(pid, bool)
                 and p["id"] == pid), None)
    if proj is None:
        return {"ok": False, "reason": f"no active project with id {pid!r}"}
    bucket = str(raw.get("advance", "0"))
    points = float(pc["progress_values"].get(bucket, 0))
    before = float(proj["progress"])
    gained = points * max(0.0, 1.0 - pc["taper"] * before / 100.0)          # slower near the end
    after = min(100.0, before + gained)
    every = pc["milestone_every"]
    crossed = [m for m in range(every, 101, every) if before < m <= after]
    note = _clean(raw.get("note"), pc["note_max_length"]) or proj["note"]
    art = raw.get("artifact")
    artifact = None
    if isinstance(art, dict) and gained > 0:                                 # making something is progress
        text = " ".join(str(art.get("text") or "").split())[:pc["artifact_max_length"]].strip()
        if text:
            artifact = {"kind": _clean(art.get("kind"), 30) or "piece",
                        "title": _clean(art.get("title"), pc["title_max_length"]) or proj["title"],
                        "text": text}
    return {"ok": True, "id": proj["id"], "title": proj["title"], "bucket": bucket, "advanced": gained > 0,
            "progress_before": round(before, 2), "progress_after": round(after, 2), "gained": round(gained, 2),
            "milestones": crossed, "completed": after >= 100.0 and before < 100.0, "note": note,
            "artifact": artifact}


def commit_advance(conn: Connection, adv: dict, tick_id: int, sim_time: str) -> int | None:
    """Write the progress, note, last-worked time and any artifact. Returns the artifact id."""
    if not adv or not adv.get("ok"):
        return None
    sets, args = ["progress=?", "note=?", "updated_at=?"], [adv["progress_after"], adv["note"], sim_time]
    if adv["advanced"]:
        sets += ["last_worked=?", "ticks_worked=ticks_worked+1", "worked_today=1", "idle_nights=0"]
        args.append(sim_time)
    if adv["completed"]:
        sets.append("status='done'")
    conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", (*args, adv["id"]))
    aid = None
    if adv["artifact"]:
        a = adv["artifact"]
        aid = conn.execute("INSERT INTO artifacts(project_id,tick_id,kind,title,text,created_sim_time) "
                           "VALUES(?,?,?,?,?,?)",
                           (adv["id"], tick_id, a["kind"], a["title"], a["text"], sim_time)).lastrowid
    return aid


# ---------- what the LLMs are told ----------
def ago_phrase(hours: float) -> str:
    if hours < 1:
        return "a short while ago"
    if hours < 24:
        return f"about {int(hours)} hour{'s' if int(hours) != 1 else ''} ago"
    if hours < 48:
        return "about a day ago"
    return f"about {int(hours // 24)} days ago"


def brain_view(cfg: Config, conn: Connection, now: str) -> list[dict]:
    """The active projects as the brain sees them: words and the running note, never the progress number."""
    from cat import clock
    pc = cfg.settings["projects"]
    out = []
    for p in active(conn):
        idle = ""
        if p["last_worked"]:
            hours = (clock.parse(now) - clock.parse(p["last_worked"])).total_seconds() / 3600
            if hours >= pc["idle_hours_note"]:
                idle = ago_phrase(hours)
        out.append({"title": p["title"], "why": p["why"], "stage": stage_word(p["progress"], cfg),
                    "note": p["note"], "idle": idle, "untouched": not p["last_worked"]})
    return out


def narrator_view(cfg: Config, conn: Connection) -> list[dict]:
    return [{"id": p["id"], "title": p["title"], "why": p["why"], "stage": stage_word(p["progress"], cfg),
             "note": p["note"]} for p in active(conn)]

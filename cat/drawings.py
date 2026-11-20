"""Drawings: real things it makes at a `paper` object, one sitting at a time, that stay in the world.

What it can draw is only what it has known (settings `drawing`): places it has been, things it has handled or
looked at, happenings it witnessed. A drawing is a world object (tag `drawing`, state `status` and `sittings`)
plus a row in the `drawings` table holding its history: the subject, and every sitting with the minutes spent
and the state it was in (mood, anxiety, energy). Code draws the picture from that history (`cat/art.py`), so a
drawing started calm and finished anxious shows both, and nothing in it is invented.

Moves (moves.yaml `does`): draw_new (start one), draw_more (another sitting), finish_drawing (it calls it done
and may give it a title through its journal), pin (onto a `board`), discard (throw it away). A sitting is an
activity: if hunger or an event interrupts it, the sitting is recorded as shorter. Every world change goes
through cat/effects.py; this module only plans the changes and writes the history inside the tick transaction.

If a drawing project is active (projects.medium = 'drawing', formed in sleep), each sitting is that project's
work and each finished drawing is one of its pieces (an artifact with a drawing_id): no LLM writes the piece."""
from __future__ import annotations

import json
import random
import re
from sqlite3 import Connection
from typing import Any

from cat.config import Config
from cat.db import dumps

STATUSES = ("in progress", "finished", "pinned", "discarded")


def enabled(cfg: Config) -> bool:
    return bool((cfg.settings.get("drawing") or {}).get("enabled"))


# ---------- what it can draw: only what it has known ----------
def _moment(text: str) -> str:
    """'The clock strikes, slow and deep, and ...' -> 'the moment when the clock strikes'."""
    first = re.split(r"[.,;:!?]", text.strip(), maxsplit=1)[0].strip()
    words, out = first.split(), []
    for w in words:
        if len(" ".join(out + [w])) > 50:
            break
        out.append(w)
    clause = " ".join(out) or "something happened"
    return "the moment when " + clause[0].lower() + clause[1:]


def subjects(conn: Connection, cfg: Config, world) -> list[dict]:
    """Every subject it could draw, from its own history: places it has been (ticks), things it has handled or
    looked at (objects its moves targeted that still exist; not its own drawings), happenings it witnessed
    (recent events). Newest first within each kind."""
    out: list[dict] = []
    areas = cfg.world["areas"]
    for (aid,) in conn.execute("SELECT area FROM ticks WHERE kind='tick' GROUP BY area ORDER BY MAX(id) DESC"):
        if aid in areas:
            out.append({"key": f"place:{aid}", "kind": "place", "name": areas[aid]["name"], "tags": []})
    seen: set[str] = set()
    for (key,) in conn.execute("SELECT move_key FROM ticks WHERE kind='tick' AND move_key LIKE '%:%' ORDER BY id DESC"):
        oid = key.split(":", 1)[1]
        o = world.objects.get(oid)
        if oid in seen or o is None or "drawing" in o["tags"]:
            continue
        seen.add(oid)
        out.append({"key": f"thing:{oid}", "kind": "thing", "name": o["name"], "tags": list(o["tags"])})
    texts: set[str] = set()
    n = int(cfg.settings["drawing"]["happenings_considered"])
    for eid, text in conn.execute("SELECT id, text FROM events ORDER BY id DESC LIMIT ?", (n,)):
        if text in texts:
            continue
        texts.add(text)
        out.append({"key": f"happening:{eid}", "kind": "happening", "name": _moment(text), "tags": []})
    return out


def offered_subjects(cfg: Config, conn: Connection, world, seed: str) -> list[tuple[str, str]]:
    """A few of them for this decision (seeded: same time and place, same pick), as menu targets."""
    allsub = subjects(conn, cfg, world)
    k = min(len(allsub), int(cfg.settings["drawing"]["subjects_offered"]))
    picked = random.Random(f"subjects|{seed}").sample(allsub, k) if k else []
    return [(s["key"], s["name"]) for s in picked]


def subject(conn: Connection, cfg: Config, world, key: str) -> dict | None:
    return next((s for s in subjects(conn, cfg, world) if s["key"] == key), None)


def blocked(cfg: Config, world) -> set[str]:
    """Moves that cannot be offered right now: no room for another unfinished drawing, or the board is full."""
    out: set[str] = set()
    if not enabled(cfg):
        return {"draw_new", "draw_more", "finish_drawing", "pin", "discard"}
    d = cfg.settings["drawing"]
    drawings = [o for o in world.objects.values() if "drawing" in o["tags"]]
    if sum(1 for o in drawings if (o.get("state") or {}).get("status") == "in progress") >= d["max_in_progress"]:
        out.add("draw_new")
    if sum(1 for o in drawings if (o.get("state") or {}).get("status") == "pinned") >= d["board_capacity"]:
        out.add("pin")
    return out


# ---------- one move ----------
def row_for(conn: Connection, object_id: str) -> dict | None:
    try:
        r = conn.execute("SELECT * FROM drawings WHERE object_id=? ORDER BY id DESC LIMIT 1", (object_id,)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    d = dict(r)
    d["sittings"] = json.loads(d["sittings"] or "[]")
    d["subject_tags"] = json.loads(d["subject_tags"] or "[]")
    return d


def plan(cfg: Config, conn: Connection, world, mv, does: str, project: dict | None) -> dict:
    """The world changes a drawing move makes and what to record. Pure: writes nothing."""
    s = cfg.settings
    if does == "draw_new":
        sub = subject(conn, cfg, world, str(mv.target_id)) or {
            "key": str(mv.target_id), "kind": str(mv.target_id).split(":")[0], "name": mv.target_name, "tags": []}
        name = f"a drawing of {sub['name']}"[:s["name_max_length"]]
        return {"does": does, "subject": sub, "name": name, "project_id": project["id"] if project else None,
                "changes": [{"op": "create", "name": name, "tags": ["drawing"],
                             "state": {"status": "in progress", "sittings": "1"}}]}
    o = world.objects.get(str(mv.target_id))
    n = int((o or {}).get("state", {}).get("sittings", "0") or 0)
    if does == "draw_more":
        ch = [{"op": "set_state", "object_id": mv.target_id, "key": "sittings", "value": str(n + 1)}]
    elif does == "finish_drawing":
        ch = [{"op": "set_state", "object_id": mv.target_id, "key": "status", "value": "finished"}]
    elif does == "pin":
        ch = [{"op": "set_state", "object_id": mv.target_id, "key": "status", "value": "pinned"}]
    else:
        ch = [{"op": "discard", "object_id": mv.target_id}]
    return {"does": does, "object_id": mv.target_id, "name": (o or {}).get("name", mv.target_name),
            "project_id": project["id"] if project else None, "changes": ch}


def commit(conn: Connection, p: dict, applied: list[dict], tick_id: int, sim_time: str, sitting: dict,
           title: str | None = None) -> int | None:
    """Write the drawing's history for a move whose world change was applied (inside the tick transaction).
    `sitting` is {minutes, mood, anxiety, energy} for a drawing sitting. Returns the drawing's id."""
    does = p["does"]
    if does == "draw_new":
        made = next((c for c in applied if c.get("op") == "create"), None)
        if not made:
            return None
        sub = p["subject"]
        cur = conn.execute(
            "INSERT INTO drawings(object_id,subject_kind,subject_key,subject_name,subject_tags,title,status,"
            "project_id,sittings,started_sim_time,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (made["object_id"], sub["kind"], sub["key"], sub["name"], dumps(sub.get("tags") or []), None,
             "in progress", p["project_id"], dumps([{**sitting, "tick_id": tick_id, "sim_time": sim_time}]),
             sim_time, sim_time))
        return cur.lastrowid
    if not any(c.get("object_id") == p["object_id"] for c in applied):
        return None                                           # the change was rejected: nothing happened
    d = row_for(conn, p["object_id"])
    if d is None:
        return None
    if does == "draw_more":
        d["sittings"].append({**sitting, "tick_id": tick_id, "sim_time": sim_time})
        conn.execute("UPDATE drawings SET sittings=?, updated_at=?, project_id=COALESCE(project_id, ?) WHERE id=?",
                     (dumps(d["sittings"]), sim_time, p["project_id"], d["id"]))
    elif does == "finish_drawing":
        conn.execute("UPDATE drawings SET status='finished', finished_sim_time=?, title=COALESCE(?, title), "
                     "updated_at=? WHERE id=?", (sim_time, title, sim_time, d["id"]))
        pid = d["project_id"]
        if pid:                                               # one of a drawing project's pieces: the real drawing
            conn.execute("INSERT INTO artifacts(project_id,tick_id,kind,title,text,created_sim_time,drawing_id) "
                         "VALUES(?,?,?,?,?,?,?)", (pid, tick_id, "drawing", title or p["name"], "", sim_time, d["id"]))
    elif does == "pin":
        conn.execute("UPDATE drawings SET status='pinned', pinned_sim_time=?, updated_at=? WHERE id=?",
                     (sim_time, sim_time, d["id"]))
    elif does == "discard":
        conn.execute("UPDATE drawings SET status='discarded', discarded_sim_time=?, updated_at=? WHERE id=?",
                     (sim_time, sim_time, d["id"]))
    return d["id"]


def cut_sitting(conn: Connection, object_id: str, minutes: float) -> None:
    """An interrupted sitting lasted only `minutes`: record that (so the picture has fewer marks from it)."""
    d = row_for(conn, object_id)
    if not d or not d["sittings"]:
        return
    d["sittings"][-1]["minutes"] = round(max(0.0, float(minutes)), 1)
    d["sittings"][-1]["interrupted"] = True
    conn.execute("UPDATE drawings SET sittings=? WHERE id=?", (dumps(d["sittings"]), d["id"]))


def all_rows(conn: Connection, statuses: tuple[str, ...] = STATUSES) -> list[dict]:
    try:
        rows = conn.execute(f"SELECT * FROM drawings WHERE status IN ({','.join('?' * len(statuses))}) "
                            "ORDER BY id DESC", statuses).fetchall()
    except Exception:                                         # an older database without the table
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["sittings"] = json.loads(d["sittings"] or "[]")
        d["subject_tags"] = json.loads(d["subject_tags"] or "[]")
        out.append(d)
    return out


def active_project(projects: list[dict]) -> dict | None:
    return next((p for p in projects if p.get("medium") == "drawing" and p["status"] == "active"), None)


def describe(d: dict[str, Any]) -> str:
    return d.get("title") or d.get("subject_name") or "a drawing"


# ---------- the drawing tool: it places its own marks, code checks every one ----------
SHAPES = ("line", "curve", "circle", "scribble", "spiral", "wave", "dots", "blot", "wash", "rect")
WEIGHTS = ("light", "medium", "heavy")
SKETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "marks": {"type": "array", "items": {"type": "object", "properties": {
            "shape": {"type": "string", "enum": list(SHAPES)}, "x": {"type": "number"}, "y": {"type": "number"},
            "size": {"type": "number"}, "angle": {"type": "number"},
            "weight": {"type": "string", "enum": list(WEIGHTS)}, "colour": {"type": "string"},
            "about": {"type": "string"}}, "required": ["shape", "x", "y", "size", "colour"]}},
        "going_for": {"type": "string"}},
    "required": ["marks"],
}


def colours_here(cfg: Config, here: list[dict]) -> dict[str, str]:
    """What it can draw with right now: charcoal with the paper, colours only while something tagged `paints`
    is here. {name: hex}."""
    c = cfg.settings["drawing"]["colours"]
    out = dict(c["charcoal"])
    if any("paints" in o["tags"] for o in here):
        out.update(c["paints"])
    return out


def budget(cfg: Config, minutes: float) -> int:
    d = cfg.settings["drawing"]
    return max(int(d["marks_min"]), min(int(d["marks_max"]), round(d["marks_max"] * float(minutes) / 60)))


def subject_facts(cfg: Config, conn: Connection, kind: str, key: str, tags: list[str]) -> str:
    """What code holds as true of the subject: a thing's tag facts, a place's description, a happening's words."""
    if kind == "place":
        a = cfg.world["areas"].get(key.split(":", 1)[1])
        return a["description"] if a else ""
    if kind == "happening":
        try:
            r = conn.execute("SELECT text FROM events WHERE id=?", (int(key.split(":", 1)[1]),)).fetchone()
        except (ValueError, IndexError):
            r = None
        return r[0] if r else ""
    facts = cfg.world.get("tag_facts") or {}
    bits = [facts[t] for t in tags if facts.get(t)]
    return ("It " + ", ".join(bits) + ".") if bits else ""


def _summary(marks: list[dict]) -> tuple[str, str]:
    from collections import Counter
    shapes = ", ".join(s for s, _ in Counter(m["shape"] for m in marks).most_common(3)) or "nothing"
    colours = ", ".join(c for c, _ in Counter(m["colour"] for m in marks).most_common(3)) or "nothing"
    return shapes, colours


def sketch_messages(cfg: Config, conn: Connection, *, sub: dict, minutes: float, colours: dict[str, str],
                    sittings: list[dict], exclude_id: int | None, mood: dict, energy: float) -> list[dict]:
    from cat import brain
    from cat import journal as JR
    from cat import prompts as P
    K = lambda key, **kw: P.render(cfg, "sketch." + key, **kw)
    L = [K("subject", subject=sub["name"])]
    facts = subject_facts(cfg, conn, sub["kind"], sub["key"], sub.get("tags") or [])
    if facts:
        L.append(K("facts", facts=facts))
    L += [K("budget", n=budget(cfg, minutes)), K("colours", colours=", ".join(colours))]
    L.append("\n" + K("heading_sheet"))
    if not any(s.get("marks") for s in sittings):
        L.append(K("blank"))
    for i, s in enumerate(sittings, 1):
        if s.get("marks"):
            shp, col = _summary(s["marks"])
            L.append(K("sitting", n=i, marks=len(s["marks"]), shapes=shp, colours=col, going_for=s.get("going_for") or "-"))
    past = [d for d in all_rows(conn) if d["id"] != exclude_id][:int(cfg.settings["drawing"]["history_shown"])]
    if past:
        L.append("\n" + K("heading_past"))
        for d in past:
            marks = [m for s in d["sittings"] for m in s.get("marks") or []]
            shp, col = _summary(marks)
            status = {"pinned": "you pinned it on the board", "discarded": "you threw it away",
                      "finished": "you finished it", "in progress": "unfinished"}.get(d["status"], d["status"])
            L.append(K("past", name=describe(d), status=status, colours=col, shapes=shp))
    L.append("\n" + K("heading_now"))
    L += brain.state_lines(cfg, mood, energy)
    L.append("\n" + K("closing"))
    system = JR.system_prompt(cfg).rsplit("\n\n", 1)[0]          # its identity lines, without the journal's rules
    return [{"role": "system", "content": system + "\n\n" + K("system", shapes=", ".join(SHAPES))},
            {"role": "user", "content": "\n".join(L)}]


def _num(v, lo: float, hi: float, default: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return max(lo, min(hi, float(v)))


def parse_marks(cfg: Config, raw: dict, n: int, colours: dict[str, str]) -> dict:
    """Keep only valid marks, clamped to the sheet, up to the sitting's budget; a colour it does not have is
    dropped, not swapped for another. Raises BackendError if nothing usable is left."""
    from cat.llm import BackendError
    out = []
    for m in raw.get("marks") or []:
        if not isinstance(m, dict) or m.get("shape") not in SHAPES:
            continue
        colour = str(m.get("colour") or "").strip().lower()
        if colour not in colours:
            continue
        out.append({"shape": m["shape"], "x": round(_num(m.get("x"), 0, 100, 50), 1),
                    "y": round(_num(m.get("y"), 0, 100, 50), 1), "size": round(_num(m.get("size"), 1, 100, 20), 1),
                    "angle": round(_num(m.get("angle"), 0, 360, 0), 1),
                    "weight": m.get("weight") if m.get("weight") in WEIGHTS else "medium", "colour": colour,
                    "about": " ".join(str(m.get("about") or "").split())[:40]})
        if len(out) >= n:
            break
    if not out:
        raise BackendError("sketch returned no usable marks")
    return {"marks": out, "going_for": " ".join(str(raw.get("going_for") or "").split())[:120]}


def sketch(backend, cfg: Config, conn: Connection, **kw) -> dict | None:
    """Its own marks for this sitting, or None (sketching off, or the call failed: code's marks are used)."""
    from cat.llm import BackendError, call_json
    if not cfg.settings["drawing"].get("sketch"):
        return None
    msgs = sketch_messages(cfg, conn, **kw)
    n, colours = budget(cfg, kw["minutes"]), kw["colours"]
    try:
        return call_json(backend, "sketch", cfg.settings["brain_model"], msgs, SKETCH_SCHEMA,
                         lambda r: parse_marks(cfg, r, n, colours), cfg.settings["llm_retries"])
    except BackendError:
        return None

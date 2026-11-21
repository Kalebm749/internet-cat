"""What the website and the dev dashboard show: read-only views of nit.db (docs/rewrite-contracts.md §8).

Every function takes a read-only connection and returns plain data (dicts and lists) for templates and their
JSON twins. Nothing here writes, and nothing calls a model.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from cat import behaviour, clock, memories, mind as mindmod
from cat.body import Body, BodyState
from cat.content import Content


def _j(v: str | None, default: Any = None) -> Any:
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def state(conn: sqlite3.Connection) -> dict | None:
    r = conn.execute("SELECT data, updated_at FROM state WHERE id = 1").fetchone()
    return _j(r["data"]) if r else None


def spawned_at(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("SELECT created_at FROM character_versions WHERE version = 1").fetchone()
    return r[0] if r else None


def day_number(conn: sqlite3.Connection, sim_time: str) -> int:
    first = spawned_at(conn) or sim_time
    return clock.day_number(clock.parse(first), clock.parse(sim_time))


def _name(content: Content, action_id: str | None) -> str:
    a = content.actions.get(action_id or "")
    return a.name if a else (action_id or "")


def _place(content: Content, loc: str | None) -> str:
    l = content.locations.get(loc or "")
    return l.name if l else (loc or "")


# ---------------------------------------------------------------- now

def now(conn: sqlite3.Connection, content: Content, tuning: dict, heartbeat_age: float | None = None) -> dict | None:
    st = state(conn)
    if st is None:
        return None
    sheet = (conn.execute("SELECT sheet FROM character_versions ORDER BY version DESC LIMIT 1").fetchone() or [None])[0]
    sheet = _j(sheet, {})
    body = Body(tuning["body"], content.world)
    bs = BodyState(st["mood"], st["energy"], st["hunger"])
    desc = body.describe(bs, behaviour.repetition_score(conn, tuning["repetition"]["recent"]))
    run = conn.execute("SELECT * FROM activities WHERE status = 'running' ORDER BY id DESC LIMIT 1").fetchone()
    weather = conn.execute("SELECT value FROM facts WHERE key = 'world.weather'").fetchone()
    thought = conn.execute("SELECT sim_time, thought FROM decisions WHERE thought != '' AND fallback = 0 "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    t = clock.parse(st["sim_time"])
    stale = heartbeat_age is not None and heartbeat_age > 120
    return {
        "name": sheet.get("name", "Nit"), "pronoun": sheet.get("pronoun", "it"),
        "sim_time": st["sim_time"], "clock": st["sim_time"][-5:], "day": day_number(conn, st["sim_time"]),
        "part_of_day": clock.part_of_day(t), "weather": _j(weather[0]) if weather else None,
        "location": st["location"], "place": _place(content, st["location"]), "asleep": bool(st.get("asleep")),
        "doing": ({"id": run["id"], "action": run["action_id"], "name": _name(content, run["action_id"]),
                   "line": diary_line(content, sheet.get("name", "Nit"), run["action_id"], run["location"],
                                      _j(run["inputs"], {}), "running"), "since": run["started_at"]}
                  if run else None),
        "body": {"mood": round(st["mood"], 3), "energy": round(st["energy"], 3), "hunger": round(st["hunger"], 3),
                 "words": desc.words, "derived": desc.derived, "derived_words": desc.derived_words},
        "thought": {"sim_time": thought[0], "text": thought[1]} if thought else None,
        "engine_alive": not stale if heartbeat_age is not None else None,
    }


# ---------------------------------------------------------------- the diary

def diary_line(content: Content, name: str, action_id: str, location: str, inputs: dict, status: str) -> str:
    """The website's third-person line for an activity: the action's `diary` text, e.g. "Nit is walking to the
    market stall". Inputs naming a place read as its name. Falls back to "<name> is: <action name>"."""
    from cat import templating
    a = content.actions.get(action_id)
    shown = {k: _place(content, v) if isinstance(v, str) and v in content.locations else v for k, v in inputs.items()}
    text = None
    if a is not None and a.diary:
        try:
            text = templating.render(a.diary.replace("{name}", name), {"inputs": shown, "location": {
                "id": location, "name": _place(content, location)}, "now": "", "exits": []})
        except templating.TemplateError:
            text = None
    text = text or f"{name} is: {_name(content, action_id)}"
    if status == "rejected":
        return f"{text} (but could not)"
    if status == "interrupted":
        return f"{text} (stopped early)"
    return text


def diary(conn: sqlite3.Connection, content: Content, before: str | None = None, after: str | None = None,
          limit: int = 40) -> tuple[list[dict], str | None]:
    """Newest first: finished activities (with the thought that chose them), perceived events and nights.
    `before`/`after` are sim times. Returns (entries, next `before` for the following page or None)."""
    lo, hi = after or "", before or "9999"
    sheet = _j((conn.execute("SELECT sheet FROM character_versions ORDER BY version DESC LIMIT 1").fetchone() or [None])[0], {})
    name = sheet.get("name") or "Nit"
    rows: list[dict] = []
    for r in conn.execute(
            "SELECT a.id, a.started_at, a.ended_at, a.action_id, a.location, a.status, a.text, a.inputs, d.thought, "
            "d.letter, d.real_time "
            "FROM activities a LEFT JOIN decisions d ON d.id = a.decision_id "
            "WHERE a.ended_at IS NOT NULL AND a.ended_at > ? AND a.ended_at < ? ORDER BY a.ended_at DESC, a.id DESC "
            "LIMIT ?", (lo, hi, limit + 1)):
        minutes = round((clock.parse(r["ended_at"]) - clock.parse(r["started_at"])).total_seconds() / 60)
        action_spec = content.actions.get(r["action_id"])
        cap_row = (conn.execute("SELECT capability FROM activities WHERE id = ?", (r["id"],)).fetchone()
                   if action_spec and action_spec.capability == "draw" else None)
        cap = _j(cap_row["capability"], {}) if cap_row else {}
        artifact = cap.get("artifact") if isinstance(cap, dict) else None
        artwork_id = artifact.get("id") if isinstance(artifact, dict) and artifact.get("type") == "drawing" else None
        rows.append({"kind": "activity", "id": f"a{r['id']}", "time": r["ended_at"], "status": r["status"],
                     "started": r["started_at"], "start_day": day_number(conn, r["started_at"]),
                     "start_clock": r["started_at"][-5:], "end_clock": r["ended_at"][-5:], "minutes": minutes,
                     "decided_real": (r["real_time"] or "")[11:19] or None,
                     "action": r["action_id"], "name": _name(content, r["action_id"]),
                     "place": _place(content, r["location"]), "text": r["text"],
                     "line": diary_line(content, name, r["action_id"], r["location"], _j(r["inputs"], {}), r["status"]),
                     "thought": r["thought"] or None,
                     "artwork_id": artwork_id,
                     "answered_letter": bool(r["letter"])})
    for r in conn.execute("SELECT id, sim_time, text, location FROM experiences WHERE kind = 'event' AND sim_time > ? "
                          "AND sim_time < ? ORDER BY sim_time DESC, id DESC LIMIT ?", (lo, hi, limit + 1)):
        rows.append({"kind": "event", "id": f"e{r['id']}", "time": r["sim_time"], "text": r["text"],
                     "place": _place(content, r["location"])})
    for r in conn.execute("SELECT id, finished_at, summary FROM sleep_runs WHERE summary IS NOT NULL AND summary != '' "
                          "AND finished_at > ? AND finished_at < ? ORDER BY finished_at DESC LIMIT ?", (lo, hi, limit + 1)):
        rows.append({"kind": "night", "id": f"n{r['id']}", "time": r["finished_at"], "text": r["summary"]})
    rows.sort(key=lambda e: (e["time"], e["id"]), reverse=True)
    more = len(rows) > limit
    rows = rows[:limit]
    for e in rows:
        e["day"] = day_number(conn, e["time"])
        e["clock"] = e["time"][-5:]
    return rows, (rows[-1]["time"] if more and rows else None)


# ---------------------------------------------------------------- who it is

def identity(conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT version, sheet, created_at FROM character_versions ORDER BY version DESC LIMIT 1").fetchone()
    sheet = _j(r["sheet"], {}) if r else {}
    core = sheet.get("core") or {}
    return {"version": r["version"] if r else 0, "name": sheet.get("name"), "nature": core.get("nature", ""),
            "senses": core.get("senses") or [], "abilities": core.get("abilities") or [],
            "boundaries": core.get("boundaries") or [],
            "traits": mindmod._trait_words(sheet.get("traits") or {}), "traits_raw": sheet.get("traits") or {},
            "tendencies": sheet.get("tendencies") or {}, "background": sheet.get("background", ""),
            "core_memories": memories.core_texts(conn),
            "likes": [p.words or p.subject_id for p in memories.preferences(conn).values() if p.strength >= 0.15],
            "dislikes": [p.words or p.subject_id for p in memories.preferences(conn).values() if p.strength <= -0.15]}


def places(conn: sqlite3.Connection, content: Content) -> list[dict]:
    """Every place: what is there, what can be seen, what Nit has found it can do there (undiscovered ones counted)."""
    known = {r[0] for r in conn.execute("SELECT action_id FROM knowledge")}
    facts = {r["key"]: _j(r["value"]) for r in conn.execute("SELECT key, value FROM facts")}
    out = []
    for loc in content.locations.values():
        seen = []
        for key in loc.facts:
            spec = content.facts[key]
            if spec.visible == "none" or key == content.world.get("wishes"):
                continue
            v = facts.get(key, spec.default)
            seen.append(f"{key.split('.', 1)[1].replace('_', ' ')}: {len(v) if isinstance(v, list) else v}")
        acts = [content.actions[a] for a in loc.actions]
        out.append({"id": loc.id, "name": loc.name, "description": loc.description.strip(),
                    "exits": [_place(content, x) for x in loc.exits], "sheltered": loc.sheltered, "visible": seen,
                    "known": [a.name for a in acts if a.id in known],
                    "undiscovered": sum(1 for a in acts if a.id not in known)})
    return out


def wishes(conn: sqlite3.Connection, content: Content) -> list[dict]:
    key = content.world.get("wishes")
    if not key:
        return []
    r = conn.execute("SELECT value FROM facts WHERE key = ?", (key,)).fetchone()
    out = []
    for w in reversed(_j(r[0], []) if r else []):
        if isinstance(w, dict):
            out.append({"id": str(w.get("id")), "text": w.get("text", ""), "status": w.get("status", "open"),
                        "made": w.get("made"), "day": day_number(conn, w["made"]) if w.get("made") else None,
                        "granted_at": w.get("granted_at"), "note": w.get("note")})
    return out


def knowledge(conn: sqlite3.Connection, content: Content) -> list[dict]:
    return [{"action": r["action_id"], "name": _name(content, r["action_id"]), "place": _place(content, r["location"]),
             "discovered_at": r["discovered_at"], "how": r["discovered_by"], "provisional": bool(r["provisional"]),
             "own_words": r["own_words"], "times_done": r["times_done"], "last_done_at": r["last_done_at"]}
            for r in conn.execute("SELECT * FROM knowledge ORDER BY discovered_at, action_id")]


def character_history(conn: sqlite3.Connection) -> dict:
    versions = [{"version": r["version"], "created_at": r["created_at"], "reason": r["reason"],
                 "change": _j(r["change"]), "evidence": _j(r["evidence"], [])}
                for r in conn.execute("SELECT * FROM character_versions ORDER BY version DESC")]
    proposals = [{"id": r["id"], "sim_time": r["sim_time"], "proposal": _j(r["proposal"], {}),
                  "verdicts": _j(r["verdicts"], []), "accepted": bool(r["accepted"]), "version": r["version"]}
                 for r in conn.execute("SELECT * FROM dev_proposals ORDER BY id DESC")]
    prefs = [{"subject": f"{p.subject_kind}:{p.subject_id}", "valence": round(p.valence, 3),
              "confidence": round(p.confidence, 3), "strength": round(p.strength, 3), "words": p.words,
              "evidence": len(p.evidence), "updated_at": p.updated_at}
             for p in sorted(memories.preferences(conn).values(), key=lambda p: -abs(p.strength))]
    return {"versions": versions, "proposals": proposals, "preferences": prefs}


def memory_view(conn: sqlite3.Connection, content: Content, tuning: dict, sim_time: str) -> dict:
    out = {"core": [], "long": []}
    for m in memories.all_memories(conn):
        row = {"id": m.id, "text": m.text, "importance": round(m.importance, 2), "created_at": m.created_at,
               "sources": m.sources, "recalls": m.recall_count, "last_recalled": m.last_recalled,
               "retention": round(memories.retention(m, sim_time, tuning["memory"]), 3)}
        out[m.kind].append(row)
    sleep_ids = [a.id for a in content.actions.values() if a.builtin == "sleep"]
    out["notable_today"] = memories.notable_today(conn, tuning, memories.last_wake(conn, sleep_ids))
    out["forget_below"] = tuning["memory"]["forget_below"]
    return out


# ---------------------------------------------------------------- engine internals (dashboard)

def decisions(conn: sqlite3.Connection, before_id: int | None = None, limit: int = 50) -> list[dict]:
    q = "SELECT id, sim_time, real_time, kind, triggers, action, inputs, thought, fallback, ms, letter FROM decisions"
    args: tuple = ()
    if before_id:
        q += " WHERE id < ?"
        args = (before_id,)
    q += " ORDER BY id DESC LIMIT ?"
    return [{"id": r["id"], "sim_time": r["sim_time"], "real_time": r["real_time"], "kind": r["kind"],
             "triggers": [t["kind"] for t in _j(r["triggers"], [])], "action": r["action"], "inputs": _j(r["inputs"], {}),
             "thought": r["thought"], "fallback": bool(r["fallback"]), "ms": r["ms"], "letter": r["letter"]}
            for r in conn.execute(q, args + (limit,))]


def decision(conn: sqlite3.Connection, did: int) -> dict | None:
    r = conn.execute("SELECT * FROM decisions WHERE id = ?", (did,)).fetchone()
    if r is None:
        return None
    acts = [dict(a) for a in conn.execute("SELECT id, action_id, status, started_at, ended_at, planned_minutes, text, "
                                          "freshness, repeat_parts, restart_of, changes FROM activities WHERE decision_id = ?",
                                          (did,))]
    for a in acts:
        a["repeat_parts"], a["changes"] = _j(a["repeat_parts"], {}), _j(a["changes"], [])
    return {"id": r["id"], "sim_time": r["sim_time"], "real_time": r["real_time"], "kind": r["kind"],
            "triggers": _j(r["triggers"], []), "action": r["action"], "inputs": _j(r["inputs"], {}),
            "thought": r["thought"], "reasoning": r["reasoning"], "preserve": bool(r["preserve"]), "letter": r["letter"],
            "fallback": bool(r["fallback"]), "notes": _j(r["notes"], []), "attempts": _j(r["attempts"], []),
            "messages": _j(r["messages"], []), "mind": _j(r["mind"], {}), "dropped": _j(r["dropped"], []),
            "ms": r["ms"], "activities": acts}


def body_series(conn: sqlite3.Connection, hours: float, sim_time: str) -> dict[str, list[tuple[str, float]]]:
    """Mood, energy and hunger over the last `hours` sim hours (the value after each change, thinned)."""
    since = clock.fmt(clock.parse(sim_time) - timedelta(hours=hours))
    out: dict[str, list] = {"mood": [], "energy": [], "hunger": []}
    for r in conn.execute("SELECT sim_time, var, value FROM body_changes WHERE sim_time >= ? ORDER BY id", (since,)):
        s = out.get(r["var"])
        if s is not None:
            if s and s[-1][0] == r["sim_time"]:
                s[-1] = (r["sim_time"], r["value"])
            else:
                s.append((r["sim_time"], r["value"]))
    return out


def sparkline(series: list[tuple[str, float]], lo: float, hi: float, w: int = 320, h: int = 48) -> str:
    """A small inline SVG polyline (no JavaScript, no CDN)."""
    if len(series) < 2:
        return ""
    t0, t1 = clock.parse(series[0][0]), clock.parse(series[-1][0])
    span = max(1.0, (t1 - t0).total_seconds())
    pts = " ".join(f"{(clock.parse(t) - t0).total_seconds() / span * w:.1f},{h - (v - lo) / (hi - lo) * h:.1f}"
                   for t, v in series)
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="spark" role="img">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{pts}"/></svg>')


def repetition_view(conn: sqlite3.Connection, tuning: dict) -> dict:
    rows = conn.execute("SELECT action_id, COUNT(*) AS n, AVG(freshness) AS f, MIN(freshness) AS fmin, "
                        "SUM(restart_of IS NOT NULL) AS restarts FROM activities WHERE status IN ('finished', 'interrupted') "
                        "GROUP BY action_id ORDER BY n DESC").fetchall()
    recent = conn.execute("SELECT id, started_at, action_id, freshness, repeat_parts, restart_of FROM activities "
                          "WHERE status IN ('finished', 'interrupted', 'running') ORDER BY id DESC LIMIT 30").fetchall()
    th = conn.execute("SELECT COUNT(*), COALESCE(SUM(repeated), 0) FROM thoughts").fetchone()
    return {"score": behaviour.repetition_score(conn, tuning["repetition"]["recent"]),
            "by_action": [{"action": r["action_id"], "count": r["n"], "mean_freshness": round(r["f"], 2),
                           "min_freshness": round(r["fmin"], 2), "restarts": r["restarts"]} for r in rows],
            "recent": [{"id": r["id"], "started_at": r["started_at"], "action": r["action_id"],
                        "freshness": r["freshness"], "parts": _j(r["repeat_parts"], {}), "restart_of": r["restart_of"]}
                       for r in recent],
            "thoughts": {"total": th[0], "repeated": th[1]},
            "repeated_thoughts": [dict(r) for r in conn.execute(
                "SELECT sim_time, text FROM thoughts WHERE repeated = 1 ORDER BY id DESC LIMIT 15")]}


def world_view(conn: sqlite3.Connection, content: Content) -> dict:
    facts = []
    for r in conn.execute("SELECT key, value, touched, seq FROM facts ORDER BY key"):
        spec = content.facts.get(r["key"])
        facts.append({"key": r["key"], "value": _j(r["value"]), "touched": bool(r["touched"]), "seq": r["seq"],
                      "type": spec.type if spec else "?", "visible": spec.visible if spec else "?",
                      "declared": spec is not None})
    events = [dict(r) for r in conn.execute("SELECT sim_time, location, text FROM experiences WHERE kind = 'event' "
                                            "ORDER BY id DESC LIMIT 30")]
    usage = [dict(r) for r in conn.execute("SELECT capability, day, uses FROM capability_usage ORDER BY day DESC, capability")]
    return {"facts": facts, "events": events, "capability_usage": usage,
            "locations": [{"id": l.id, "name": l.name, "exits": l.exits, "actions": l.actions}
                          for l in content.locations.values()]}


def sleep_view(conn: sqlite3.Connection) -> list[dict]:
    return [{"id": r["id"], "started_at": r["started_at"], "finished_at": r["finished_at"], "status": r["status"],
             "summary": r["summary"], "repeated": bool(r["repeated"]), "stages": _j(r["stages"], {})}
            for r in conn.execute("SELECT * FROM sleep_runs ORDER BY id DESC LIMIT 30")]


def mail_view(conn: sqlite3.Connection, content: Content) -> dict:
    key = content.world.get("mailbox")
    box = _j((conn.execute("SELECT value FROM facts WHERE key = ?", (key,)).fetchone() or [None])[0], []) if key else []
    pile_key = None
    if key:
        loc = key.split(".", 1)[0]
        pile_key = f"{loc}.answered" if f"{loc}.answered" in content.facts else None
    pile = _j((conn.execute("SELECT value FROM facts WHERE key = ?", (pile_key,)).fetchone() or [None])[0], []) \
        if pile_key else []
    imports = [dict(r) for r in conn.execute("SELECT * FROM mail_imports ORDER BY letter_id DESC LIMIT 50")]
    return {"mailbox": box, "answered": pile, "imports": imports}


def content_check(content_dir: Path, settings_path: Path) -> dict:
    from cat import content as contentmod, tuning as tuningmod
    out: dict = {"content": "ok", "content_problems": [], "tuning": "ok", "tuning_problems": []}
    try:
        contentmod.load(content_dir)
    except contentmod.ContentError as e:
        out["content"], out["content_problems"] = "invalid", e.problems
    try:
        tuningmod.load(settings_path)
    except tuningmod.TuningError as e:
        out["tuning"], out["tuning_problems"] = "invalid", e.problems
    return out


def counts(conn: sqlite3.Connection) -> dict:
    tables = ("decisions", "activities", "experiences", "memories", "knowledge", "preferences", "character_versions",
              "sleep_runs", "thoughts", "mail_imports")
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

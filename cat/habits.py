"""Habits: patterns in what the entity actually did, found by code and worded by the sleeping mind.

Code finds them (a move it keeps making at the same part of the day, counted over the last
`habits.window_ticks` decisions); the LLM only puts a short line to each. A habit is a description of
the past, shown to the brain as "you tend to..."; it never ranks, marks or suggests a move."""
from __future__ import annotations

from sqlite3 import Connection
from typing import Any

from cat import clock
from cat import prompts as P
from cat.config import Config

HABITS_SCHEMA = {
    "type": "object",
    "properties": {"habits": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["id", "text"]}}},
    "required": ["habits"],
}


def mine(conn: Connection, cfg: Config) -> list[dict]:
    """Candidates: (move, part of the day) pairs that recur. Strongest first, at most habits.max_stored."""
    h = cfg.settings["habits"]
    rows = conn.execute("SELECT sim_time, move_key, action FROM ticks WHERE kind='tick' AND move_key IS NOT NULL "
                        "ORDER BY id DESC LIMIT ?", (h["window_ticks"],)).fetchall()
    totals: dict[str, int] = {}
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:                                   # newest first: the first label seen is the latest wording
        part = clock.part_of_day(clock.parse(r["sim_time"]))
        totals[part] = totals.get(part, 0) + 1
        e = seen.setdefault((r["move_key"], part), {"move_key": r["move_key"], "part": part,
                                                    "label": r["action"] or r["move_key"], "count": 0})
        e["count"] += 1
    out = []
    for e in seen.values():
        total = totals[e["part"]]
        share = e["count"] / total
        if e["count"] >= h["min_count"] and share >= h["min_share"]:
            out.append({**e, "key": f"{e['move_key']}|{e['part']}", "total": total, "share": round(share, 2)})
    out.sort(key=lambda e: (-e["share"] * e["count"], e["key"]))
    return out[:h["max_stored"]]


def fallback_text(cfg: Config, c: dict) -> str:
    return P.render(cfg, "habits.fallback", label=c["label"], part=c["part"])


def messages(cfg: Config, summary: str, cands: list[dict]) -> list[dict]:
    c = cfg.character
    lines = "\n".join(f"- [{i}] {x['label']} in the {x['part']} ({x['count']} of its last {x['total']} "
                      f"decisions then)" for i, x in enumerate(cands))
    sysm = P.render(cfg, "sleep.system", name=c["name"], core_traits=", ".join(c["core_traits"]),
                    job=P.render(cfg, "sleep.jobs.habits"))
    return [{"role": "system", "content": sysm},
            {"role": "user", "content": P.render(cfg, "sleep.habits", summary=summary, candidates=lines)}]


def decide(cfg: Config, existing: dict[str, dict], cands: list[dict], raw: Any, day: str) -> list[dict]:
    """The new habit set. Wording comes from the model when it gave a usable line, else the one it already
    had, else the template. Habits that no longer recur are dropped (code owns existence)."""
    h = cfg.settings["habits"]
    said: dict[int, str] = {}
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, dict) and isinstance(it.get("id"), int) and isinstance(it.get("text"), str):
            t = " ".join(it["text"].split())[:h["text_max_length"]]
            if t:
                said[it["id"]] = t
    out = []
    for i, c in enumerate(cands):
        old = existing.get(c["key"])
        text = said.get(i) or (old["text"] if old else "") or fallback_text(cfg, c)
        out.append({"key": c["key"], "text": text, "part": c["part"], "count": c["count"],
                    "share": c["share"], "formed_day": old["formed_day"] if old else day})
    return out


def commit(conn: Connection, habits: list[dict], sim_time: str) -> None:
    conn.execute("DELETE FROM habits")
    for x in habits:
        conn.execute("INSERT INTO habits(key,text,part,count,share,formed_day,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (x["key"], x["text"], x["part"], x["count"], x["share"], x["formed_day"], sim_time))


def shown(conn: Connection, cfg: Config) -> list[str]:
    """Lines for the brain prompt (most pronounced first)."""
    try:
        rows = conn.execute("SELECT text FROM habits ORDER BY share*count DESC, id LIMIT ?",
                            (cfg.settings["habits"]["max_shown"],)).fetchall()
    except Exception:
        return []
    return [r["text"] for r in rows]

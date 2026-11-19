"""The sleep cycle: consolidate the day's memories, drift tendencies a little, learn preferences.

`plan()` does all the LLM/embedding work and writes nothing. `commit()` applies the result in
ONE transaction. If any LLM call fails, nothing is written and the engine retries later, so a
half-done night never happens. Core traits are never touched; tendencies are always clamped."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from sqlite3 import Connection
from typing import Any

from cat import clock
from cat.config import Config
from cat.db import dumps
from cat.llm import BackendError, LLMBackend, call_json
from cat.memory import Memory

log = logging.getLogger("cat.sleep")

CONSOLIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "memories": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"},
                           "importance": {"type": "string", "enum": ["low", "medium", "high"]}},
            "required": ["text", "importance"]}},
    },
    "required": ["summary", "memories"],
}


def tendencies_schema(cfg: Config) -> dict:
    return {"type": "object",
            "properties": {"changes": {"type": "object", "properties": {
                t: {"type": "number"} for t in cfg.character["tendencies"]}}},
            "required": ["changes"]}


PREFERENCES_SCHEMA = {
    "type": "object",
    "properties": {"preferences": {"type": "array", "items": {
        "type": "object",
        "properties": {"subject": {"type": "string"}, "valence": {"type": "number"},
                       "reason": {"type": "string"}},
        "required": ["subject", "valence", "reason"]}}},
    "required": ["preferences"],
}


@dataclass
class SleepPlan:
    summary: str
    memories: list[dict] = field(default_factory=list)          # prepared (embedded) long memories
    tendencies: dict[str, float] = field(default_factory=dict)  # full new tendency values
    tendency_changes: dict[str, dict] = field(default_factory=dict)  # {name: {from, to}}
    preferences: list[dict] = field(default_factory=list)       # {subject, valence, reason, previous}
    last_tick_id: int = 0


# ---------- pure helpers (unit-tested) ----------
def clamp_tendencies(cfg: Config, current: dict[str, float],
                     proposed: Any) -> tuple[dict[str, float], dict[str, dict]]:
    """Each proposal is -1..1; it is scaled by `tendency_max_step` and clamped to 0..1.
    Unknown tendencies and non-numbers are ignored. Never moves more than the max step."""
    step = cfg.settings["sleep"]["tendency_max_step"]
    new, changes = dict(current), {}
    if not isinstance(proposed, dict):
        return new, changes
    for name, old in current.items():
        v = proposed.get(name)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        delta = max(-1.0, min(1.0, float(v))) * step
        val = round(max(0.0, min(1.0, old + delta)), 4)
        if val != round(old, 4):
            new[name] = val
            changes[name] = {"from": round(old, 4), "to": val}
    return new, changes


def merge_preferences(cfg: Config, existing: dict[str, dict], proposed: Any) -> list[dict]:
    """Blend tonight's opinions into existing valences. A brand-new preference starts weak
    (proposal x blend) and strengthens only if the experience repeats."""
    sl = cfg.settings["sleep"]
    if not isinstance(proposed, list):
        return []
    items = []
    for p in proposed:
        if not isinstance(p, dict):
            continue
        subj = str(p.get("subject") or "").strip()[:sl["max_preference_subject_length"]]
        v = p.get("valence")
        if not subj or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        items.append((subj, max(-1.0, min(1.0, float(v))), str(p.get("reason") or "").strip()))
    items.sort(key=lambda t: -abs(t[1]))
    out, seen = [], set()
    for subj, v, reason in items:
        if len(out) >= sl["max_preferences_per_night"]:
            break
        key = subj.lower()
        if key in seen:
            continue
        seen.add(key)
        old = existing.get(key)
        blend = sl["preference_blend"]
        new = v * blend if old is None else old["valence"] * (1 - blend) + v * blend
        new = round(max(-1.0, min(1.0, new)), 3)
        out.append({"subject": old["subject"] if old else subj, "valence": new,
                    "reason": reason or (old["reason"] if old else ""),
                    "previous": old["valence"] if old else None})
    return out


# ---------- gathering ----------
def _last_sleep_tick_id(conn: Connection) -> int:
    from cat.db import get_meta
    return int(get_meta(conn, "last_sleep_tick_id", "0") or 0)


def day_log(conn: Connection, cfg: Config) -> tuple[list[str], int]:
    """The day's tick log since the last completed sleep, newest `day_log_max_ticks`."""
    since = _last_sleep_tick_id(conn)
    rows = conn.execute(
        "SELECT id, sim_time, action, outcome FROM ticks WHERE kind='tick' AND id>? "
        "ORDER BY id DESC LIMIT ?", (since, cfg.settings["sleep"]["day_log_max_ticks"])).fetchall()
    top = conn.execute("SELECT COALESCE(MAX(id),0) FROM ticks").fetchone()[0]
    lines = [f"{clock.time_label(clock.parse(r['sim_time']))}: {r['action']} -> {r['outcome']}"
             for r in rows][::-1]
    return lines, top


# ---------- prompts ----------
def _sys(cfg: Config, job: str) -> str:
    c = cfg.character
    return (f"You are the sleeping mind of {c['name']}, a {c['species']} "
            f"(core traits: {', '.join(c['core_traits'])}). {job} Reply with JSON only.")


def consolidate_messages(cfg: Config, shorts: list[str], log_lines: list[str]) -> list[dict]:
    n = cfg.settings["sleep"]["max_new_memories"]
    user = ("Today's notable moments:\n" + ("\n".join(f"- {t}" for t in shorts) or "(none)")
            + "\n\nToday's log:\n" + ("\n".join(f"- {t}" for t in log_lines) or "(nothing)")
            + f"\n\nWrite: a one or two sentence summary of the day (what {cfg.character['name']} "
              f"might dream about), and 1 to {n} lasting memories worth keeping, each one short "
              "plain sentence in the first person. Merge trivial moments; drop the rest. "
              "importance: low (small thing), medium (meaningful) or high (life-changing).")
    return [{"role": "system", "content": _sys(cfg, "You sort out the day's memories.")},
            {"role": "user", "content": user}]


def tendencies_messages(cfg: Config, summary: str, current: dict[str, float]) -> list[dict]:
    labels = cfg.character.get("tendency_labels", {})
    lines = []
    for k, v in current.items():
        ends = f" ({labels[k][0]} <-> {labels[k][1]})" if k in labels else ""
        lines.append(f"- {k}: {v:.2f}{ends}")
    user = (f"Summary of the day: {summary}\n\nCurrent tendencies (0 to 1):\n" + "\n".join(lines)
            + "\n\nFor each tendency give a number from -1 to 1: how much today nudged it "
              "(-1 = clearly lower, 0 = no change, 1 = clearly higher). Most should be near 0. "
              "Core traits never change.")
    return [{"role": "system", "content": _sys(cfg, "You decide how the day shaped the creature.")},
            {"role": "user", "content": user}]


def preferences_messages(cfg: Config, summary: str, log_lines: list[str],
                         existing: dict[str, dict]) -> list[dict]:
    n = cfg.settings["sleep"]["max_preferences_per_night"]
    ex = "\n".join(f"- {p['subject']} ({p['valence']:+.1f})" for p in existing.values()) or "(none yet)"
    user = (f"Summary of the day: {summary}\n\nToday's log:\n"
            + ("\n".join(f"- {t}" for t in log_lines) or "(nothing)")
            + f"\n\nExisting likes (+) and dislikes (-):\n{ex}\n\nName up to {n} specific things "
              "(a place, object, weather, activity) the creature clearly liked or disliked today. "
              "valence: -1 (strongly dislikes) to 1 (strongly likes). reason: a few words. "
              "Return an empty list if nothing stood out.")
    return [{"role": "system", "content": _sys(cfg, "You work out what the creature has come to like or dislike.")},
            {"role": "user", "content": user}]


# ---------- parsing ----------
def parse_consolidation(cfg: Config, raw: dict) -> tuple[str, list[tuple[str, float]]]:
    sl = cfg.settings["sleep"]
    summary = str(raw.get("summary") or "").strip()
    mems = []
    for m in raw.get("memories") or []:
        if not isinstance(m, dict):
            continue
        text = str(m.get("text") or "").strip()
        imp = sl["importance_values"].get(str(m.get("importance")), sl["importance_values"]["medium"])
        if text:
            mems.append((text, float(imp)))
    if not summary and not mems:
        raise BackendError("consolidation returned nothing usable")
    return summary or "An uneventful day.", mems[:sl["max_new_memories"]]


# ---------- the cycle ----------
def plan(cfg: Config, backend: LLMBackend, conn: Connection, memory: Memory,
         current_tendencies: dict[str, float], sim_time: str) -> SleepPlan:
    """All LLM + embedding work. Raises BackendError (and writes nothing) if any call fails."""
    s = cfg.settings
    model, retries = s["sleep_model"], s["llm_retries"]
    shorts = [m["text"] for m in memory.all_short()]
    lines, top_id = day_log(conn, cfg)

    if shorts or lines:
        summary, mems = call_json(backend, "consolidate", model,
                                  consolidate_messages(cfg, shorts, lines), CONSOLIDATE_SCHEMA,
                                  lambda r: parse_consolidation(cfg, r), retries)
    else:
        summary, mems = "An uneventful day.", []

    raw_t = call_json(backend, "tendencies", model,
                      tendencies_messages(cfg, summary, current_tendencies),
                      tendencies_schema(cfg), lambda r: r.get("changes"), retries)
    new_t, t_changes = clamp_tendencies(cfg, current_tendencies, raw_t)   # never skipped

    rows = conn.execute("SELECT subject, valence, reason FROM preferences").fetchall()
    existing = {r["subject"].lower(): dict(r) for r in rows}
    raw_p = call_json(backend, "preferences", model,
                      preferences_messages(cfg, summary, lines, existing),
                      PREFERENCES_SCHEMA, lambda r: r.get("preferences"), retries)
    prefs = merge_preferences(cfg, existing, raw_p)

    prepared = [memory.prepare(text, imp, sim_time) for text, imp in mems]   # embeddings
    return SleepPlan(summary, prepared, new_t, t_changes, prefs, top_id)


def commit(conn: Connection, cfg: Config, memory: Memory, p: SleepPlan, sim_time: str,
           real_time: str, sim_date: str | None = None) -> None:
    """Apply a plan. Call inside a transaction (the caller also saves the new tendencies).
    `sim_date` is the date the sleep began (the website's "last night")."""
    from cat.db import get_meta, set_meta
    sl = cfg.settings["sleep"]
    for m in p.memories:
        memory.save(m, "long")
    memory.clear_short()
    memory.prune_long()
    for pr in p.preferences:
        row = conn.execute("SELECT id FROM preferences WHERE lower(subject)=lower(?)",
                           (pr["subject"],)).fetchone()
        if row:
            conn.execute("UPDATE preferences SET valence=?, reason=?, updated_at=? WHERE id=?",
                         (pr["valence"], pr["reason"], sim_time, row["id"]))
        else:
            conn.execute("INSERT INTO preferences(subject,valence,reason,updated_at) VALUES(?,?,?,?)",
                         (pr["subject"], pr["valence"], pr["reason"], sim_time))
    # forget weak preferences, then cap the total
    conn.execute("DELETE FROM preferences WHERE abs(valence) < ?", (sl["preference_min_abs"],))
    conn.execute(
        "DELETE FROM preferences WHERE id NOT IN (SELECT id FROM preferences "
        "ORDER BY abs(valence) DESC, id DESC LIMIT ?)", (sl["max_preferences_total"],))
    conn.execute(
        "INSERT INTO sleep_log(sim_date,summary,tick_id,tendency_changes,new_memories,"
        "new_preferences) VALUES(?,?,?,?,?,?)",
        ((sim_date or sim_time[:10]), p.summary, int(get_meta(conn, "sleep_tick_id", "0") or 0), dumps(p.tendency_changes), dumps([m["text"] for m in p.memories]),
         dumps([{"subject": x["subject"], "valence": x["valence"], "reason": x["reason"]}
                for x in p.preferences])))
    set_meta(conn, "sleep_pending", 0)
    set_meta(conn, "last_sleep_tick_id", p.last_tick_id)

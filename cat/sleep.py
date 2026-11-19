"""The sleep cycle: consolidate the day's memories, drift tendencies a little, learn preferences,
and (at most one line a night) revise what it thinks it is.

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
from cat import prompts as P
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


SELF_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {"self_image": {"type": "array", "maxItems": 1, "items": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "confidence": {"type": "number"},
                       "replaces": {"type": "integer"}},
        "required": ["text", "confidence"]}}},
    "required": ["self_image"],
}


@dataclass
class SleepPlan:
    summary: str
    memories: list[dict] = field(default_factory=list)          # prepared (embedded) long memories
    tendencies: dict[str, float] = field(default_factory=dict)  # full new tendency values
    tendency_changes: dict[str, dict] = field(default_factory=dict)  # {name: {from, to}}
    preferences: list[dict] = field(default_factory=list)       # {subject, valence, reason, previous}
    self_image: dict = field(default_factory=lambda: {"action": "none"})   # see decide_self_image()
    last_tick_id: int = 0
    trace_steps: list[dict] = field(default_factory=list)   # for the dev dashboard (sleep_traces)


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


def decide_self_image(cfg: Config, existing: list[dict], proposed: Any, sim_date: str) -> dict:
    """At most one proposal -> an outcome the commit applies. `existing` rows are
    {id, text, confidence, formed_day}. A proposal that names an existing line in `replaces`
    revises or contradicts it (the old line is replaced). Over the cap, the least confident
    lines are forgotten, the new one included. Always returns a dict with an `action`:
    none | rejected | added | revised | dropped_at_cap; plus text/confidence/replaced/dropped."""
    sl = cfg.settings["sleep"]
    items = proposed if isinstance(proposed, list) else []
    p = next((x for x in items if isinstance(x, dict)), None)
    if p is None:
        return {"action": "none", "reason": "no proposal"}
    text = " ".join(str(p.get("text") or "").split())[:sl["max_self_image_length"]].strip()
    conf = p.get("confidence")
    if not text:
        return {"action": "rejected", "reason": "empty text"}
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf):
        return {"action": "rejected", "text": text, "reason": "confidence is not a number"}
    conf = round(max(0.0, min(1.0, float(conf))), 3)
    if conf < sl["self_image_min_confidence"]:
        return {"action": "rejected", "text": text, "confidence": conf,
                "reason": f"confidence below {sl['self_image_min_confidence']}"}
    dup = next((e for e in existing if e["text"].strip().lower() == text.lower()), None)
    if dup:
        return {"action": "rejected", "text": text, "confidence": conf,
                "reason": f"already a line (id {dup['id']})"}
    rid = p.get("replaces")
    old = next((e for e in existing if not isinstance(rid, bool) and e["id"] == rid), None)
    rows = [e for e in existing if e is not old]
    new = {"id": None, "text": text, "confidence": conf, "formed_day": sim_date}
    # least confident first to go; among equals the older one goes
    order = sorted(rows + [new], key=lambda e: (e["confidence"], e["formed_day"] or "", e["id"] or 0))
    drop = order[:max(0, len(order) - sl["max_self_image_lines"])]
    out = {"action": "revised" if old else "added", "text": text, "confidence": conf,
           "formed_day": sim_date, "replaced": ({"id": old["id"], "text": old["text"],
                                                  "confidence": old["confidence"]} if old else None),
           "dropped": [{"id": e["id"], "text": e["text"], "confidence": e["confidence"]}
                       for e in drop if e["id"] is not None]}
    if any(e["id"] is None for e in drop):
        out["action"] = "dropped_at_cap"
        out["reason"] = "less confident than every line kept at the cap"
        out["replaced"] = None   # the old line stays: nothing replaced it
        out["dropped"] = []
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
    return P.render(cfg, "sleep.system", name=c["name"], core_traits=", ".join(c["core_traits"]),
                    job=P.render(cfg, "sleep.jobs." + job))


def _lines(cfg: Config, items: list[str], none_key: str) -> str:
    return "\n".join(f"- {t}" for t in items) or P.render(cfg, "sleep." + none_key)


def self_image_messages(cfg: Config, summary: str, shorts: list[str], log_lines: list[str],
                        existing: list[dict]) -> list[dict]:
    ex = "\n".join(f"- [{e['id']}] {e['text']} (confidence {e['confidence']:.1f})"
                   for e in existing) or P.render(cfg, "sleep.none_existing_self_image")
    seed = (cfg.character.get("self_image") or {}).get("seed", "")
    user = P.render(cfg, "sleep.self_image", summary=summary, shorts=_lines(cfg, shorts, "none_shorts"),
                    log=_lines(cfg, log_lines, "none_log"), existing=ex,
                    seed_hint=P.render(cfg, "sleep.seed_hint", seed=seed) if seed else "")
    return [{"role": "system", "content": _sys(cfg, "self_image")},
            {"role": "user", "content": user}]


def consolidate_messages(cfg: Config, shorts: list[str], log_lines: list[str]) -> list[dict]:
    user = P.render(cfg, "sleep.consolidate", shorts=_lines(cfg, shorts, "none_shorts"),
                    log=_lines(cfg, log_lines, "none_log"), name=cfg.character["name"],
                    n=cfg.settings["sleep"]["max_new_memories"])
    return [{"role": "system", "content": _sys(cfg, "consolidate")},
            {"role": "user", "content": user}]


def tendencies_messages(cfg: Config, summary: str, current: dict[str, float]) -> list[dict]:
    labels = cfg.character.get("tendency_labels", {})
    lines = []
    for k, v in current.items():
        ends = f" ({labels[k][0]} <-> {labels[k][1]})" if k in labels else ""
        lines.append(f"- {k}: {v:.2f}{ends}")
    user = P.render(cfg, "sleep.tendencies", summary=summary, tendencies="\n".join(lines))
    return [{"role": "system", "content": _sys(cfg, "tendencies")},
            {"role": "user", "content": user}]


def preferences_messages(cfg: Config, summary: str, log_lines: list[str],
                         existing: dict[str, dict]) -> list[dict]:
    ex = "\n".join(f"- {p['subject']} ({p['valence']:+.1f})" for p in existing.values()) \
        or P.render(cfg, "sleep.none_existing_preferences")
    user = P.render(cfg, "sleep.preferences", summary=summary, log=_lines(cfg, log_lines, "none_log"),
                    existing=ex, n=cfg.settings["sleep"]["max_preferences_per_night"])
    return [{"role": "system", "content": _sys(cfg, "preferences")},
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
    step = cfg.settings["sleep"]["tendency_max_step"]

    rows = conn.execute("SELECT subject, valence, reason FROM preferences").fetchall()
    existing = {r["subject"].lower(): dict(r) for r in rows}
    raw_p = call_json(backend, "preferences", model,
                      preferences_messages(cfg, summary, lines, existing),
                      PREFERENCES_SCHEMA, lambda r: r.get("preferences"), retries)
    prefs = merge_preferences(cfg, existing, raw_p)

    si_rows = [dict(r) for r in conn.execute(
        "SELECT id, text, confidence, formed_day FROM self_image ORDER BY id")]
    raw_s = call_json(backend, "self_image", model,
                      self_image_messages(cfg, summary, shorts, lines, si_rows),
                      SELF_IMAGE_SCHEMA, lambda r: r.get("self_image"), retries)
    from cat.db import get_meta
    day = (get_meta(conn, "sleep_started") or sim_time)[:10]       # the day the sleep began, as in sleep_log
    si = decide_self_image(cfg, si_rows, raw_s, day)
    log.info("self-image proposal: %s", si["action"], extra={"event": "self_image", "extra": si})

    prepared = [memory.prepare(text, imp, sim_time) for text, imp in mems]   # embeddings
    steps = [
        {"step": "consolidate", "input": {"short_memories": shorts, "day_log": lines},
         "output": {"summary": summary, "memories": [{"text": t, "importance": i} for t, i in mems]},
         "proposed": None, "clamped": None},
        {"step": "tendencies", "input": {"summary": summary, "current": current_tendencies},
         "output": raw_t,
         "proposed": {k: (raw_t.get(k) if isinstance(raw_t, dict) else None) for k in current_tendencies},
         "clamped": {k: {"from": current_tendencies[k], "to": new_t[k],
                         "applied_delta": round(new_t[k] - current_tendencies[k], 4), "max_step": step}
                     for k in current_tendencies}},
        {"step": "preferences", "input": {"summary": summary, "existing": list(existing.values())},
         "output": raw_p, "proposed": raw_p, "clamped": prefs},
        {"step": "self_image", "input": {"summary": summary, "short_memories": shorts,
                                         "existing": si_rows},
         "output": raw_s, "proposed": raw_s, "clamped": si},
    ]
    return SleepPlan(summary, prepared, new_t, t_changes, prefs, si, top_id, steps)


def commit(conn: Connection, cfg: Config, memory: Memory, p: SleepPlan, sim_time: str,
           real_time: str, sim_date: str | None = None) -> int:
    """Apply a plan. Call inside a transaction (the caller also saves the new tendencies).
    `sim_date` is the date the sleep began (the website's "last night")."""
    from cat.db import get_meta, set_meta
    sl = cfg.settings["sleep"]
    tick_for_memories = int(get_meta(conn, "sleep_tick_id", "0") or 0) or None
    for m in p.memories:
        memory.save(m, "long", tick_id=tick_for_memories)
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
    si = p.self_image
    if si["action"] in ("added", "revised"):
        if si["replaced"]:
            conn.execute("DELETE FROM self_image WHERE id=?", (si["replaced"]["id"],))
        conn.execute("INSERT INTO self_image(text,confidence,formed_day,updated_at) VALUES(?,?,?,?)",
                     (si["text"], si["confidence"], si["formed_day"], sim_time))
        for d in si["dropped"]:
            conn.execute("DELETE FROM self_image WHERE id=?", (d["id"],))
    cur = conn.execute(
        "INSERT INTO sleep_log(sim_date,summary,tick_id,tendency_changes,new_memories,"
        "new_preferences) VALUES(?,?,?,?,?,?)",
        ((sim_date or sim_time[:10]), p.summary, int(get_meta(conn, "sleep_tick_id", "0") or 0), dumps(p.tendency_changes), dumps([m["text"] for m in p.memories]),
         dumps([{"subject": x["subject"], "valence": x["valence"], "reason": x["reason"]}
                for x in p.preferences])))
    set_meta(conn, "sleep_pending", 0)
    set_meta(conn, "last_sleep_tick_id", p.last_tick_id)
    return cur.lastrowid

"""The sleep cycle (docs/rewrite-contracts.md §5.2-§5.6): seven stages, planned first, committed all at once.

    1 consolidate   the day's experiences -> a summary and what mattered          (LLM ranks, code bounds)
    2 core          thoughts it chose to keep -> at most core_per_night core memories   (LLM chooses, code caps)
    3 long_term     what mattered -> lasting memories linked to real experiences  (LLM writes, code checks links)
    4 learned       provisional discoveries -> confirmed, in its own words        (code facts, LLM words)
    5 preferences   code measures how things went by mood -> likes and dislikes   (code proposes, LLM may reject)
    6 develop       at most one small character change, checked by code          (LLM proposes, code decides)
    7 maintenance   merge near-duplicate memories, forget faded ones             (code)

`plan` only reads (it may run on its own connection beside the loop) and asks the sleep model; `commit` writes
the whole night in one transaction. A stage that fails leaves its part for the next night (stages 1-3 keep a
cursor over the experiences they have covered); the creature still sleeps.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np

from cat import clock, identity, knowledge, memories
from cat.config import CONFIG_DIR
from cat.content import Content
from cat.decision import load_prompt_file
from cat.llm import BackendError, LLMBackend, call_json

log = logging.getLogger("cat.sleepcycle")

PROMPTS_PATH = CONFIG_DIR / "prompts" / "sleep.yaml"
PROMPT_FIELDS = {
    "system": {"name", "nature", "boundaries"},
    "consolidate": {"day", "mood_low", "mood_high", "mood_end", "name", "lines", "max"},
    "core": {"name", "lines", "max"},
    "long_term": {"summary", "lines", "max", "name"},
    "learned": {"name", "lines"},
    "preferences": {"name", "lines"},
    "develop": {"name", "sheet", "behaviour", "preferences", "memories", "lines"},
    "conflict": {"name", "boundaries", "change"},
    "repeated": {"name", "day"},
}
CURSOR = "sleep.cursor"
OWN_KINDS = ("activity", "thought")


def load_prompts(path: Path | str = PROMPTS_PATH) -> dict[str, str]:
    return load_prompt_file(path, PROMPT_FIELDS)


@dataclass
class SleepPlan:
    started_at: str
    stages: dict[str, dict] = field(default_factory=dict)     # stage -> {status, input, output, problems}
    summary: str = ""
    mattered: list[dict] = field(default_factory=list)        # [{id, why}]
    core: list[dict] = field(default_factory=list)            # [{text, sources}]
    long_term: list[dict] = field(default_factory=list)       # [{text, importance, sources, embedding, merge_into}]
    learned: dict[str, str] = field(default_factory=dict)     # action id -> own words
    prefs: list[dict] = field(default_factory=list)           # rows to upsert
    prefs_drop: list[tuple[str, str]] = field(default_factory=list)
    proposal: dict | None = None
    verdicts: list[dict] = field(default_factory=list)
    accepted: bool = False
    merges: list[tuple[int, int]] = field(default_factory=list)   # (keep, drop)
    forget: list[int] = field(default_factory=list)
    cursor: int | None = None                                  # new experience cursor, if stages 1-3 covered the window
    summary_embedding: list[float] | None = None
    summary_repeated: bool = False                             # §6.4: close to an earlier night's summary, even after a retry


class StageError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _ask(backend: LLMBackend, model: str, task: str, system: str, user: str, schema: dict, parse, retries: int):
    def checked(raw: dict):
        try:
            return parse(raw)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise BackendError(f"unusable answer: {e}") from None
    return call_json(backend, task, model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                     schema, checked, retries)


def _lines(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items) if items else "- (nothing)"


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _exp_line(r: sqlite3.Row) -> str:
    t = f" (thought: {r['thought']})" if r["thought"] else ""
    return f"[e{r['id']}] {r['sim_time'][-5:]} {r['text']}{t}"


def _window(conn: sqlite3.Connection, cursor: int, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM experiences WHERE id > ? ORDER BY id", (cursor,)).fetchall()
    if len(rows) <= limit:
        return rows
    keep = sorted(rows, key=lambda r: (-r["remember"], -r["importance"], -r["id"]))[:limit]
    return sorted(keep, key=lambda r: r["id"])


def _own(r: sqlite3.Row) -> bool:
    """The creature's own behaviour or thought, not words that came from outside (a visitor's letter)."""
    if r["kind"] not in OWN_KINDS:
        return False
    data = json.loads(r["data"] or "{}")
    return not data.get("external") and (r["kind"] != "activity" or data.get("status") == "finished")


def _days_ago(now: str, days: float) -> str:
    return clock.fmt(clock.parse(now) - timedelta(days=days))


# ---------------------------------------------------------------- the stages

def plan(conn: sqlite3.Connection, content: Content, tuning: dict, backend: LLMBackend, prompts: dict,
         sheet: dict, now: str) -> SleepPlan:
    sl = tuning["sleep"]
    model = tuning["models"]["sleep_model"]
    retries = sl["retries"]
    core = sheet.get("core") or {}
    name = sheet.get("name", "it")
    system = prompts["system"].format(name=name, nature=core.get("nature", ""), boundaries=_lines(core.get("boundaries") or []))
    p = SleepPlan(started_at=now)

    def stage(key: str, fn):
        try:
            out = fn()
            p.stages[key] = {"status": "ok" if out is not None else "skipped", "output": out}
        except (BackendError, StageError) as e:
            log.warning("sleep stage %s failed: %s", key, e)
            p.stages[key] = {"status": "failed", "problems": [str(e)]}

    cursor = int((conn.execute("SELECT value FROM meta WHERE key = ?", (CURSOR,)).fetchone() or ["0"])[0])
    newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM experiences").fetchone()[0]
    window = _window(conn, cursor, sl["day_log_max"])
    by_id = {r["id"]: r for r in window}

    # 1 consolidate
    def consolidate():
        if not window:
            return None
        moods = [r[0] for r in conn.execute("SELECT value FROM body_changes WHERE var = 'mood' AND sim_time >= ? "
                                            "ORDER BY id", (window[0]["sim_time"],))] or [0.0]
        user = prompts["consolidate"].format(day=window[0]["sim_time"][:10], mood_low=f"{min(moods):+.2f}",
                                             mood_high=f"{max(moods):+.2f}", mood_end=f"{moods[-1]:+.2f}", name=name,
                                             lines=_lines([_exp_line(r) for r in window]), max=sl["consolidate_max"])
        schema = {"type": "object", "properties": {
            "summary": {"type": "string"},
            "mattered": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "integer"}, "why": {"type": "string"}}, "required": ["id", "why"]}}},
            "required": ["summary", "mattered"]}

        def parse(raw):
            if not isinstance(raw["summary"], str) or not raw["summary"].strip():
                raise ValueError("summary is empty")
            seen, out = set(), []
            for m in raw["mattered"]:
                i = int(m["id"])
                if i in by_id and i not in seen:
                    seen.add(i)
                    out.append({"id": i, "why": str(m.get("why", ""))})
            return {"summary": raw["summary"].strip(), "mattered": out[: sl["consolidate_max"]]}
        res = _ask(backend, model, "sleep_consolidate", system, user, schema, parse, retries)
        close = _close_summary(conn, tuning, backend, res["summary"])
        if close is not None:                                  # the same entry as an earlier night: once more
            day, _ = close
            again = _ask(backend, model, "sleep_consolidate", system,
                         user + "\n\n" + prompts["repeated"].format(name=name, day=day), schema, parse, retries)
            if _close_summary(conn, tuning, backend, again["summary"]) is None:
                res = again
            else:
                p.summary_repeated = True
        p.summary, p.mattered = res["summary"], res["mattered"]
        p.summary_embedding = memories.embed(backend, tuning["models"]["embed_model"], [p.summary])[0]
        res["repeated"] = p.summary_repeated
        return res
    stage("consolidate", consolidate)

    # 2 core
    def core_stage():
        cands = [r for r in window if r["kind"] == "thought" and json.loads(r["data"] or "{}").get("preserve")]
        if not cands or sl["core_per_night"] == 0:
            return None
        ids = {r["id"] for r in cands}
        user = prompts["core"].format(name=name, lines=_lines([_exp_line(r) for r in cands]), max=sl["core_per_night"])
        schema = {"type": "object", "properties": {"keep": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["id", "text"]}}}, "required": ["keep"]}

        def parse(raw):
            out = []
            for k in raw["keep"]:
                i = int(k["id"])
                if i in ids and all(i not in c["sources"] for c in out):
                    text = str(k.get("text") or "").strip() or by_id[i]["thought"] or by_id[i]["text"]
                    out.append({"text": text, "sources": [i]})
            return out[: sl["core_per_night"]]
        p.core = _ask(backend, model, "sleep_core", system, user, schema, parse, retries)
        return p.core
    stage("core", core_stage)

    # 3 long-term
    def long_term():
        if p.stages["consolidate"]["status"] == "failed":
            raise StageError("needs the consolidation")
        if not p.mattered or sl["long_term_per_night"] == 0:
            return None
        lines = [f"{_exp_line(by_id[m['id']])} -- {m['why']}" for m in p.mattered]
        user = prompts["long_term"].format(summary=p.summary, lines=_lines(lines), max=sl["long_term_per_night"], name=name)
        schema = {"type": "object", "properties": {"memories": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "importance": {"type": "number"},
            "sources": {"type": "array", "items": {"type": "integer"}}}, "required": ["text", "importance", "sources"]}}},
            "required": ["memories"]}

        def parse(raw):
            out = []
            for m in raw["memories"]:
                src = sorted({int(i) for i in m["sources"] if int(i) in by_id})
                text = str(m["text"]).strip()
                if src and text:
                    imp = float(m["importance"]) if _num(m["importance"]) else 0.5
                    out.append({"text": text, "importance": min(1.0, max(0.0, imp)), "sources": src})
            return out[: sl["long_term_per_night"]]
        mems = _ask(backend, model, "sleep_long_term", system, user, schema, parse, retries)
        vecs = memories.embed(backend, tuning["models"]["embed_model"], [m["text"] for m in mems])
        existing = [m for m in memories.all_memories(conn, "long") if m.embedding is not None]
        for m, v in zip(mems, vecs):
            m["embedding"] = v
            m["merge_into"] = None
            if v is not None:
                vec = np.asarray(v, dtype=np.float32)
                best = max(existing, key=lambda e: memories.cosine(e.embedding, vec), default=None)
                if best is not None and memories.cosine(best.embedding, vec) > tuning["memory"]["dup_threshold"]:
                    m["merge_into"] = best.id
        p.long_term = mems
        return [{k: v for k, v in m.items() if k != "embedding"} for m in mems]
    stage("long_term", long_term)
    if window and all(p.stages[s]["status"] in ("ok", "skipped") for s in ("consolidate", "core", "long_term")):
        p.cursor = newest            # what the night left out was the least important; it is not read again

    # 4 learned actions
    def learned():
        prov = [k for k in knowledge.all_known(conn).values() if k.provisional and k.action_id in content.actions]
        if not prov:
            return None
        fallback = {}
        lines = []
        for k in prov:
            a = content.actions[k.action_id]
            where = content.locations[k.location].name if k.location in content.locations else "anywhere"
            fallback[a.id] = f"I can {a.name}" + (f" at {where}." if k.location else ".")
            lines.append(f"[a:{a.id}] {a.name}, at {where}, found by {k.discovered_by} on {k.discovered_at}: {a.description}")
        schema = {"type": "object", "properties": {"words": {"type": "array", "items": {"type": "object", "properties": {
            "action": {"type": "string"}, "words": {"type": "string"}}, "required": ["action", "words"]}}},
            "required": ["words"]}

        def parse(raw):
            got = {}
            for w in raw["words"]:
                aid = str(w["action"]).removeprefix("a:")
                if aid in fallback and str(w["words"]).strip():
                    got[aid] = str(w["words"]).strip()[:200]
            return got
        try:
            got = _ask(backend, model, "sleep_learned", system, prompts["learned"].format(name=name, lines=_lines(lines)),
                       schema, parse, retries)
        except BackendError:
            got = {}                              # the facts are code's: confirm them in plain words anyway
        p.learned = {a: got.get(a, fallback[a]) for a in fallback}
        return p.learned
    stage("learned", learned)

    # 5 preferences
    stage("preferences", lambda: _preferences(conn, content, tuning, backend, prompts, system, name, now, p))

    # 6 character development
    stage("develop", lambda: _develop(conn, content, tuning, backend, prompts, system, sheet, now, p))

    # 7 maintenance
    stage("maintenance", lambda: _maintenance(conn, tuning, now, p))
    return p


def _preferences(conn, content, tuning, backend, prompts, system, name, now, p: SleepPlan):
    sl = tuning["sleep"]
    since = _days_ago(now, sl["pref_window_days"])
    rows = conn.execute("SELECT * FROM experiences WHERE kind = 'activity' AND sim_time >= ?", (since,)).fetchall()
    samples: dict[tuple[str, str], list[tuple[float, int]]] = {}
    all_moods = []
    for r in rows:
        data = json.loads(r["data"] or "{}")
        if data.get("status") != "finished" or r["action_id"] not in content.actions:
            continue
        a = content.actions[r["action_id"]]
        if a.builtin:                     # moving, inspecting, sleeping are means, not things to like or dislike
            continue
        md = float(data.get("mood_base", 0.0))
        all_moods.append(md)
        for key in memories.subjects_of(a, r["location"]):
            samples.setdefault(key, []).append((md, r["id"]))
    existing = memories.preferences(conn)
    base = sum(all_moods) / len(all_moods) if all_moods else 0.0
    cands = []
    for key, xs in samples.items():
        if len(xs) < sl["pref_min_samples"]:
            continue
        signal = sum(m for m, _ in xs) / len(xs) - base
        if abs(signal) < sl["pref_threshold"] and key not in existing:
            continue
        target = max(-1.0, min(1.0, signal / (5 * sl["pref_threshold"]))) if sl["pref_threshold"] else 0.0
        conf_target = min(1.0, len(xs) / (3 * sl["pref_min_samples"]))
        old = existing.get(key)
        v0, c0 = (old.valence, old.confidence) if old else (0.0, 0.0)
        step = sl["pref_step"]
        v = v0 + max(-step, min(step, target - v0))
        c = c0 + max(-step, min(step, conf_target - c0))
        cands.append({"subject_kind": key[0], "subject_id": key[1], "valence": round(v, 4), "confidence": round(c, 4),
                      "signal": round(signal, 4), "count": len(xs), "evidence": [i for _, i in xs][-20:],
                      "words": old.words if old else None, "new": old is None})
    seen = {(c["subject_kind"], c["subject_id"]) for c in cands}
    for key, old in existing.items():
        if key in seen:
            continue
        v = old.valence - max(-sl["pref_decay"], min(sl["pref_decay"], old.valence))
        if abs(v) < 0.01:
            p.prefs_drop.append(key)
        else:
            p.prefs.append({"subject_kind": key[0], "subject_id": key[1], "valence": round(v, 4),
                            "confidence": old.confidence, "words": old.words, "evidence": old.evidence})
    if not cands:
        return {"decayed": len(p.prefs), "dropped": len(p.prefs_drop)} if (p.prefs or p.prefs_drop) else None

    def label(c):
        if c["subject_kind"] == "location":
            return content.locations[c["subject_id"]].name if c["subject_id"] in content.locations else c["subject_id"]
        if c["subject_kind"] == "action":
            return content.actions[c["subject_id"]].name if c["subject_id"] in content.actions else c["subject_id"]
        return f"anything to do with {c['subject_id']}"
    lines = [f"[p:{c['subject_kind']}:{c['subject_id']}] {label(c)}: "
             f"{'better' if c['signal'] > 0 else 'worse'} than usual ({c['count']} times)" for c in cands]
    schema = {"type": "object", "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
        "subject": {"type": "string"}, "keep": {"type": "boolean"}, "words": {"type": "string"}},
        "required": ["subject", "keep", "words"]}}}, "required": ["verdicts"]}

    def parse(raw):
        return {str(v["subject"]).removeprefix("p:"): (bool(v["keep"]), str(v.get("words") or "").strip())
                for v in raw["verdicts"]}
    try:
        verdicts = _ask(backend, tuning["models"]["sleep_model"], "sleep_preferences", system,
                        prompts["preferences"].format(name=name, lines=_lines(lines)), schema, parse, sl["retries"])
    except BackendError:
        verdicts = {}                                         # code's measure stands; it may only reject new ones
    for c in cands:
        keep, words = verdicts.get(f"{c['subject_kind']}:{c['subject_id']}", (True, ""))
        if c["new"] and not keep:
            continue
        if words:
            c["words"] = words[:200]
        p.prefs.append({k: c[k] for k in ("subject_kind", "subject_id", "valence", "confidence", "words", "evidence")})
    return {"candidates": cands, "verdicts": {k: list(v) for k, v in verdicts.items()}}


def _develop(conn, content, tuning, backend, prompts, system, sheet, now, p: SleepPlan):
    sl = tuning["sleep"]
    since = _days_ago(now, sl["dev_min_days"] * 2)
    rows = [r for r in conn.execute("SELECT * FROM experiences WHERE sim_time >= ? ORDER BY id", (since,)) if _own(r)]
    if len(rows) < sl["dev_min_evidence"]:
        return None
    fam: dict[str, int] = {}
    loc: dict[str, int] = {}
    for r in rows:
        if r["action_id"] in content.actions:
            f = content.actions[r["action_id"]].family
            fam[f] = fam.get(f, 0) + 1
        loc[r["location"]] = loc.get(r["location"], 0) + 1
    behaviour = _lines([f"{k}: {v} times" for k, v in sorted(fam.items(), key=lambda kv: -kv[1])]
                       + [f"at {content.locations[k].name if k in content.locations else k}: {v} times"
                          for k, v in sorted(loc.items(), key=lambda kv: -kv[1])])
    prefs = ", ".join(f"{q.words or q.subject_id} ({q.strength:+.2f})" for q in memories.preferences(conn).values()) or "none yet"
    mems = _lines([m.text for m in memories.all_memories(conn)][-10:])
    shown = rows[-40:]
    traits = ", ".join(f"{k} {v:.2f}" for k, v in (sheet.get("traits") or {}).items())
    tend = ", ".join(f"{k} {v:.2f}" for k, v in (sheet.get("tendencies") or {}).items()) or "none"
    sheet_text = f"traits: {traits}\ntendencies: {tend}\nbackground: {sheet.get('background', '')}"
    schema = {"type": "object", "properties": {
        "change": {"type": "boolean"},
        "kind": {"type": "string", "enum": list(identity.CHANGE_KINDS)},
        "target": {"type": "string"}, "value": {"type": "string"}, "rationale": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "integer"}}},
        "required": ["change", "kind", "target", "value", "rationale", "evidence"]}

    def parse(raw):
        if not raw["change"]:
            return None
        return {"kind": str(raw["kind"]), "target": str(raw["target"]).strip(), "value": str(raw["value"]).strip(),
                "rationale": str(raw["rationale"]).strip(), "evidence": [int(i) for i in raw["evidence"]]}
    user = prompts["develop"].format(name=sheet.get("name"), sheet=sheet_text, behaviour=behaviour, preferences=prefs,
                                     memories=mems, lines=_lines([_exp_line(r) for r in shown]))
    proposal = _ask(backend, tuning["models"]["sleep_model"], "sleep_develop", system, user, schema, parse, sl["retries"])
    if proposal is None:
        return {"proposal": None}
    p.proposal = proposal
    p.verdicts, p.accepted = check_proposal(conn, tuning, backend, prompts, sheet, proposal, now)
    return {"proposal": proposal, "verdicts": p.verdicts, "accepted": p.accepted}


def check_proposal(conn, tuning, backend, prompts, sheet, proposal: dict, now: str) -> tuple[list[dict], bool]:
    """§5.6: evidence, behaviour, conflict, size and cooldown. Returns (verdicts, accepted); may adjust the value."""
    sl = tuning["sleep"]
    verdicts: list[dict] = []

    def verdict(check, ok, detail=""):
        verdicts.append({"check": check, "ok": bool(ok), "detail": detail})
        return ok

    ids = sorted(set(proposal.get("evidence") or []))
    rows = [conn.execute("SELECT * FROM experiences WHERE id = ?", (i,)).fetchone() for i in ids]
    rows = [r for r in rows if r is not None]
    own = [r for r in rows if _own(r)]
    days = {r["sim_time"][:10] for r in own}
    ok = verdict("evidence", len(own) >= sl["dev_min_evidence"] and len(days) >= sl["dev_min_days"],
                 f"{len(own)} of its own experiences over {len(days)} days "
                 f"(needs {sl['dev_min_evidence']} over {sl['dev_min_days']}); {len(rows) - len(own)} cited were not its own")
    acts = [r for r in own if r["kind"] == "activity"]
    ok = verdict("behaviour", len(acts) * 2 >= max(1, len(own)), f"{len(acts)} of {len(own)} are things it did") and ok
    text = " ".join(str(proposal.get(k, "")) for k in ("target", "value", "rationale")).lower()
    bad = [w for w in sl["dev_forbidden_words"] if w.lower() in text.split() or f" {w.lower()} " in f" {text} "]
    conflict_ok = verdict("conflict_words", not bad, f"forbidden words: {bad}" if bad else "")
    kind = proposal.get("kind")
    change = {"kind": kind, "target": proposal.get("target")}
    section = "traits" if str(kind).endswith("_trait") else "tendencies"
    current = (sheet.get(section) or {}).get(proposal.get("target"))
    if kind in ("add_trait", "add_tendency"):
        change["value"] = sl["dev_new_strength"]
    elif kind in ("adjust_trait", "adjust_tendency"):
        try:
            want = float(proposal.get("value"))
        except (TypeError, ValueError):
            want = current if current is not None else 0.0
        if current is not None:
            change["value"] = round(current + max(-sl["dev_max_step"], min(sl["dev_max_step"], want - current)), 4)
    elif kind == "background_note":
        change["value"] = str(proposal.get("value", ""))[:200]
    try:
        identity.apply_change(sheet, change)
        size_ok = verdict("size", True, f"applied as {change}")
    except identity.IdentityError as e:
        size_ok = verdict("size", False, str(e))
    since = _days_ago(now, sl["dev_cooldown_days"])
    recent = [v for v in identity.history(conn) if v.created_at >= since and v.change
              and v.change.get("target") == change.get("target") and kind != "background_note"]
    cool_ok = verdict("cooldown", not recent, f"changed {len(recent)} time(s) in the last {sl['dev_cooldown_days']} days")
    ok = ok and conflict_ok and size_ok and cool_ok
    if ok:
        core = sheet.get("core") or {}
        user = prompts["conflict"].format(name=sheet.get("name"), boundaries=_lines(core.get("boundaries") or []),
                                          change=json.dumps(change, ensure_ascii=False))
        schema = {"type": "object", "properties": {"conflict": {"type": "boolean"}, "why": {"type": "string"}},
                  "required": ["conflict", "why"]}
        try:
            res = _ask(backend, tuning["models"]["sleep_model"], "sleep_conflict",
                       prompts["system"].format(name=sheet.get("name"), nature=core.get("nature", ""),
                                                boundaries=_lines(core.get("boundaries") or [])),
                       user, schema, lambda r: (bool(r["conflict"]), str(r.get("why", ""))), sl["retries"])
            ok = verdict("conflict_check", not res[0], res[1])
        except BackendError as e:
            ok = verdict("conflict_check", False, f"could not be checked: {e}")
    proposal["applied"] = change
    return verdicts, ok


def _close_summary(conn, tuning, backend, summary: str) -> tuple[str, float] | None:
    """(day, similarity) of an earlier night's summary this one is too close to, or None."""
    rp = tuning["repetition"]
    if not rp["reflection_window"]:
        return None
    rows = conn.execute("SELECT started_at, summary_embedding FROM sleep_runs WHERE summary_embedding IS NOT NULL "
                        "ORDER BY id DESC LIMIT ?", (rp["reflection_window"],)).fetchall()
    if not rows:
        return None
    vec = memories.embed(backend, tuning["models"]["embed_model"], [summary])[0]
    if vec is None:
        return None
    v = np.asarray(vec, dtype=np.float32)
    for r in rows:
        sim = memories.cosine(memories.from_blob(r["summary_embedding"]), v)
        if sim > rp["reflection_dup_threshold"]:
            return r["started_at"][:10], sim
    return None


def repeat_streak(conn: sqlite3.Connection) -> int:
    """How many nights in a row (up to the latest) repeated themselves."""
    n = 0
    for r in conn.execute("SELECT repeated FROM sleep_runs ORDER BY id DESC"):
        if not r[0]:
            break
        n += 1
    return n


def _maintenance(conn, tuning, now, p: SleepPlan):
    m = tuning["memory"]
    longs = memories.all_memories(conn, "long")
    protected: set[int] = set()
    for v in identity.history(conn):
        protected |= set(v.evidence)
    dropped: set[int] = set()
    for i, a in enumerate(longs):
        if a.id in dropped or a.embedding is None:
            continue
        for b in longs[i + 1:]:
            if b.id in dropped or b.embedding is None:
                continue
            if memories.cosine(a.embedding, b.embedding) > m["dup_threshold"]:
                keep, drop = (a, b) if a.importance >= b.importance else (b, a)
                p.merges.append((keep.id, drop.id))
                dropped.add(drop.id)
                if drop.id == a.id:
                    break
    for x in longs:
        if x.id in dropped or set(x.sources) & protected:
            continue
        if memories.retention(x, now, m) < m["forget_below"]:
            p.forget.append(x.id)
    return {"merges": p.merges, "forget": p.forget} if (p.merges or p.forget) else None


# ---------------------------------------------------------------- commit

def commit(conn: sqlite3.Connection, p: SleepPlan, now: str) -> int:
    """Write the whole night (inside the caller's transaction). Returns the sleep_runs id."""
    for c in p.core:
        conn.execute("INSERT INTO memories(kind, text, importance, created_at, sources) VALUES ('core', ?, 1.0, ?, ?)",
                     (c["text"], now, json.dumps(c["sources"])))
    for m in p.long_term:
        if m.get("merge_into"):
            row = conn.execute("SELECT importance, sources FROM memories WHERE id = ?", (m["merge_into"],)).fetchone()
            if row is not None:
                src = sorted(set(json.loads(row["sources"])) | set(m["sources"]))
                conn.execute("UPDATE memories SET importance = ?, sources = ? WHERE id = ?",
                             (max(row["importance"], m["importance"]), json.dumps(src), m["merge_into"]))
                continue
        conn.execute("INSERT INTO memories(kind, text, importance, embedding, created_at, sources) "
                     "VALUES ('long', ?, ?, ?, ?, ?)",
                     (m["text"], m["importance"], memories.to_blob(m.get("embedding")), now, json.dumps(m["sources"])))
    for aid, words in p.learned.items():
        conn.execute("UPDATE knowledge SET provisional = 0, own_words = ? WHERE action_id = ?", (words, aid))
    for pr in p.prefs:
        conn.execute("INSERT INTO preferences(subject_kind, subject_id, valence, confidence, words, evidence, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(subject_kind, subject_id) DO UPDATE SET "
                     "valence = excluded.valence, confidence = excluded.confidence, words = excluded.words, "
                     "evidence = excluded.evidence, updated_at = excluded.updated_at",
                     (pr["subject_kind"], pr["subject_id"], pr["valence"], pr["confidence"], pr["words"],
                      json.dumps(pr["evidence"]), now))
    for kind, sid in p.prefs_drop:
        conn.execute("DELETE FROM preferences WHERE subject_kind = ? AND subject_id = ?", (kind, sid))
    if p.proposal is not None:
        version = None
        if p.accepted:
            v = identity.commit_change(conn, p.proposal["applied"], f"sleep {now[:10]}: {p.proposal.get('rationale', '')}"[:300],
                                       p.proposal.get("evidence") or [], now)
            version = v.version
        conn.execute("INSERT INTO dev_proposals(sim_time, proposal, verdicts, accepted, version) VALUES (?, ?, ?, ?, ?)",
                     (now, json.dumps(p.proposal, ensure_ascii=False), json.dumps(p.verdicts), int(p.accepted), version))
    for keep, drop in p.merges:
        a = conn.execute("SELECT sources, recall_count FROM memories WHERE id = ?", (keep,)).fetchone()
        b = conn.execute("SELECT sources, recall_count FROM memories WHERE id = ?", (drop,)).fetchone()
        if a is None or b is None:
            continue
        src = sorted(set(json.loads(a["sources"])) | set(json.loads(b["sources"])))
        conn.execute("UPDATE memories SET sources = ?, recall_count = ? WHERE id = ?",
                     (json.dumps(src), a["recall_count"] + b["recall_count"], keep))
        conn.execute("DELETE FROM memories WHERE id = ?", (drop,))
    if p.forget:
        conn.executemany("DELETE FROM memories WHERE id = ? AND kind = 'long'", [(i,) for i in p.forget])
    if p.cursor is not None:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (CURSOR, str(p.cursor)))
    status = "failed" if any(s["status"] == "failed" for s in p.stages.values()) else "ok"
    cur = conn.execute("INSERT INTO sleep_runs(started_at, finished_at, status, stages, summary, summary_embedding, "
                       "repeated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (p.started_at, now, status, json.dumps(p.stages, ensure_ascii=False, default=str), p.summary,
                        memories.to_blob(p.summary_embedding), int(p.summary_repeated)))
    return cur.lastrowid

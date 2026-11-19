"""Queries behind the dev dashboard pages and /api/dev/* (read-only on cat.db)."""
from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator

import httpx

from cat import character, clock, db, devqueue
from cat.config import Config


# ---------- helpers ----------
def jload(v: Any, default: Any = None) -> Any:
    if v is None or v == "":
        return default
    try:
        return json.loads(v) if isinstance(v, (str, bytes)) else v
    except (ValueError, TypeError):
        return default


def rows(conn: sqlite3.Connection, sql: str, *args: Any) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


def one(conn: sqlite3.Connection, sql: str, *args: Any) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] if lo == hi else v[lo] + (v[hi] - v[lo]) * (k - lo)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def r1(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)


@contextmanager
def game_db(cfg: Config) -> Iterator[sqlite3.Connection | None]:
    """A read-only connection to cat.db (None while the engine has never run). The dashboard can
    not change game state: the connection is opened mode=ro."""
    conn = None
    try:
        if cfg.db_path.exists():
            conn = db.connect(cfg.db_path, readonly=True)
            conn.execute("SELECT 1 FROM ticks LIMIT 1")
    except sqlite3.OperationalError:
        if conn is not None:
            conn.close()
        conn = None
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


@contextmanager
def dev_db(cfg: Config) -> Iterator[devqueue.DevQueue]:
    q = devqueue.DevQueue(cfg.dev_db_path)
    try:
        yield q
    finally:
        q.close()


# ---------- Ollama probe ----------
_probe_cache: dict[str, tuple[float, dict]] = {}


def ollama_info(cfg: Config, ttl: float = 3.0) -> dict:
    """Reachability, installed model versions (/api/tags) and loaded models + VRAM (/api/ps)."""
    host = cfg.settings["ollama_host"].rstrip("/")
    hit = _probe_cache.get(host)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    timeout = cfg.settings["health_timeout_seconds"]
    out: dict[str, Any] = {"host": host, "reachable": False, "error": None, "installed": [], "loaded": []}
    try:
        with httpx.Client(timeout=timeout) as c:
            tags = c.get(f"{host}/api/tags")
            tags.raise_for_status()
            out["reachable"] = True
            for m in tags.json().get("models", []):
                d = m.get("details") or {}
                out["installed"].append({"name": m.get("name"), "digest": (m.get("digest") or "")[:12],
                                         "size_mb": round((m.get("size") or 0) / 1e6), "modified": m.get("modified_at"),
                                         "family": d.get("family"), "parameters": d.get("parameter_size"),
                                         "quantization": d.get("quantization_level")})
            try:
                ps = c.get(f"{host}/api/ps")
                ps.raise_for_status()
                for m in ps.json().get("models", []):
                    size, vram = m.get("size") or 0, m.get("size_vram") or 0
                    out["loaded"].append({"name": m.get("name"), "size_mb": round(size / 1e6),
                                          "vram_mb": round(vram / 1e6),
                                          "gpu_pct": round(100 * vram / size) if size else None,
                                          "expires": m.get("expires_at")})
            except (httpx.HTTPError, ValueError) as e:
                out["ps_error"] = str(e)
    except (httpx.HTTPError, ValueError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    _probe_cache[host] = (time.time(), out)
    return out


def web_health(cfg: Config) -> tuple[bool, str]:
    url = cfg.settings["devdash"]["web_url"].rstrip("/") + "/healthz"
    try:
        r = httpx.get(url, timeout=cfg.settings["health_timeout_seconds"])
        return r.status_code == 200, f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}"


# ---------- Overview ----------
def _cutoff(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).isoformat(timespec="milliseconds")


def stats(conn: sqlite3.Connection | None, cfg: Config, hours: float | None = None) -> dict:
    hours = hours or cfg.settings["devdash"]["stats_hours"]
    out: dict[str, Any] = {"hours": hours, "ticks": 0, "doze": 0, "errors": 0, "invalid_json_rate": None,
                           "llm_attempts": 0, "rejected_rate": None, "effects": 0,
                           "brain": {"n": 0, "mean_ms": None, "p95_ms": None},
                           "narrator": {"n": 0, "mean_ms": None, "p95_ms": None},
                           "tokens_per_tick": None}
    if conn is None:
        return out
    since = _cutoff(hours)
    tr = rows(conn, "SELECT status, COUNT(*) n FROM tick_traces WHERE started_at>=? GROUP BY status", since)
    by = {r["status"]: r["n"] for r in tr}
    out["ticks"] = sum(by.values())
    out["doze"], out["errors"] = by.get("doze", 0), by.get("error", 0)
    llm = rows(conn, "SELECT kind, valid_json, total_ms, prompt_tokens, completion_tokens FROM llm_calls WHERE started_at>=?", since)
    out["llm_attempts"] = len(llm)
    if llm:
        out["invalid_json_rate"] = sum(1 for c in llm if not c["valid_json"]) / len(llm)
    for kind in ("brain", "narrator"):
        ms = [c["total_ms"] for c in llm if c["kind"] == kind and c["total_ms"] is not None]
        out[kind] = {"n": len(ms), "mean_ms": r1(mean(ms)), "p95_ms": r1(percentile(ms, 95))}
    toks = sum((c["prompt_tokens"] or 0) + (c["completion_tokens"] or 0) for c in llm)
    if out["ticks"]:
        out["tokens_per_tick"] = round(toks / out["ticks"])
    eff = one(conn, "SELECT COUNT(*) n, SUM(CASE WHEN accepted=0 THEN 1 ELSE 0 END) rej FROM effect_logs "
                    "WHERE tick_id IN (SELECT tick_id FROM tick_traces WHERE started_at>=?)", since)
    out["effects"] = eff["n"] or 0
    if eff["n"]:
        out["rejected_rate"] = (eff["rej"] or 0) / eff["n"]
    return out


def engine_view(conn: sqlite3.Connection | None, cfg: Config, q: devqueue.DevQueue) -> dict:
    st = q.get_status()
    interval = cfg.settings["tick_interval_seconds"]
    hb = st.get("heartbeat")
    age = (time.time() - hb) if hb else None
    stale = age is None or age > max(30, 3 * (st.get("interval_seconds") or interval), 3 * cfg.settings["dev_poll_seconds"])
    state = st.get("state") or "unknown"
    out = {"known": bool(st), "state": "not running" if (not st or (stale and state != "stopped")) else state,
           "published_state": state, "paused": bool(st.get("paused")), "step_pending": bool(st.get("step_pending")),
           "heartbeat_age_s": r1(age), "stale": stale, "interval_s": st.get("interval_seconds", interval),
           "interval_override": st.get("interval_override"), "interval_override_ticks": st.get("interval_override_ticks"),
           "pid": st.get("pid"), "downtime_policy": st.get("downtime_policy", cfg.settings["downtime_policy"]),
           "trace_level": st.get("trace_level", cfg.settings["trace_level"]),
           "injected_queued": st.get("injected_queued", 0),
           "current_tick": None, "sim_time": None, "sim_label": None, "last_tick_ms": st.get("last_tick_ms"),
           "brain_online": None, "area": None, "asleep": None}
    if conn is not None:
        s = db.get_state(conn)
        out["current_tick"] = (one(conn, "SELECT MAX(id) m FROM ticks") or {}).get("m")
        if s:
            first = clock.parse(db.get_meta(conn, "first_sim_time") or cfg.settings["sim_start"])
            now = clock.parse(s["sim_time"])
            out.update(sim_time=s["sim_time"], sim_label=f"{clock.day_label(first, now)} · {clock.time_label(now)}",
                       brain_online=bool(s["brain_online"]), area=cfg.world["areas"].get(s["area"], {}).get("name", s["area"]),
                       asleep=bool(s["is_asleep"]))
        last = one(conn, "SELECT total_ms FROM tick_traces WHERE tick_id>0 ORDER BY tick_id DESC LIMIT 1")
        if last and out["last_tick_ms"] is None:
            out["last_tick_ms"] = last["total_ms"]
    return out


def services_view(cfg: Config, engine: dict, ollama: dict, check_web: bool = True) -> list[dict]:
    from cat.healthcheck import heartbeat_age, engine_limit
    age = heartbeat_age(cfg.heartbeat_path)
    limit = engine_limit(cfg)
    out = [{"name": "engine", "ok": age is not None and age <= limit,
            "detail": "no heartbeat file" if age is None else f"heartbeat {age:.0f}s ago (limit {limit:.0f}s)"},
           {"name": "ollama", "ok": ollama["reachable"], "detail": ollama["error"] or f"{len(ollama['installed'])} models installed, {len(ollama['loaded'])} loaded"}]
    if check_web:
        ok, detail = web_health(cfg)
        out.append({"name": "web", "ok": ok, "detail": detail})
    out.append({"name": "devdash", "ok": True, "detail": "this page"})
    return out


def overview(cfg: Config) -> dict:
    with game_db(cfg) as conn, dev_db(cfg) as q:
        eng = engine_view(conn, cfg, q)
        oll = ollama_info(cfg)
        return {"engine": eng, "ollama": oll, "services": services_view(cfg, eng, oll),
                "stats": stats(conn, cfg), "commands": q.recent(10),
                "models": {k: cfg.settings[k] for k in ("brain_model", "narrator_model", "sleep_model", "embed_model")},
                "refresh_s": cfg.settings["devdash"]["refresh_seconds"]}


# ---------- Ticks list ----------
TICK_CTE = """
WITH t AS (
  SELECT k.id AS id, k.kind AS kind, k.sim_time AS sim_time, k.area AS area, k.action AS action,
         k.outcome AS outcome, COALESCE(tt.status, '-') AS status, tt.total_ms AS total_ms,
         k.real_time AS ts,
         (k.rejected_changes IS NOT NULL AND k.rejected_changes NOT IN ('[]', '')) AS has_rej,
         (COALESCE(tt.status, '') = 'error' OR EXISTS(SELECT 1 FROM llm_calls c WHERE c.tick_id = k.id AND c.error IS NOT NULL)) AS has_err
  FROM ticks k LEFT JOIN tick_traces tt ON tt.tick_id = k.id
  UNION ALL
  SELECT tt.tick_id, 'error', NULL, NULL, NULL, tt.error, 'error', tt.total_ms, tt.started_at, 0, 1
  FROM tick_traces tt WHERE tt.tick_id < 0
)
"""


def ticks_list(cfg: Config, f: dict, page: int = 1, limit: int | None = None) -> dict:
    limit = min(limit or cfg.settings["devdash"]["page_size"], 500)
    page = max(1, page)
    where, args = [], []
    if f.get("status"):
        where.append("status = ?"); args.append(f["status"])
    if f.get("kind"):
        where.append("kind = ?"); args.append(f["kind"])
    if f.get("area"):
        where.append("area = ?"); args.append(f["area"])
    if f.get("q"):
        where.append("(action LIKE ? OR outcome LIKE ?)"); args += [f"%{f['q']}%"] * 2
    if f.get("rejections"):
        where.append("has_rej = 1")
    if f.get("errors"):
        where.append("has_err = 1")
    if f.get("slow_ms") is not None:
        where.append("total_ms > ?"); args.append(f["slow_ms"])
    w = (" WHERE " + " AND ".join(where)) if where else ""
    with game_db(cfg) as conn:
        if conn is None:
            return {"items": [], "total": 0, "page": page, "limit": limit, "pages": 1}
        total = one(conn, TICK_CTE + f"SELECT COUNT(*) n FROM t{w}", *args)["n"]
        items = rows(conn, TICK_CTE + f"SELECT * FROM t{w} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                     *args, limit, (page - 1) * limit)
        areas = [r["area"] for r in conn.execute("SELECT DISTINCT area FROM ticks WHERE area IS NOT NULL ORDER BY area")]
    for it in items:
        it["area_name"] = cfg.world["areas"].get(it["area"], {}).get("name", it["area"])
        it["total_ms"] = r1(it["total_ms"])
    return {"items": items, "total": total, "page": page, "limit": limit,
            "pages": max(1, math.ceil(total / limit)), "areas": areas}


# ---------- Tick detail ----------
STAGE_ORDER = ["world", "recall", "brain", "narrator", "apply", "record", "sleep"]


def _llm_view(c: dict) -> dict:
    c["options"] = jload(c["options"], {})
    c["schema_obj"] = jload(c["schema"])
    c["parsed_obj"] = jload(c["parsed"])
    c["status"] = "error" if c["error"] else ("ok" if c["valid_json"] else "invalid JSON")
    return c


def tick_detail(cfg: Config, tick_id: int) -> dict | None:
    with game_db(cfg) as conn:
        if conn is None:
            return None
        tick = one(conn, "SELECT * FROM ticks WHERE id=?", tick_id)
        trace = one(conn, "SELECT * FROM tick_traces WHERE tick_id=?", tick_id)
        if tick is None and trace is None:
            return None
        d: dict[str, Any] = {"id": tick_id, "tick": tick, "trace": trace}
        first = clock.parse(db.get_meta(conn, "first_sim_time") or cfg.settings["sim_start"])
        d["prev_id"] = (one(conn, "SELECT MAX(id) m FROM ticks WHERE id<? AND id>0", tick_id) or {}).get("m") if tick_id > 0 else None
        d["next_id"] = (one(conn, "SELECT MIN(id) m FROM ticks WHERE id>?", tick_id) or {}).get("m") if tick_id > 0 else None
        stages = jload(trace["stage_timings"], {}) if trace else {}
        total = (trace["total_ms"] if trace else 0) or 0
        d["stages"] = [{"name": k, "ms": stages[k], "pct": round(100 * stages[k] / total, 1) if total else 0}
                       for k in STAGE_ORDER + sorted(set(stages) - set(STAGE_ORDER)) if k in stages]
        d["stages_other_ms"] = round(max(0, total - sum(stages.values())), 1)
        if tick:
            t0 = clock.parse(tick["sim_time"])
            t1 = clock.parse(clock.advance(tick["sim_time"], tick["duration"] or 0))
            d["clock"] = {"from": tick["sim_time"], "to": clock.fmt(t1), "minutes": tick["duration"],
                          "label": f"{clock.day_label(first, t0)} · {clock.time_label(t0)} → {clock.time_label(t1)}",
                          "area": cfg.world["areas"].get(tick["area"], {}).get("name", tick["area"])}
            for k in ("gains", "changes", "rejected_changes", "needs_after", "mood_after"):
                tick[k] = jload(tick[k], {} if k in ("gains", "needs_after", "mood_after") else [])
            na = tick["needs_after"]
            d["energy"] = character.energy(na, cfg) if na and set(na) >= set(cfg.character["needs"]) else None
            d["energy_terms"] = [{"need": k, "weight": n["energy_weight"], "value": round(na.get(k, 0), 2)}
                                 for k, n in cfg.character["needs"].items() if n.get("energy_weight")] if na else []
        d["events"] = rows(conn, "SELECT * FROM events WHERE tick_id=? ORDER BY id", tick_id)
        d["event_rolls"] = rows(conn, "SELECT * FROM event_rolls WHERE tick_id=? ORDER BY id", tick_id)
        d["llm"] = [_llm_view(c) for c in rows(conn, "SELECT * FROM llm_calls WHERE tick_id=? ORDER BY id", tick_id)]
        d["embeds"] = rows(conn, "SELECT * FROM embed_calls WHERE tick_id=? ORDER BY id", tick_id)
        rl = one(conn, "SELECT * FROM recall_logs WHERE tick_id=? ORDER BY id DESC LIMIT 1", tick_id)
        if rl:
            rl["candidates"] = jload(rl["candidates"], [])
        d["recall"] = rl
        d["effects"] = [{**r, "args": jload(r["args"])} for r in rows(conn, "SELECT * FROM effect_logs WHERE tick_id=? ORDER BY id", tick_id)]
        d["need_math"] = rows(conn, "SELECT * FROM need_math WHERE tick_id=? ORDER BY id", tick_id)
        mm = one(conn, "SELECT * FROM mood_math WHERE tick_id=?", tick_id)
        if mm:
            mm["inputs"] = jload(mm["inputs"], {})
        d["mood_math"] = mm
        d["memories"] = rows(conn, "SELECT id, kind, text, importance, created_sim_time, embed_model, "
                                   "(embedding IS NOT NULL) AS has_embedding FROM memories WHERE tick_id=? ORDER BY id", tick_id)
        d["sleep_logs"] = []
        for s in rows(conn, "SELECT * FROM sleep_log WHERE tick_id=?", tick_id):
            s["tendency_changes"] = jload(s["tendency_changes"], {})
            s["new_memories"] = jload(s["new_memories"], [])
            s["new_preferences"] = jload(s["new_preferences"], [])
            s["traces"] = [{**r, "input": jload(r["input"]), "output": jload(r["output"]),
                            "proposed": jload(r["proposed"]), "clamped": jload(r["clamped"])}
                           for r in rows(conn, "SELECT * FROM sleep_traces WHERE sleep_log_id=? ORDER BY id", s["id"])]
            d["sleep_logs"].append(s)
        d["need_labels"] = {k: character.need_label(k, cfg) for k in cfg.character["needs"]}
        d["public"] = _public_view(cfg, d)
        return d


def _public_view(cfg: Config, d: dict) -> dict | None:
    """Every number the public website shows for this tick, each pointing at the math that made it."""
    tick = d["tick"]
    if not tick or not tick["needs_after"]:
        return None
    na = tick["needs_after"]
    needs = [{"id": k, "label": character.need_label(k, cfg), "value": round(na[k]),
              "word": character.need_word(k, na[k], cfg)} for k in cfg.character["needs"] if k in na]
    mood = tick["mood_after"] or {}
    return {"needs": needs, "mood": mood,
            "energy": {"value": round(d["energy"]) if d.get("energy") is not None else None,
                       "word": character.energy_word(d["energy"], cfg) if d.get("energy") is not None else None},
            "area": d["clock"]["area"], "clock": d["clock"]["label"], "action": tick["action"],
            "outcome": tick["outcome"], "reaction": tick["reaction"],
            "events": [e["text"] for e in d["events"]],
            "world_changes": [c.get("label") or c.get("name") or c.get("object_id") for c in tick["changes"]
                              if c.get("op") in ("create", "consume", "take", "drop", "move_object")]}


# ---------- commands ----------
def enqueue_command(cfg: Config, command: str, args: dict | None) -> int:
    with dev_db(cfg) as q:
        return q.enqueue(command, args, cfg.settings["allowed_tags"])


def recent_commands(cfg: Config, limit: int = 20, offset: int = 0) -> list[dict]:
    with dev_db(cfg) as q:
        return q.recent(limit, offset)

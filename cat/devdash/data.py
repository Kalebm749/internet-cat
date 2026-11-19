"""Queries behind the dev dashboard pages and /api/dev/* (read-only on cat.db)."""
from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
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
        d["project"] = jload(trace.get("project"), None) if trace else None
        pr = jload(trace.get("pressing"), None) if trace else None
        d["pressing"] = pr
        d["pressing_marks"] = ([r["expression"] for r in pr["rows"] if r["selected"] and r["expression"]]
                               + ([pr["stale_text"]] if pr.get("stale_text") else [])) if pr else []
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
            ea = tick.get("energy_after")
            d["energy"] = round(ea, 2) if ea is not None else None
            d["energy_effort"] = tick["effort"]
            d["energy_spent"] = cfg.settings["effort_energy_cost"].get(tick["effort"], 0) if tick["effort"] else 0
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


# ---------- LLM calls ----------
LLM_COLS = ("id, tick_id, kind, model, attempt, valid_json, error, prompt_tokens, completion_tokens, "
            "load_ms, prompt_eval_ms, eval_ms, total_ms, started_at, (system_prompt IS NOT NULL) AS has_prompts")


def _llm_where(f: dict) -> tuple[str, list]:
    where, args = [], []
    if f.get("kind"):
        where.append("kind = ?"); args.append(f["kind"])
    if f.get("model"):
        where.append("model = ?"); args.append(f["model"])
    if f.get("valid") == "valid":
        where.append("valid_json = 1 AND error IS NULL")
    elif f.get("valid") == "invalid":
        where.append("(valid_json = 0 OR error IS NOT NULL)")
    if f.get("errors"):
        where.append("error IS NOT NULL")
    if f.get("slow_ms") is not None:
        where.append("total_ms > ?"); args.append(f["slow_ms"])
    if f.get("tick_id") is not None:
        where.append("tick_id = ?"); args.append(f["tick_id"])
    return ((" WHERE " + " AND ".join(where)) if where else ""), args


def llm_list(cfg: Config, f: dict, page: int = 1, limit: int | None = None) -> dict:
    limit = min(limit or cfg.settings["devdash"]["page_size"], 500)
    page = max(1, page)
    w, args = _llm_where(f)
    with game_db(cfg) as conn:
        if conn is None:
            return {"items": [], "total": 0, "page": page, "limit": limit, "pages": 1, "kinds": [], "models": []}
        total = one(conn, f"SELECT COUNT(*) n FROM llm_calls{w}", *args)["n"]
        items = rows(conn, f"SELECT {LLM_COLS} FROM llm_calls{w} ORDER BY id DESC LIMIT ? OFFSET ?",
                     *args, limit, (page - 1) * limit)
        kinds = [r[0] for r in conn.execute("SELECT DISTINCT kind FROM llm_calls ORDER BY kind")]
        models = [r[0] for r in conn.execute("SELECT DISTINCT model FROM llm_calls ORDER BY model")]
    for it in items:
        it["status"] = "error" if it["error"] else ("ok" if it["valid_json"] else "invalid JSON")
    return {"items": items, "total": total, "page": page, "limit": limit,
            "pages": max(1, math.ceil(total / limit)), "kinds": kinds, "models": models}


def llm_detail(cfg: Config, call_id: int) -> dict | None:
    with game_db(cfg) as conn:
        if conn is None:
            return None
        c = one(conn, "SELECT * FROM llm_calls WHERE id=?", call_id)
        if c is None:
            return None
        c = _llm_view(c)
        c["has_prompts"] = c["system_prompt"] is not None
        c["siblings"] = rows(conn, "SELECT id, attempt, valid_json, error, total_ms FROM llm_calls "
                                   "WHERE tick_id=? AND kind=? ORDER BY id", c["tick_id"], c["kind"])
        return c


def llm_series(cfg: Config, kind: str = "", model: str = "", x: str = "tick", buckets: int = 60) -> dict:
    """Latency (p50/p95), tokens and invalid-JSON rate, bucketed over tick number or real time."""
    out: dict[str, Any] = {"x": [], "p50": [], "p95": [], "tokens_in": [], "tokens_out": [], "invalid": [],
                           "n": [], "axis": x}
    f = {"kind": kind, "model": model}
    w, args = _llm_where(f)
    with game_db(cfg) as conn:
        if conn is None:
            return out
        recs = rows(conn, f"SELECT id, tick_id, started_at, total_ms, prompt_tokens, completion_tokens, valid_json, error "
                          f"FROM llm_calls{w} ORDER BY id LIMIT 50000", *args)
    if not recs:
        return out
    buckets = max(1, min(buckets, cfg.settings["devdash"]["max_series_points"]))
    if x == "time":
        for r in recs:
            r["_x"] = datetime.fromisoformat(r["started_at"]).timestamp()
    else:
        for r in recs:
            r["_x"] = float(r["tick_id"])
    lo, hi = min(r["_x"] for r in recs), max(r["_x"] for r in recs)
    width = max((hi - lo) / buckets, 1.0)
    n_buckets = buckets if width > 1.0 else int((hi - lo) // width) + 1      # never more than asked for
    groups: dict[int, list[dict]] = {}
    for r in recs:
        groups.setdefault(min(int((r["_x"] - lo) // width), n_buckets - 1), []).append(r)
    for k in sorted(groups):
        g = groups[k]
        ms = [r["total_ms"] for r in g if r["total_ms"] is not None]
        out["x"].append(lo + k * width)
        out["p50"].append(r1(percentile(ms, 50)))
        out["p95"].append(r1(percentile(ms, 95)))
        out["tokens_in"].append(r1(mean([r["prompt_tokens"] for r in g if r["prompt_tokens"] is not None])))
        out["tokens_out"].append(r1(mean([r["completion_tokens"] for r in g if r["completion_tokens"] is not None])))
        out["invalid"].append(round(100 * sum(1 for r in g if (not r["valid_json"]) or r["error"]) / len(g), 1))
        out["n"].append(len(g))
    return out


# ---------- Memory and embeddings ----------
def recall_stats(conn: sqlite3.Connection) -> tuple[dict[int, int], dict[int, int], int]:
    """How often each memory was *selected* by recall (from recall_logs), the last tick that
    selected it, and how many recall events there were."""
    counts: dict[int, int] = {}
    last: dict[int, int] = {}
    n = 0
    for r in conn.execute("SELECT tick_id, candidates FROM recall_logs ORDER BY id"):
        n += 1
        for c in jload(r["candidates"], []):
            if c.get("selected"):
                counts[c["memory_id"]] = counts.get(c["memory_id"], 0) + 1
                last[c["memory_id"]] = r["tick_id"]
    return counts, last, n


def _mem_row(cfg: Config, first: datetime, r: dict, counts: dict, last: dict) -> dict:
    created = clock.parse(r["created_sim_time"])
    return {"id": r["id"], "kind": r["kind"], "text": r["text"], "importance": r["importance"],
            "created": r["created_sim_time"], "day_number": clock.day_number(first, created),
            "created_label": f"{clock.day_label(first, created)} {clock.time_label(created)}",
            "last_recalled": r["last_recalled"], "times_recalled": counts.get(r["id"], 0),
            "last_recall_tick": last.get(r["id"]), "has_embedding": bool(r["has_embedding"]),
            "embed_model": r["embed_model"], "tick_id": r["tick_id"]}


def memory_tables(cfg: Config, page: int = 1, limit: int | None = None) -> dict:
    limit = min(limit or cfg.settings["devdash"]["page_size"], 500)
    page = max(1, page)
    with game_db(cfg) as conn:
        if conn is None:
            return {"long": [], "short": [], "long_total": 0, "page": page, "pages": 1, "recall_events": 0,
                    "most_recalled": [], "never_recalled": [], "dims": None}
        first = clock.parse(db.get_meta(conn, "first_sim_time") or cfg.settings["sim_start"])
        counts, last, n_events = recall_stats(conn)
        cols = ("id, kind, text, importance, created_sim_time, last_recalled, embed_model, tick_id, "
                "(embedding IS NOT NULL) AS has_embedding")
        long_total = one(conn, "SELECT COUNT(*) n FROM memories WHERE kind='long'")["n"]
        longs = [_mem_row(cfg, first, r, counts, last) for r in rows(
            conn, f"SELECT {cols} FROM memories WHERE kind='long' ORDER BY id DESC LIMIT ? OFFSET ?", limit, (page - 1) * limit)]
        shorts = [_mem_row(cfg, first, r, counts, last) for r in rows(
            conn, f"SELECT {cols} FROM memories WHERE kind='short' ORDER BY id DESC LIMIT 200")]
        all_long = [_mem_row(cfg, first, r, counts, last) for r in rows(
            conn, f"SELECT {cols} FROM memories WHERE kind='long' ORDER BY id DESC")]
    most = sorted((m for m in all_long if m["times_recalled"]), key=lambda m: (-m["times_recalled"], -m["id"]))[:10]
    never = [m for m in all_long if not m["times_recalled"]]
    return {"long": longs, "short": shorts, "long_total": long_total, "page": page,
            "pages": max(1, math.ceil(long_total / limit)), "recall_events": n_events,
            "most_recalled": most, "never_recalled": never[:50], "never_total": len(never)}


def embedding_map(cfg: Config, query_vec: list[float] | None = None, selected_ids: set[int] | None = None) -> dict:
    """2D PCA (numpy, computed here on the server) of every memory embedding made with the current
    embed model. Optionally projects a query vector into the same plane."""
    import numpy as np
    from cat.memory import from_blob
    out: dict[str, Any] = {"points": [], "n": 0, "explained": None, "note": None, "query": None, "dims": None, "model": cfg.settings["embed_model"]}
    with game_db(cfg) as conn:
        if conn is None:
            out["note"] = "no memories yet"
            return out
        counts, _, _ = recall_stats(conn)
        first = clock.parse(db.get_meta(conn, "first_sim_time") or cfg.settings["sim_start"])
        recs = rows(conn, "SELECT id, kind, text, importance, created_sim_time, embedding, embed_model FROM memories "
                          "WHERE embedding IS NOT NULL ORDER BY id DESC LIMIT ?", cfg.settings["devdash"]["embedding_map_max_points"])
    use = [r for r in recs if r["embed_model"] == cfg.settings["embed_model"]]
    vecs = [(r, from_blob(r["embedding"])) for r in use]
    vecs = [(r, v) for r, v in vecs if v is not None and v.size]
    if not vecs:
        out["note"] = "no memory has an embedding made with the current embed model" if recs else "no memories yet"
        return out
    dim = max({v.size for _, v in vecs}, key=lambda d: sum(v.size == d for _, v in vecs))
    vecs = [(r, v) for r, v in vecs if v.size == dim]
    X = np.vstack([v for _, v in vecs]).astype(np.float64)
    mu = X.mean(axis=0)
    Xc = X - mu
    comps = np.zeros((2, dim))
    ratio = [0.0, 0.0]
    if len(vecs) >= 2:
        _, S, Vt = np.linalg.svd(Xc, full_matrices=False)
        k = min(2, Vt.shape[0])
        comps[:k] = Vt[:k]
        var = S ** 2
        if var.sum() > 0:
            ratio = [float(var[i] / var.sum()) if i < len(var) else 0.0 for i in range(2)]
    P = Xc @ comps.T
    q2 = None
    if query_vec is not None and len(query_vec) == dim:
        q2 = (np.asarray(query_vec, dtype=np.float64) - mu) @ comps.T
    pts = P if q2 is None else np.vstack([P, q2])
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    span = max(float((hi - lo).max()), 1e-9)
    center = (lo + hi) / 2

    def norm(p):
        return [round(0.5 + float((p[0] - center[0]) / span) * 0.92, 4), round(0.5 + float((p[1] - center[1]) / span) * 0.92, 4)]
    newest = max(clock.parse(r["created_sim_time"]) for r, _ in vecs)
    oldest = min(clock.parse(r["created_sim_time"]) for r, _ in vecs)
    age_span = max((newest - oldest).total_seconds(), 1.0)
    selected_ids = selected_ids or set()
    for (r, _), p in zip(vecs, P):
        created = clock.parse(r["created_sim_time"])
        x, y = norm(p)
        out["points"].append({"id": r["id"], "kind": r["kind"], "text": r["text"], "importance": r["importance"], "x": x, "y": y,
                              "age": round((newest - created).total_seconds() / age_span, 3),
                              "day_number": clock.day_number(first, created), "recalled": counts.get(r["id"], 0),
                              "selected": r["id"] in selected_ids})
    out.update(n=len(vecs), explained=[round(x, 4) for x in ratio], dims=dim)
    if q2 is not None:
        out["query"] = dict(zip(("x", "y"), norm(q2)))
    skipped = len(recs) - len(vecs)
    if skipped:
        out["note"] = f"{skipped} memories skipped: embedded with another model or dimension (run scripts/reembed.py)"
    return out


def embed_text(cfg: Config, text: str) -> tuple[list[float] | None, str | None]:
    host = cfg.settings["ollama_host"].rstrip("/")
    try:
        with httpx.Client(timeout=cfg.settings["devdash"]["llm_timeout_seconds"]) as c:
            r = c.post(f"{host}/api/embed", json={"model": cfg.settings["embed_model"], "input": [text]})
            r.raise_for_status()
            vecs = r.json().get("embeddings")
        if not vecs or not vecs[0]:
            return None, "Ollama returned no embedding"
        return vecs[0], None
    except (httpx.HTTPError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"


def memory_search(cfg: Config, text: str) -> dict:
    """Embed `text` and score every long-term memory with the engine's own Memory.score_candidates:
    recency, importance, relevance (cosine), weighted score, and whether it would be selected."""
    from cat.memory import Memory
    rc = cfg.settings["recall"]
    out: dict[str, Any] = {"query": text, "error": None, "weights": rc, "results": [], "dims": None, "used_embedding": False}
    vec, err = embed_text(cfg, text)
    out["error"] = err
    out["dims"] = len(vec) if vec else None
    out["vector"] = vec
    with game_db(cfg) as conn:
        if conn is None:
            return out
        st = db.get_state(conn)
        now = st["sim_time"] if st else cfg.settings["sim_start"]
        out["now"] = now
        mem = Memory(conn, cfg, None)
        cands = mem.score_candidates(vec, now)
        counts, _, _ = recall_stats(conn)
    cands.sort(key=lambda c: (-c["score"], -c["row"]["id"]))
    out["used_embedding"] = vec is not None
    out["results"] = [{"memory_id": c["row"]["id"], "text": c["row"]["text"], "recency": round(c["recency"], 4),
                       "importance": round(c["importance"], 4), "relevance": round(c["relevance"], 4),
                       "score": round(c["score"], 4), "selected": i < rc["k"], "times_recalled": counts.get(c["row"]["id"], 0),
                       "terms": {"recency": round(rc["w_recency"] * c["recency"], 4),
                                 "importance": round(rc["w_importance"] * c["importance"], 4),
                                 "relevance": round(rc["w_relevance"] * c["relevance"], 4)}}
                      for i, c in enumerate(cands)]
    return out


# ---------- Needs and mood ----------
RANGES = {"24h": 24 * 60, "7d": 7 * 24 * 60, "all": None}


def _epoch(sim_time: str) -> int:
    import calendar
    return calendar.timegm(clock.parse(sim_time).timetuple())     # sim times are naive: plotted as-is (UTC axis)


def _downsample(n: int, limit: int) -> list[int]:
    if n <= limit:
        return list(range(n))
    step = (n - 1) / (limit - 1)
    return sorted({round(i * step) for i in range(limit)})


def needs_series(cfg: Config, rng: str = "24h") -> dict:
    """Needs, energy and the mood slider over sim time, plus sleep/doze bands and the needs
    that sat at a critical level the longest."""
    rng = rng if rng in RANGES else "24h"
    crit = cfg.settings["need_bands"]["empty_below"]       # the empty band is the critical one
    out: dict[str, Any] = {"range": rng, "x": [], "needs": {}, "energy": [], "mood": [],
                           "bands": [], "critical": [], "critical_below": crit, "from": None, "to": None, "ticks": 0,
                           "labels": {k: character.need_label(k, cfg) for k in cfg.character["needs"]}}
    with game_db(cfg) as conn:
        if conn is None:
            return out
        recs = rows(conn, "SELECT id, sim_time, kind, duration, needs_after, mood_after, energy_after FROM ticks "
                          "WHERE needs_after IS NOT NULL ORDER BY id DESC LIMIT 60000")[::-1]
    if not recs:
        return out
    if RANGES[rng]:
        cutoff = _epoch(recs[-1]["sim_time"]) - RANGES[rng] * 60
        recs = [r for r in recs if _epoch(r["sim_time"]) >= cutoff]
    for r in recs:
        r["_n"], r["_m"], r["_t"] = jload(r["needs_after"], {}), jload(r["mood_after"], {}), _epoch(r["sim_time"])
    out["ticks"], out["from"], out["to"] = len(recs), recs[0]["sim_time"], recs[-1]["sim_time"]

    # critical time: sim minutes (sum of tick durations) each need spent below the critical level
    for nid in cfg.character["needs"]:
        total = streak = longest = 0
        for r in recs:
            v = r["_n"].get(nid)
            if v is not None and v < crit:
                streak += r["duration"] or 0
                total += r["duration"] or 0
                longest = max(longest, streak)
            else:
                streak = 0
        span = max(1, sum((r["duration"] or 0) for r in recs))
        out["critical"].append({"need": nid, "label": out["labels"][nid], "minutes": total, "hours": round(total / 60, 1),
                                "pct": round(100 * total / span, 1), "longest_minutes": longest})
    out["critical"].sort(key=lambda c: -c["minutes"])

    # bands: runs of asleep / doze / catch-up ticks
    run: dict | None = None
    for r in recs:
        if r["kind"] in ("asleep", "doze", "catchup"):
            end = _epoch(clock.advance(r["sim_time"], r["duration"] or 0))
            if run and run["kind"] == r["kind"]:
                run["to"] = end
            else:
                if run:
                    out["bands"].append(run)
                run = {"kind": r["kind"], "from": r["_t"], "to": end}
        elif run:
            out["bands"].append(run)
            run = None
    if run:
        out["bands"].append(run)

    keep = _downsample(len(recs), cfg.settings["devdash"]["max_series_points"])
    sel = [recs[i] for i in keep]
    out["x"] = [r["_t"] for r in sel]
    out["needs"] = {nid: [round(r["_n"].get(nid), 2) if r["_n"].get(nid) is not None else None for r in sel]
                    for nid in cfg.character["needs"]}
    out["energy"] = [r.get("energy_after") for r in sel]
    out["mood"] = [r["_m"].get("value", r["_m"].get("valence")) for r in sel]
    return out


# ---------- Behaviour ----------
def normalize_action(a: str | None) -> str:
    return " ".join((a or "").lower().split())


def normalize_reason(reason: str | None) -> str:
    import re
    return re.sub(r"'[^']*'", "'…'", reason or "(none)")


def behaviour(cfg: Config) -> dict:
    out: dict[str, Any] = {"days": [], "top_actions": [], "penalty": {"hits": 0, "ticks": 0, "recent": [], "by_day": {}},
                           "areas": [], "ops": [], "reasons": [], "total_actions": 0, "distinct_actions": 0}
    with game_db(cfg) as conn:
        if conn is None:
            return out
        first = clock.parse(db.get_meta(conn, "first_sim_time") or cfg.settings["sim_start"])
        ticks = rows(conn, "SELECT id, kind, sim_time, area, action, duration FROM ticks")
        by_day: dict[int, dict] = {}
        counts: dict[str, int] = {}
        area_min: dict[str, dict] = {}
        for t in ticks:
            dn = clock.day_number(first, clock.parse(t["sim_time"]))
            a = area_min.setdefault(t["area"], {"tick": 0, "asleep": 0, "doze": 0, "catchup": 0})
            a[t["kind"]] = a.get(t["kind"], 0) + (t["duration"] or 0)
            if t["kind"] == "tick" and t["action"]:
                act = normalize_action(t["action"])
                d = by_day.setdefault(dn, {"day": dn, "total": 0, "distinct": set()})
                d["total"] += 1
                d["distinct"].add(act)
                counts[act] = counts.get(act, 0) + 1
        out["days"] = [{"day": d["day"], "label": clock.day_label(first, first + timedelta(days=d["day"] - 1)),
                        "total": d["total"], "distinct": len(d["distinct"]),
                        "variety": round(len(d["distinct"]) / d["total"], 2)} for d in sorted(by_day.values(), key=lambda d: -d["day"])]
        total = sum(counts.values())
        out["total_actions"], out["distinct_actions"] = total, len(counts)
        out["top_actions"] = [{"action": a, "count": n, "pct": round(100 * n / total, 1)} for a, n in
                              sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]] if total else []
        tot_min = sum(sum(v.values()) for v in area_min.values()) or 1
        out["areas"] = sorted(
            [{"area": a, "name": cfg.world["areas"].get(a, {}).get("name", a), "minutes": sum(v.values()), "hours": round(sum(v.values()) / 60, 1),
              "pct": round(100 * sum(v.values()) / tot_min, 1), "awake": v["tick"], "asleep": v["asleep"], "doze": v["doze"]}
             for a, v in area_min.items()], key=lambda r: -r["minutes"])
        # repetition-penalty hits: a non-physical need's gain was cut by the repetition multiplier
        hit = rows(conn, "SELECT n.tick_id, t.sim_time, t.action, COUNT(*) needs, MIN(n.repetition_mult) mult FROM need_math n "
                         "JOIN ticks t ON t.id = n.tick_id WHERE n.repetition_mult < 1 AND n.gain_raw > 0 "
                         "GROUP BY n.tick_id ORDER BY n.tick_id DESC")
        out["penalty"]["hits"] = len(hit)
        out["penalty"]["ticks"] = (one(conn, "SELECT COUNT(*) n FROM ticks WHERE kind='tick'") or {}).get("n", 0)
        out["penalty"]["recent"] = hit[:15]
        for h in hit:
            dn = clock.day_number(first, clock.parse(h["sim_time"]))
            out["penalty"]["by_day"][dn] = out["penalty"]["by_day"].get(dn, 0) + 1
        for d in out["days"]:
            d["penalty_hits"] = out["penalty"]["by_day"].get(d["day"], 0)
        ops = rows(conn, "SELECT op, SUM(accepted) ok, SUM(1-accepted) rejected, COUNT(*) n FROM effect_logs GROUP BY op ORDER BY n DESC")
        out["ops"] = [{"op": o["op"] or "(invalid)", "accepted": o["ok"] or 0, "rejected": o["rejected"] or 0, "n": o["n"]} for o in ops]
        reasons: dict[str, int] = {}
        for r in conn.execute("SELECT reason FROM effect_logs WHERE accepted=0"):
            k = normalize_reason(r["reason"])
            reasons[k] = reasons.get(k, 0) + 1
        out["reasons"] = [{"reason": k, "count": n} for k, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))]
    return out


# ---------- Sleep ----------
def sleep_nights(cfg: Config, page: int = 1, limit: int | None = None) -> dict:
    limit = min(limit or 20, 200)
    out: dict[str, Any] = {"nights": [], "total": 0, "page": page, "pages": 1, "history": {"x": [], "tendencies": {}},
                           "tendency_ids": list(cfg.character["tendencies"]), "max_step": cfg.settings["sleep"]["tendency_max_step"]}
    with game_db(cfg) as conn:
        if conn is None:
            return out
        first = clock.parse(db.get_meta(conn, "first_sim_time") or cfg.settings["sim_start"])
        out["total"] = one(conn, "SELECT COUNT(*) n FROM sleep_log")["n"]
        out["pages"] = max(1, math.ceil(out["total"] / limit))
        logs_ = rows(conn, "SELECT * FROM sleep_log ORDER BY id DESC LIMIT ? OFFSET ?", limit, (max(1, page) - 1) * limit)
        for s in logs_:
            s["tendency_changes"] = jload(s["tendency_changes"], {})
            s["new_memories"] = jload(s["new_memories"], [])
            s["new_preferences"] = jload(s["new_preferences"], [])
            d = clock.parse(s["sim_date"] + "T00:00")
            s["day_label"] = clock.day_label(first, d)
            s["traces"] = {t["step"]: {**t, "input": jload(t["input"]), "output": jload(t["output"]),
                                       "proposed": jload(t["proposed"]), "clamped": jload(t["clamped"])}
                           for t in rows(conn, "SELECT * FROM sleep_traces WHERE sleep_log_id=? ORDER BY id", s["id"])}
            s["calls"] = rows(conn, "SELECT id, kind, attempt, valid_json, error, total_ms FROM llm_calls WHERE tick_id=? "
                                    "AND kind LIKE 'sleep_%' ORDER BY id", s["tick_id"]) if s["tick_id"] else []
            tt = s["traces"].get("tendencies")
            s["tendency_table"] = []
            if tt and tt["clamped"]:
                for k, c in tt["clamped"].items():
                    prop = (tt["proposed"] or {}).get(k)
                    s["tendency_table"].append({"id": k, "proposed": prop, "applied": c["applied_delta"], "from": c["from"],
                                                "to": c["to"], "max_step": c["max_step"], "clamped": prop is not None and abs(prop) * c["max_step"] > abs(c["applied_delta"]) + 1e-9})
            else:
                s["tendency_table"] = [{"id": k, "proposed": None, "applied": round(c["to"] - c["from"], 4), "from": c["from"], "to": c["to"],
                                        "max_step": out["max_step"], "clamped": False} for k, c in s["tendency_changes"].items()]
            pt = s["traces"].get("preferences")
            s["pref_table"] = []
            for p in (pt["clamped"] if pt and pt["clamped"] else s["new_preferences"]):
                s["pref_table"].append({"subject": p["subject"], "valence": p["valence"], "previous": p.get("previous"), "reason": p.get("reason", "")})
        out["nights"] = logs_
        # tendency history: value after each night (oldest first)
        allogs = rows(conn, "SELECT id, sim_date, tendency_changes FROM sleep_log ORDER BY id")
        traced = {r["sleep_log_id"]: jload(r["clamped"], {}) for r in conn.execute(
            "SELECT sleep_log_id, clamped FROM sleep_traces WHERE step='tendencies'")}
    cur = dict(cfg.character["tendencies"])
    hist: dict[str, list] = {k: [] for k in cur}
    xs = []
    for i, s in enumerate(allogs):
        ch = jload(s["tendency_changes"], {})
        full = traced.get(s["id"]) or {}
        for k in hist:
            if k in full:
                cur[k] = full[k]["to"]
            elif k in ch:
                cur[k] = ch[k]["to"]
            hist[k].append(round(cur[k], 4))
        xs.append(i + 1)
    out["history"] = {"x": xs, "tendencies": hist, "dates": [s["sim_date"] for s in allogs]}
    return out


# ---------- World state ----------
def _change_detail(cfg: Config, c: dict) -> str:
    areas = cfg.world["areas"]
    nm = lambda a: areas.get(a, {}).get("name", a)
    op = c.get("op")
    if op == "create":
        return f"created '{c.get('name') or c.get('label')}' in {nm(c.get('area'))}" + (" (spawned by a world event)" if c.get("source") == "event" else " (by the creature)")
    if op == "consume":
        return "eaten / used up"
    if op == "take":
        return "picked up (now carried)"
    if op == "drop":
        return f"put down in {nm(c.get('area'))}"
    if op == "move_object":
        return f"moved to {nm(c.get('area'))}"
    if op == "set_state":
        return f"state {c.get('key')} = {c.get('value')}"
    if op == "move_self":
        return f"creature moved to {nm(c.get('area'))}"
    return str(op)


def world_state(cfg: Config, object_id: str = "", page: int = 1, limit: int = 50) -> dict:
    out: dict[str, Any] = {"areas": [], "objects": [], "inventory": [], "history": [], "history_total": 0, "page": page, "pages": 1,
                           "creature_area": None, "weather": None, "object": object_id, "object_row": None}
    areas = cfg.world["areas"]
    with game_db(cfg) as conn:
        counts: dict[str, int] = {}
        objs: list[dict] = []
        if conn is not None:
            st = db.get_state(conn)
            if st:
                out["creature_area"], out["weather"] = st["area"], st["weather"]
            objs = rows(conn, "SELECT * FROM objects ORDER BY area, name")
            for o in objs:
                o["tags"], o["state"] = jload(o["tags"], []), jload(o["state"], {})
                o["area_name"] = areas.get(o["area"], {}).get("name", "carried" if o["area"] == "inventory" else o["area"])
                counts[o["area"]] = counts.get(o["area"], 0) + 1
            hist = []
            for t in conn.execute("SELECT id, kind, sim_time, changes FROM ticks WHERE changes IS NOT NULL AND changes NOT IN ('[]','') ORDER BY id DESC LIMIT 3000"):
                for c in reversed(jload(t["changes"], [])):          # newest first, also within one tick
                    hist.append({"tick_id": t["id"], "sim_time": t["sim_time"], "op": c.get("op"), "object_id": c.get("object_id"),
                                 "label": c.get("label") or c.get("name") or c.get("object_id") or "(creature)",
                                 "detail": _change_detail(cfg, c), "source": c.get("source") or "creature"})
            if object_id:
                hist = [h for h in hist if h["object_id"] == object_id]
                out["object_row"] = next((o for o in objs if o["id"] == object_id), None)
            out["history_total"] = len(hist)
            out["pages"] = max(1, math.ceil(len(hist) / limit))
            out["history"] = hist[(max(1, page) - 1) * limit: max(1, page) * limit]
    for aid, a in areas.items():
        out["areas"].append({"id": aid, "name": a["name"], "description": a["description"], "exits": [areas[e]["name"] for e in a["exits"]],
                             "objects": counts.get(aid, 0), "here": aid == out["creature_area"]})
    out["objects"] = [o for o in objs if o["area"] != "inventory"]
    out["inventory"] = [o for o in objs if o["area"] == "inventory"]
    return out


# ---------- Logs ----------
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_SERVICE_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{1,40}$")


def log_services(cfg: Config) -> list[str]:
    d = cfg.log_dir
    if not d.is_dir():
        return []
    return sorted({p.name.split(".jsonl")[0] for p in d.glob("*.jsonl*") if p.name.split(".jsonl")[0]})


def _log_files(cfg: Config, service: str) -> list[Path]:
    """The service's log files, newest first: <svc>.jsonl, <svc>.jsonl.1, <svc>.jsonl.2 ..."""
    d = cfg.log_dir
    files = []
    if (d / f"{service}.jsonl").is_file():
        files.append(d / f"{service}.jsonl")
    rotated = sorted((p for p in d.glob(f"{service}.jsonl.*") if p.name.rsplit(".", 1)[-1].isdigit()),
                     key=lambda p: int(p.name.rsplit(".", 1)[-1]))
    return files + rotated


def tail_lines(path: Path, max_lines: int, block: int = 64 * 1024) -> list[str]:
    """The last `max_lines` lines of a file, reading from the END in blocks (log files can be big)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            data_ = b""
            pos = size
            while pos > 0 and data_.count(b"\n") <= max_lines:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data_ = f.read(step) + data_
    except OSError:
        return []
    lines = data_.split(b"\n")
    if pos > 0:
        lines = lines[1:]                  # the first piece may be a partial line
    out = [ln.decode("utf-8", "replace") for ln in lines if ln.strip()]
    return out[-max_lines:]


def _parse_log_line(raw: str, service: str) -> dict:
    import hashlib
    try:
        r = json.loads(raw)
        if not isinstance(r, dict):
            raise ValueError
    except ValueError:                      # a stray print / traceback line
        r = {"ts": "", "level": "?", "service": service, "tick_id": None, "event": "raw", "message": raw[:2000], "extra": {}}
    r.setdefault("service", service)
    r["id"] = hashlib.sha1(f"{service}|{raw}".encode()).hexdigest()[:14]
    return r


def read_logs(cfg: Config, service: str = "", level: str = "", tick_id: int | None = None, q: str = "",
              limit: int | None = None, since: str = "") -> dict:
    """Merged, filtered tail of data/logs/*.jsonl, newest first. `since` (an ISO ts) returns only
    lines at or after it (used by the live tail)."""
    limit = min(limit or cfg.settings["devdash"]["log_tail_lines"], 2000)
    min_level = LEVELS.get(level.upper(), 0) if level else 0
    ql = q.lower().strip()
    services = log_services(cfg)
    if service:
        services = [service] if _SERVICE_RE.match(service) and service in services else []
    out: list[dict] = []
    scanned = 0
    for svc in services:
        got = 0
        for path in _log_files(cfg, svc):
            for raw in reversed(tail_lines(path, 5000)):
                scanned += 1
                r = _parse_log_line(raw, svc)
                if since and (r.get("ts") or "") < since:
                    continue
                if min_level and LEVELS.get(str(r.get("level")).upper(), 0) < min_level:
                    continue
                if tick_id is not None and r.get("tick_id") != tick_id:
                    continue
                if ql and ql not in raw.lower():
                    continue
                out.append(r)
                got += 1
                if got >= limit:
                    break
            if got >= limit:
                break
    out.sort(key=lambda r: (r.get("ts") or "", r["id"]), reverse=True)
    return {"lines": out[:limit], "services": log_services(cfg), "dir": str(cfg.log_dir), "scanned": scanned,
            "newest": out[0].get("ts") if out else ""}


# ---------- Config ----------
def _norm_yaml(obj: Any) -> str:
    import yaml
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)


def config_view(cfg: Config) -> dict:
    """The config as the engine loaded it (from devdash.db), compared with the files on disk now."""
    import difflib
    import hashlib
    import yaml
    with dev_db(cfg) as q:
        loaded = q.get_config()
        status = q.get_status()
    files = []
    for name in ("settings", "character", "world", "prompts"):
        path = Path(cfg.config_dir) / f"{name}.yaml"
        item: dict[str, Any] = {"name": name, "file": f"{name}.yaml", "loaded": None, "loaded_at": None, "disk": None,
                                "diff": "", "state": "unknown", "error": None, "applies": "live (reload_settings)" if name in ("settings", "prompts") else "on restart"}
        try:
            raw = path.read_bytes()
            item["disk_sha"] = hashlib.sha256(raw).hexdigest()
            item["disk"] = _norm_yaml(yaml.safe_load(raw))
        except FileNotFoundError:
            item["error"] = "file is missing on disk"
        except yaml.YAMLError as e:
            item["error"] = f"invalid YAML on disk: {e}"
        row = loaded.get(name)
        if row:
            item["loaded"], item["loaded_at"] = row["content"], row["loaded_at"]
            if item["error"]:
                item["state"] = "invalid"
            elif row["raw_sha"] == item.get("disk_sha"):
                item["state"] = "same"
            else:
                diff = list(difflib.unified_diff(row["content"].splitlines(), (item["disk"] or "").splitlines(),
                                                 "as loaded by the engine", f"{name}.yaml on disk now", lineterm="", n=2))
                item["diff"] = "\n".join(diff)
                item["state"] = "changed" if diff else "cosmetic"       # raw file differs but the values do not
        else:
            item["state"] = "invalid" if item["error"] else "not-loaded"
        files.append(item)
    return {"files": files, "engine_known": bool(loaded), "changed": [f["name"] for f in files if f["state"] == "changed"],
            "engine_state": status.get("state")}


def projects_page(cfg: Config, limit: int = 50) -> dict:
    """Every project with its progress number, idle counters and artifacts, and the settings behind them."""
    pc = cfg.settings["projects"]
    out: dict[str, Any] = {"projects": [], "artifacts": [], "settings": {
        k: pc[k] for k in ("max_active", "progress_values", "taper", "milestone_every", "completion_gains",
                           "pause_after_idle_nights", "stages")}}
    with game_db(cfg) as conn:
        if conn is None:
            return out
        try:
            for p in rows(conn, "SELECT * FROM projects ORDER BY id DESC"):
                p["stage"] = "finished" if p["status"] == "done" else character._word_from(pc["stages"], p["progress"])
                p["artifact_count"] = (one(conn, "SELECT COUNT(*) n FROM artifacts WHERE project_id=?", p["id"]) or {}).get("n", 0)
                out["projects"].append(p)
            out["artifacts"] = rows(conn, "SELECT * FROM artifacts ORDER BY id DESC LIMIT ?", limit)
        except Exception:                        # an old database without the tables
            pass
    return out

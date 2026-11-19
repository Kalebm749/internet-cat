"""Tracing: make every tick fully explainable.

While a tick runs, the engine hands the Tracer everything it does (exact LLM prompts and raw
responses, embedding calls, recall scoring, effect validation, need and mood arithmetic, event
rolls, sleep steps, stage timings). After the tick has been committed the Tracer writes it all in
its own transaction. **Tracing never breaks the creature:** any failure here is logged as a
warning and swallowed. `trace_level: summary` stores no prompt/response/input text."""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from sqlite3 import Connection
from typing import Any, Iterator

from cat import db
from cat.config import Config
from cat.db import dumps

log = logging.getLogger("cat.trace")

KIND = {"brain": "brain", "narrator": "narrator", "consolidate": "sleep_consolidate",
        "tendencies": "sleep_tendencies", "preferences": "sleep_preferences",
        "self_image": "sleep_self_image", "projects": "sleep_projects"}


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class Tracer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._reset()

    # ---------- settings (read live, so reload_settings applies) ----------
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.settings["trace_enabled"])

    @property
    def full(self) -> bool:
        return self.cfg.settings["trace_level"] == "full"

    # ---------- collecting ----------
    def _reset(self) -> None:
        self.started_at: str | None = None
        self._t0: float | None = None
        self.stages: dict[str, float] = {}
        self.llm: list[dict] = []
        self.embeds: list[dict] = []
        self.recall: dict | None = None
        self.effects: list[dict] = []
        self.need_math: list[dict] = []
        self.mood: dict | None = None
        self.pressing: dict | None = None      # which need expressions the brain prompt used, and why
        self.rolls: list[dict] = []
        self.sleep_steps: list[dict] = []
        self.sleep_log_id: int | None = None

    def begin(self) -> None:
        self._reset()
        self.started_at, self._t0 = _now(), time.perf_counter()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (time.perf_counter() - t) * 1000

    def add_stage(self, name: str, t_start: float) -> None:
        """Close a stage opened with `t_start = time.perf_counter()` (for big blocks)."""
        self.stages[name] = self.stages.get(name, 0.0) + (time.perf_counter() - t_start) * 1000

    def record_llm(self, task: str, model: str, attempt: int, info: dict, parsed: Any,
                   error: str | None) -> None:
        """Installed as `backend.recorder`; called by call_json after every attempt."""
        self.llm.append({"task": task, "model": model, "attempt": attempt, "info": info,
                         "parsed": parsed, "error": error or info.get("error")})

    def record_embed(self, purpose: str, model: str, text: str, dims: int | None, ms: float,
                     error: str | None, started: str) -> None:
        self.embeds.append({"purpose": purpose, "model": model, "text": text, "dims": dims,
                            "ms": ms, "error": error, "started": started})

    def set_recall(self, query: str, candidates: list[dict]) -> None:
        self.recall = {"query": query, "candidates": candidates}

    def set_effects(self, log_rows: list[dict]) -> None:
        self.effects = log_rows

    def set_need_math(self, rows: list[dict]) -> None:
        self.need_math = rows

    def set_pressing(self, selection: dict) -> None:
        self.pressing = selection

    def set_mood(self, inputs: dict, mood: dict) -> None:
        self.mood = {"inputs": inputs, "mood": mood}

    def set_event_rolls(self, rows: list[dict]) -> None:
        self.rolls = rows

    def set_sleep(self, steps: list[dict], sleep_log_id: int) -> None:
        self.sleep_steps, self.sleep_log_id = steps, sleep_log_id

    # ---------- writing ----------
    def flush(self, conn: Connection, tick_id: int | None, status: str, error: str | None = None) -> int | None:
        """Write what was collected. `tick_id=None` means the tick raised before it was recorded:
        it is stored under a negative id. Returns the id used. Never raises."""
        used = tick_id
        try:
            if self.enabled and self.started_at is not None:
                used = self._write(conn, tick_id, status, error)
        except Exception:
            log.warning("could not write trace for tick %s", tick_id, exc_info=True)
        finally:
            self._reset()
        return used

    def _write(self, conn: Connection, tick_id: int | None, status: str, error: str | None) -> int:
        full = self.full
        total = (time.perf_counter() - (self._t0 or time.perf_counter())) * 1000
        stages = {k: round(v, 1) for k, v in self.stages.items()}
        with db.transaction(conn):
            if tick_id is None:
                tick_id = -(conn.execute("SELECT COUNT(*) FROM tick_traces WHERE tick_id<0").fetchone()[0] + 1)
            prev = conn.execute("SELECT * FROM tick_traces WHERE tick_id=?", (tick_id,)).fetchone()
            if prev is not None:      # e.g. a forced sleep cycle attached to an existing tick
                merged = json.loads(prev["stage_timings"] or "{}")
                for k, v in stages.items():
                    merged[k] = round(merged.get(k, 0) + v, 1)
                conn.execute("UPDATE tick_traces SET finished_at=?, total_ms=?, stage_timings=? WHERE tick_id=?",
                             (_now(), round((prev["total_ms"] or 0) + total, 1), dumps(merged), tick_id))
            else:
                conn.execute("INSERT INTO tick_traces(tick_id,started_at,finished_at,total_ms,stage_timings,"
                             "status,error) VALUES(?,?,?,?,?,?,?)",
                             (tick_id, self.started_at, _now(), round(total, 1), dumps(stages), status, error))
            if self.pressing is not None:
                conn.execute("UPDATE tick_traces SET pressing=? WHERE tick_id=?", (dumps(self.pressing), tick_id))
            for c in self.llm:
                i = c["info"]
                conn.execute(
                    "INSERT INTO llm_calls(tick_id,kind,model,options,system_prompt,user_prompt,schema,"
                    "raw_response,parsed,valid_json,attempt,error,prompt_tokens,completion_tokens,load_ms,"
                    "prompt_eval_ms,eval_ms,total_ms,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tick_id, KIND.get(c["task"], "other"), c["model"], dumps(i.get("options") or {}),
                     i.get("system_prompt") if full else None, i.get("user_prompt") if full else None,
                     dumps(i.get("schema")) if full else None,
                     i.get("raw_response") if full else None,
                     dumps(c["parsed"]) if (full and c["parsed"] is not None) else None,
                     int(bool(i.get("valid_json"))), c["attempt"], c["error"], i.get("prompt_tokens"),
                     i.get("completion_tokens"), i.get("load_ms"), i.get("prompt_eval_ms"), i.get("eval_ms"),
                     i.get("total_ms"), i.get("started_at") or self.started_at))
            for e in self.embeds:
                conn.execute("INSERT INTO embed_calls(tick_id,purpose,model,input_text,dims,total_ms,error,"
                             "started_at) VALUES(?,?,?,?,?,?,?,?)",
                             (tick_id, e["purpose"], e["model"], e["text"] if full else None, e["dims"],
                              round(e["ms"]), e["error"], e["started"]))
            if self.recall is not None:
                keep = self.cfg.settings["trace_recall_candidates"]
                cands = [c for i, c in enumerate(self.recall["candidates"]) if i < keep or c["selected"]]
                if not full:
                    cands = [{k: v for k, v in c.items() if k != "text"} for c in cands]
                conn.execute("INSERT INTO recall_logs(tick_id,query_text,candidates) VALUES(?,?,?)",
                             (tick_id, self.recall["query"] if full else None, dumps(cands)))
            for e in self.effects:
                conn.execute("INSERT INTO effect_logs(tick_id,op,args,accepted,reason) VALUES(?,?,?,?,?)",
                             (tick_id, e["op"], dumps(e["args"]), e["accepted"], e["reason"]))
            for r in self.need_math:
                conn.execute('INSERT INTO need_math(tick_id,need,"before",decay,bucket,gain_raw,repetition_mult,'
                             'effort_cost,event_drain,dislike_drain,clamp_adjust,"after") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                             (tick_id, r["need"], r["before"], r["decay"], r["bucket"], r["gain_raw"],
                              r["repetition_mult"], r["effort_cost"], r.get("event_drain", 0.0),
                              r.get("dislike_drain", 0.0), r["clamp_adjust"], r["after"]))
            if self.mood is not None:
                m = self.mood["mood"]
                conn.execute("INSERT OR REPLACE INTO mood_math(tick_id,inputs,value,word) VALUES(?,?,?,?)",
                             (tick_id, dumps(self.mood["inputs"]), m["value"], m["word"]))
            for r in self.rolls:
                conn.execute("INSERT INTO event_rolls(tick_id,event_text,chance,roll,fired,filtered_reason) "
                             "VALUES(?,?,?,?,?,?)",
                             (tick_id, r["event_text"], r["chance"], r["roll"], r["fired"], r["filtered_reason"]))
            if self.sleep_log_id is not None:
                for st in self.sleep_steps:
                    conn.execute("INSERT INTO sleep_traces(sleep_log_id,step,input,output,proposed,clamped) "
                                 "VALUES(?,?,?,?,?,?)",
                                 (self.sleep_log_id, st["step"], dumps(st["input"]) if full else None,
                                  dumps(st["output"]) if full else None, dumps(st["proposed"]),
                                  dumps(st["clamped"])))
        return tick_id


def prune(conn: Connection, cfg: Config) -> dict[str, int]:
    """Retention: drop the big trace tables' rows older than `trace_retention_days` (real time).
    tick_traces, need_math, effect_logs, mood_math, event_rolls and sleep_traces are small and
    are kept. Call inside no transaction; never raises."""
    days = cfg.settings["trace_retention_days"]
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="milliseconds")
    out: dict[str, int] = {}
    try:
        with db.transaction(conn):
            out["llm_calls"] = conn.execute("DELETE FROM llm_calls WHERE started_at < ?", (cutoff,)).rowcount
            out["embed_calls"] = conn.execute("DELETE FROM embed_calls WHERE started_at < ?", (cutoff,)).rowcount
            out["recall_logs"] = conn.execute(
                "DELETE FROM recall_logs WHERE tick_id IN (SELECT tick_id FROM tick_traces WHERE started_at < ?)",
                (cutoff,)).rowcount
    except Exception:
        log.warning("trace retention failed", exc_info=True)
    if any(out.values()):
        log.info("trace retention (%d days): pruned %s", days, out)
    return out

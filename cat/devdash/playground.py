"""Prompt playground: replay a saved LLM call (edited or not) against Ollama and compare.

Read-only replay: nothing here is ever applied to the world. It never touches cat.db; results are
saved in dev.db's `playground_runs` so they can be compared later."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any

import httpx

from cat.config import Config
from cat.devdash import data
from cat.llm import BackendError, extract_json, ollama_metrics

LOCK = threading.Lock()          # one playground run at a time: Ollama serves one request at a time anyway
MAX_MODELS = 3
MAX_PROMPT_CHARS = 200_000


class PlaygroundError(ValueError):
    pass


def validate(body: Any) -> dict:
    """Check a run request and return it cleaned. Raises PlaygroundError."""
    if not isinstance(body, dict):
        raise PlaygroundError("body must be a JSON object")
    models = body.get("models") or ([body["model"]] if body.get("model") else [])
    if not isinstance(models, list) or not models or not all(isinstance(m, str) and m.strip() for m in models):
        raise PlaygroundError("choose at least one model")
    models = list(dict.fromkeys(m.strip() for m in models))
    if len(models) > MAX_MODELS:
        raise PlaygroundError(f"compare at most {MAX_MODELS} models at once")
    if any(len(m) > 120 for m in models):
        raise PlaygroundError("model name too long")
    system, user = str(body.get("system_prompt") or ""), str(body.get("user_prompt") or "")
    if not user.strip():
        raise PlaygroundError("the user prompt is empty")
    if len(system) + len(user) > MAX_PROMPT_CHARS:
        raise PlaygroundError("prompt too large")
    schema = body.get("schema")
    if isinstance(schema, str):
        if schema.strip():
            try:
                schema = json.loads(schema)
            except ValueError as e:
                raise PlaygroundError(f"the JSON schema is not valid JSON: {e}")
        else:
            schema = None
    if schema is not None and not isinstance(schema, dict):
        raise PlaygroundError("the JSON schema must be an object")
    opts = body.get("options") or {}
    out_opts: dict[str, Any] = {}
    if opts.get("temperature") is not None:
        t = opts["temperature"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t <= 2:
            raise PlaygroundError("temperature must be between 0 and 2")
        out_opts["temperature"] = t
    if opts.get("num_ctx") is not None:
        n = opts["num_ctx"]
        if isinstance(n, bool) or not isinstance(n, int) or not 256 <= n <= 131072:
            raise PlaygroundError("num_ctx must be a whole number between 256 and 131072")
        out_opts["num_ctx"] = n
    think = body.get("think")
    if think is not None and not isinstance(think, bool):
        raise PlaygroundError("think must be true or false")
    src = body.get("source_call_id")
    if src is not None and (isinstance(src, bool) or not isinstance(src, int)):
        raise PlaygroundError("source_call_id must be a number")
    return {"models": models, "system_prompt": system, "user_prompt": user, "schema": schema,
            "options": out_opts, "think": think, "source_call_id": src, "label": str(body.get("label") or "")[:80]}


def call_ollama(cfg: Config, model: str, system: str, user: str, schema: dict | None,
                options: dict, think: bool | None) -> dict:
    """One non-streaming /api/chat call. Returns the raw text, parse result, tokens and timings."""
    messages = ([{"role": "system", "content": system}] if system.strip() else []) + [{"role": "user", "content": user}]
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if schema:
        payload["format"] = schema
    if options:
        payload["options"] = options
    if think is not None:
        payload["think"] = think
    res: dict[str, Any] = {"raw_response": None, "parsed": None, "valid_json": 0, "error": None}
    t0 = time.perf_counter()
    try:
        host = cfg.settings["ollama_host"].rstrip("/")
        with httpx.Client(timeout=cfg.settings["devdash"]["llm_timeout_seconds"]) as c:
            r = c.post(f"{host}/api/chat", json=payload)
            r.raise_for_status()
            body = r.json()
        res.update(ollama_metrics(body))
        content = (body.get("message") or {}).get("content")
        res["raw_response"] = content if isinstance(content, str) else None
        if res["raw_response"] is None:
            raise BackendError("no message content in the response")
        if schema or res["raw_response"].lstrip().startswith(("{", "[", "<think>", "```")):
            res["parsed"] = extract_json(res["raw_response"])
            res["valid_json"] = 1
    except httpx.TimeoutException:
        res["error"] = f"timeout after {cfg.settings['devdash']['llm_timeout_seconds']}s"
    except httpx.HTTPStatusError as e:
        res["error"] = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    except (httpx.HTTPError, ValueError, BackendError) as e:
        res["error"] = f"{type(e).__name__}: {e}"
    if not res.get("total_ms"):
        res["total_ms"] = int((time.perf_counter() - t0) * 1000)
    return res


def run(cfg: Config, req: dict) -> dict:
    """Run the request on each model in turn; save every run. Returns {group_id, runs}."""
    group = uuid.uuid4().hex[:12]
    saved = []
    for model in req["models"]:
        started = datetime.now().isoformat(timespec="seconds")
        opts = dict(req["options"])
        if req["think"] is not None:
            opts_logged = {**opts, "think": req["think"]}
        else:
            opts_logged = opts
        r = call_ollama(cfg, model, req["system_prompt"], req["user_prompt"], req["schema"], opts, req["think"])
        with data.dev_db(cfg) as q:
            cur = q.conn.execute(
                "INSERT INTO playground_runs(created_at,group_id,source_call_id,label,model,options,system_prompt,"
                "user_prompt,schema,raw_response,parsed,valid_json,error,prompt_tokens,completion_tokens,load_ms,"
                "prompt_eval_ms,eval_ms,total_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (started, group, req["source_call_id"], req["label"], model, json.dumps(opts_logged),
                 req["system_prompt"], req["user_prompt"], json.dumps(req["schema"]) if req["schema"] else None,
                 r["raw_response"], json.dumps(r["parsed"]) if r["parsed"] is not None else None, r["valid_json"],
                 r["error"], r.get("prompt_tokens"), r.get("completion_tokens"), r.get("load_ms"),
                 r.get("prompt_eval_ms"), r.get("eval_ms"), r.get("total_ms")))
            saved.append(get_run(cfg, cur.lastrowid, q))
    return {"group_id": group, "runs": saved}


def _row(r: dict) -> dict:
    r["options"] = data.jload(r["options"], {})
    r["schema_obj"] = data.jload(r["schema"])
    r["parsed_obj"] = data.jload(r["parsed"])
    r["status"] = "error" if r["error"] else ("ok" if r["valid_json"] else "not JSON")
    return r


def get_run(cfg: Config, run_id: int, q=None) -> dict | None:
    def go(qq):
        r = data.one(qq.conn, "SELECT * FROM playground_runs WHERE id=?", run_id)
        return _row(r) if r else None
    if q is not None:
        return go(q)
    with data.dev_db(cfg) as qq:
        return go(qq)


def list_runs(cfg: Config, limit: int = 50, offset: int = 0) -> list[dict]:
    with data.dev_db(cfg) as q:
        rs = data.rows(q.conn, "SELECT id, created_at, group_id, source_call_id, label, model, valid_json, error, "
                               "prompt_tokens, completion_tokens, total_ms FROM playground_runs ORDER BY id DESC LIMIT ? OFFSET ?",
                       limit, offset)
    for r in rs:
        r["status"] = "error" if r["error"] else ("ok" if r["valid_json"] else "not JSON")
    return rs


def group_runs(cfg: Config, group_id: str) -> list[dict]:
    with data.dev_db(cfg) as q:
        return [_row(r) for r in data.rows(q.conn, "SELECT * FROM playground_runs WHERE group_id=? ORDER BY id", group_id)]


def diff_keys(a: Any, b: Any) -> list[str]:
    """Top-level keys whose values differ between two parsed JSON objects."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return [] if a == b else ["(whole response)"]
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))

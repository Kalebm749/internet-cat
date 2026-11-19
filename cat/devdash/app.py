"""The developer dashboard: see everything the system does. Separate service, port 8090, bound to
127.0.0.1 by default (see auth.py). Read-only on cat.db; its only writes go to dev.db."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cat import logs
from cat.config import Config, load_config
from cat.devdash import auth, data, playground
from cat.devqueue import CommandError

HERE = Path(__file__).parent

NAV = [("overview", "Overview", "/"), ("ticks", "Ticks", "/ticks"), ("llm", "LLM calls", "/llm"),
       ("memory", "Memory", "/memory"), ("needs", "Needs & mood", "/needs"),
       ("behaviour", "Behaviour", "/behaviour"), ("sleep", "Sleep", "/sleep"), ("world", "World", "/world"),
       ("logs", "Logs", "/logs"), ("config", "Config", "/config"), ("playground", "Playground", "/playground")]
SECURITY_HEADERS = {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff"}


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config(os.environ.get("CAT_CONFIG_DIR"),
                             {"db_path": os.environ["CAT_DB"]} if os.environ.get("CAT_DB") else None)
    auth.check_startup()                       # refuse to start LAN-exposed without credentials
    if logs.structured_enabled():
        logs.setup_logging("devdash", cfg)
    app = FastAPI(title="Internet Cat devdash", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False, default=str)
    templates.env.globals["nav"] = NAV

    # ---------- access rules ----------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path not in auth.OPEN_PATHS:
            creds = auth.credentials()
            if creds and not auth.authorized(request.headers.get("authorization"), creds):
                return Response("authentication required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="devdash", charset="UTF-8"', **SECURITY_HEADERS})
            if not auth.csrf_ok(request.method, request.headers):
                return JSONResponse({"error": "missing X-Requested-With: devdash header"}, status_code=403,
                                    headers=SECURITY_HEADERS)
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    def page(request: Request, name: str, active: str, title: str, refresh: bool = False, **ctx: Any):
        return templates.TemplateResponse(request, name, {
            "active": active, "title": title, "creature": cfg.character["name"], "refresh_s":
            cfg.settings["devdash"]["refresh_seconds"] if refresh else 0, "cfg": cfg, **ctx})

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "devdash"}

    # ---------- pages ----------
    @app.get("/", response_class=HTMLResponse)
    def overview_page(request: Request):
        return page(request, "overview.html", "overview", "Overview", refresh=True, o=data.overview(cfg))

    @app.get("/ticks", response_class=HTMLResponse)
    def ticks_page(request: Request, status: str = "", kind: str = "", area: str = "", q: str = "",
                   rejections: int = 0, errors: int = 0, slow_ms: int | None = None, page_no: int = Query(1, alias="page", ge=1)):
        f = {"status": status, "kind": kind, "area": area, "q": q, "rejections": rejections, "errors": errors,
             "slow_ms": slow_ms}
        return page(request, "ticks.html", "ticks", "Ticks", t=data.ticks_list(cfg, f, page_no), f=f)

    @app.get("/ticks/{tick_id}", response_class=HTMLResponse)
    def tick_page(request: Request, tick_id: int):
        d = data.tick_detail(cfg, tick_id)
        if d is None:
            raise HTTPException(404, f"no tick {tick_id}")
        return page(request, "tick.html", "ticks", f"Tick {tick_id}", d=d)

    @app.get("/llm", response_class=HTMLResponse)
    def llm_page(request: Request, kind: str = "", model: str = "", valid: str = "", errors: int = 0,
                 slow_ms: int | None = None, tick_id: int | None = None, page_no: int = Query(1, alias="page", ge=1)):
        f = {"kind": kind, "model": model, "valid": valid, "errors": errors, "slow_ms": slow_ms, "tick_id": tick_id}
        return page(request, "llm.html", "llm", "LLM calls", t=data.llm_list(cfg, f, page_no), f=f)

    @app.get("/llm/{call_id}", response_class=HTMLResponse)
    def llm_call_page(request: Request, call_id: int):
        c = data.llm_detail(cfg, call_id)
        if c is None:
            raise HTTPException(404, f"no LLM call {call_id}")
        return page(request, "llm_call.html", "llm", f"LLM call {call_id}", c=c)

    @app.get("/memory", response_class=HTMLResponse)
    def memory_page(request: Request, q: str = "", page_no: int = Query(1, alias="page", ge=1)):
        tables = data.memory_tables(cfg, page_no)
        search = data.memory_search(cfg, q.strip()) if q.strip() else None
        selected = {r["memory_id"] for r in search["results"] if r["selected"]} if search else set()
        emap = data.embedding_map(cfg, search["vector"] if search else None, selected)
        return page(request, "memory.html", "memory", "Memory & embeddings", t=tables, search=search, emap=emap, q=q)

    @app.get("/needs", response_class=HTMLResponse)
    def needs_page(request: Request, range: str = "24h"):
        return page(request, "needs.html", "needs", "Needs & mood", range=range if range in data.RANGES else "24h",
                    n=data.needs_series(cfg, range))

    @app.get("/behaviour", response_class=HTMLResponse)
    def behaviour_page(request: Request):
        return page(request, "behaviour.html", "behaviour", "Behaviour", b=data.behaviour(cfg))

    @app.get("/sleep", response_class=HTMLResponse)
    def sleep_page(request: Request, page_no: int = Query(1, alias="page", ge=1)):
        return page(request, "sleep.html", "sleep", "Sleep", s=data.sleep_nights(cfg, page_no))

    @app.get("/world", response_class=HTMLResponse)
    def world_page(request: Request, object: str = "", page_no: int = Query(1, alias="page", ge=1)):
        return page(request, "world.html", "world", "World state", w=data.world_state(cfg, object, page_no), object=object)

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request, service: str = "", level: str = "", tick_id: int | None = None, q: str = "",
                  limit: int = Query(300, ge=1, le=2000)):
        f = {"service": service, "level": level, "tick_id": tick_id, "q": q, "limit": limit}
        return page(request, "logs.html", "logs", "Logs", L=data.read_logs(cfg, service, level, tick_id, q, limit), f=f,
                    levels=list(data.LEVELS))

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request):
        return page(request, "config.html", "config", "Config", c=data.config_view(cfg))

    @app.get("/playground", response_class=HTMLResponse)
    def playground_page(request: Request, call_id: int | None = None):
        src = data.llm_detail(cfg, call_id) if call_id is not None else None
        if call_id is not None and src is None:
            raise HTTPException(404, f"no LLM call {call_id}")
        models = [m["name"] for m in data.ollama_info(cfg)["installed"]]
        return page(request, "playground.html", "playground", "Playground", src=src, models=models,
                    configured=[cfg.settings[k] for k in ("brain_model", "narrator_model", "sleep_model")],
                    runs=playground.list_runs(cfg, 15))

    @app.get("/playground/runs", response_class=HTMLResponse)
    def playground_runs_page(request: Request):
        return page(request, "playground_runs.html", "playground", "Playground runs", runs=playground.list_runs(cfg, 200))

    @app.get("/playground/groups/{group_id}", response_class=HTMLResponse)
    def playground_group_page(request: Request, group_id: str):
        runs = playground.group_runs(cfg, group_id)
        if not runs:
            raise HTTPException(404, "no such playground group")
        src_id = runs[0]["source_call_id"]
        src = data.llm_detail(cfg, src_id) if src_id else None
        return page(request, "playground_group.html", "playground", "Playground compare", runs=runs, src=src,
                    diff=playground.diff_keys)

    # ---------- JSON API (/api/dev/...) ----------
    def j(payload: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(payload, status_code=status)

    @app.get("/api/dev/overview")
    def api_overview():
        return j(data.overview(cfg))

    @app.get("/api/dev/ollama")
    def api_ollama():
        return j(data.ollama_info(cfg))

    @app.get("/api/dev/ticks")
    def api_ticks(status: str = "", kind: str = "", area: str = "", q: str = "", rejections: int = 0,
                  errors: int = 0, slow_ms: int | None = None, page_no: int = Query(1, alias="page", ge=1),
                  limit: int = Query(50, ge=1, le=500)):
        f = {"status": status, "kind": kind, "area": area, "q": q, "rejections": rejections, "errors": errors,
             "slow_ms": slow_ms}
        return j(data.ticks_list(cfg, f, page_no, limit))

    @app.get("/api/dev/ticks/{tick_id}")
    def api_tick(tick_id: int):
        d = data.tick_detail(cfg, tick_id)
        if d is None:
            raise HTTPException(404, f"no tick {tick_id}")
        return j(d)

    @app.get("/api/dev/commands")
    def api_commands(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        return j({"items": data.recent_commands(cfg, limit, offset)})

    @app.post("/api/dev/commands")
    async def api_command(request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "body must be JSON")
        if not isinstance(body, dict) or body.get("confirm") is not True:
            raise HTTPException(400, 'confirmation required: send {"command": ..., "args": {...}, "confirm": true}')
        try:
            cid = data.enqueue_command(cfg, str(body.get("command") or ""), body.get("args") or {})
        except CommandError as e:
            raise HTTPException(422, str(e))
        return j({"id": cid, "status": "pending"}, 202)

    @app.get("/api/dev/llm")
    def api_llm(kind: str = "", model: str = "", valid: str = "", errors: int = 0, slow_ms: int | None = None,
                tick_id: int | None = None, page_no: int = Query(1, alias="page", ge=1), limit: int = Query(50, ge=1, le=500)):
        f = {"kind": kind, "model": model, "valid": valid, "errors": errors, "slow_ms": slow_ms, "tick_id": tick_id}
        return j(data.llm_list(cfg, f, page_no, limit))

    @app.get("/api/dev/llm/series")
    def api_llm_series(kind: str = "", model: str = "", x: str = Query("tick", pattern="^(tick|time)$"),
                       buckets: int = Query(60, ge=1, le=2000)):
        return j(data.llm_series(cfg, kind, model, x, buckets))

    @app.get("/api/dev/llm/{call_id}")
    def api_llm_call(call_id: int):
        c = data.llm_detail(cfg, call_id)
        if c is None:
            raise HTTPException(404, f"no LLM call {call_id}")
        return j(c)

    @app.get("/api/dev/memory")
    def api_memory(page_no: int = Query(1, alias="page", ge=1), limit: int = Query(50, ge=1, le=500)):
        return j(data.memory_tables(cfg, page_no, limit))

    @app.get("/api/dev/memory/map")
    def api_memory_map():
        return j(data.embedding_map(cfg))

    @app.get("/api/dev/memory/search")
    def api_memory_search(q: str = Query(..., min_length=1, max_length=2000)):
        r = data.memory_search(cfg, q)
        r.pop("vector", None)
        return j(r)

    @app.get("/api/dev/needs")
    def api_needs(range: str = Query("24h", pattern="^(24h|7d|all)$")):
        return j(data.needs_series(cfg, range))

    @app.get("/api/dev/behaviour")
    def api_behaviour():
        return j(data.behaviour(cfg))

    @app.get("/api/dev/sleep")
    def api_sleep(page_no: int = Query(1, alias="page", ge=1), limit: int = Query(20, ge=1, le=200)):
        return j(data.sleep_nights(cfg, page_no, limit))

    @app.get("/api/dev/world")
    def api_world(object: str = "", page_no: int = Query(1, alias="page", ge=1), limit: int = Query(50, ge=1, le=500)):
        return j(data.world_state(cfg, object, page_no, limit))

    @app.get("/api/dev/logs")
    def api_logs(service: str = "", level: str = "", tick_id: int | None = None, q: str = "", since: str = "",
                 limit: int = Query(300, ge=1, le=2000)):
        return j(data.read_logs(cfg, service, level, tick_id, q, limit, since))

    @app.get("/api/dev/config")
    def api_config():
        return j(data.config_view(cfg))

    @app.get("/api/dev/playground/models")
    def api_pg_models():
        info = data.ollama_info(cfg)
        return j({"reachable": info["reachable"], "models": [m["name"] for m in info["installed"]], "error": info["error"]})

    @app.get("/api/dev/playground/runs")
    def api_pg_runs(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        return j({"items": playground.list_runs(cfg, limit, offset)})

    @app.get("/api/dev/playground/runs/{run_id}")
    def api_pg_run(run_id: int):
        r = playground.get_run(cfg, run_id)
        if r is None:
            raise HTTPException(404, f"no playground run {run_id}")
        return j(r)

    @app.post("/api/dev/playground/run")
    def api_pg_run_post(body: dict):
        """Replay a prompt on 1-3 models. Never applied to the world; saved to dev.db for comparison."""
        try:
            req = playground.validate(body)
        except playground.PlaygroundError as e:
            raise HTTPException(422, str(e))
        if not playground.LOCK.acquire(blocking=False):
            raise HTTPException(409, "another playground run is in progress (Ollama serves one request at a time)")
        try:
            result = playground.run(cfg, req)
        finally:
            playground.LOCK.release()
        return j(result)

    return app


app = create_app()

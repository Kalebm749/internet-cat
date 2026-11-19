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
from cat.devdash import auth, data
from cat.devqueue import CommandError

HERE = Path(__file__).parent

NAV = [("overview", "Overview", "/"), ("ticks", "Ticks", "/ticks")]
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

    return app


app = create_app()

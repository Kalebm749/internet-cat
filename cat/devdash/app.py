"""The developer dashboard (docs/rewrite-contracts.md §8): see everything the engine does and why.

A separate service on port 8090, bound to 127.0.0.1 by default (see auth.py; LAN exposure requires Basic auth).
Read-only on nit.db: it never writes the life and never calls a model. Every page has a JSON twin at
/api/dev/<page>.

    Overview     time, place, activity, body (graphs), derived condition, service health, counts
    Decisions    every decision; the inspector shows the Mind, the prompt, every attempt and what came of it
    Character    versions with their evidence, every development proposal with each check's verdict, preferences
    Memory       core and long-term memories (with retention), today's notable moments
    Knowledge    what it knows it can do, where, when and how it learned it
    Repetition   freshness by action and recent activities, repeated thoughts
    World        facts (touched or not), perceived events, capability use
    Sleep        each night's stages, outputs and timings
    Mail         the mailbox, the answered pile with Nit's replies, imports
    Content      validation of content/ and the nit tuning
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cat import content as contentmod, logs, readmodel, store, tuning as tuningmod
from cat.config import Config, load_config
from cat.devdash import auth
from cat.healthcheck import heartbeat_age

HERE = Path(__file__).parent
NAV = [("overview", "Overview", "/"), ("decisions", "Decisions", "/decisions"), ("character", "Character", "/character"),
       ("memory", "Memory", "/memory"), ("knowledge", "Knowledge", "/knowledge"), ("repetition", "Repetition", "/repetition"),
       ("world", "World", "/world"), ("sleep", "Sleep", "/sleep"), ("mail", "Mail", "/mail"),
       ("llm", "LLM calls", "/llm"), ("content", "Content", "/content")]
SECURITY_HEADERS = {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff"}


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config(os.environ.get("CAT_CONFIG_DIR"))
    if os.environ.get("CAT_DB"):
        cfg.settings["nit"]["db_path"] = os.environ["CAT_DB"]
    auth.check_startup()                                     # refuse to start LAN-exposed without credentials
    if logs.structured_enabled():
        logs.setup_logging("devdash", cfg)
    settings_path = cfg.config_dir / "settings.yaml"
    t = tuningmod.load(settings_path)
    content_dir = tuningmod.path(t, "content_dir")
    content = contentmod.load(content_dir)
    app = FastAPI(title="Nit devdash", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False, default=str)
    templates.env.globals["nav"] = NAV
    page_size = cfg.settings["devdash"]["page_size"]

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

    def open_db() -> sqlite3.Connection | None:
        return store.open_readonly(cfg.db_path)

    def with_db(fn: Callable[[sqlite3.Connection], dict]) -> dict:
        conn = open_db()
        if conn is None:
            return {"empty": True}
        try:
            return fn(conn)
        finally:
            conn.close()

    def sim_now(conn) -> str:
        st = readmodel.state(conn)
        return st["sim_time"] if st else "2026-01-01T00:00"

    # ---------------------------------------------------------------- page data

    def overview(conn) -> dict:
        now = readmodel.now(conn, content, t, heartbeat_age(cfg.heartbeat_path))
        series = readmodel.body_series(conn, 24, sim_now(conn))
        sparks = {v: readmodel.sparkline(series[v], *(-1, 1) if v == "mood" else (0, 1)) for v in series}
        last_night = readmodel.sleep_view(conn)[:1]
        return {"now": now, "sparks": sparks, "heartbeat_age": heartbeat_age(cfg.heartbeat_path),
                "counts": readmodel.counts(conn), "decisions": readmodel.decisions(conn, limit=12),
                "last_night": last_night[0] if last_night else None, "models": t["models"],
                "repetition": readmodel.repetition_view(conn, t)["score"]}

    def llm_calls(conn) -> dict:
        path = cfg.path("data/llm-calls.json")
        try:
            snapshot = json.loads(path.read_text())
        except (OSError, ValueError):
            snapshot = {"active": [], "recent": [], "updated_at": None}
        now = readmodel.now(conn, content, t, heartbeat_age(cfg.heartbeat_path))
        activity = None
        if now and now.get("doing"):
            doing = now["doing"]
            spec = content.actions.get(doing["action"])
            activity = {"line": doing["line"], "action": spec.name if spec else doing["action"],
                        "tool": spec.capability if spec else None, "since": doing["since"]}
        heartbeat = heartbeat_age(cfg.heartbeat_path)
        alive = heartbeat is not None and heartbeat <= cfg.settings["healthcheck"]["engine_max_age_seconds"]
        active = [dict(call) for call in snapshot.get("active", [])]
        for call in active:
            call["activity"] = activity
            call["tool"] = activity["tool"] if activity and call.get("task") == "capability" else None
        if not alive:
            for call in active:
                call["status"] = "stale"
        return {"active": active, "recent": snapshot.get("recent", []), "updated_at": snapshot.get("updated_at"),
                "activity": activity, "engine_alive": alive, "heartbeat_age": heartbeat}

    pages: dict[str, tuple[str, Callable[[sqlite3.Connection, Request], dict]]] = {
        "overview": ("Overview", lambda c, r: overview(c)),
        "decisions": ("Decisions", lambda c, r: {"decisions": readmodel.decisions(
            c, int(r.query_params["before"]) if r.query_params.get("before", "").isdigit() else None, page_size),
            "page_size": page_size}),
        "character": ("Character", lambda c, r: {**readmodel.character_history(c), "who": readmodel.identity(c)}),
        "memory": ("Memory", lambda c, r: readmodel.memory_view(c, content, t, sim_now(c))),
        "knowledge": ("Knowledge", lambda c, r: {"knowledge": readmodel.knowledge(c, content)}),
        "repetition": ("Repetition", lambda c, r: readmodel.repetition_view(c, t)),
        "world": ("World", lambda c, r: readmodel.world_view(c, content)),
        "sleep": ("Sleep", lambda c, r: {"nights": readmodel.sleep_view(c)}),
        "mail": ("Mail", lambda c, r: readmodel.mail_view(c, content)),
        "llm": ("LLM calls", lambda c, r: llm_calls(c)),
    }

    def render(request: Request, name: str, title: str, data: dict) -> HTMLResponse:
        return templates.TemplateResponse(request, f"{name}.html", {
            "active": name, "title": title, "d": data, "refresh_s": cfg.settings["devdash"]["refresh_seconds"]
            if name in ("overview", "llm") else 0})

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "devdash"}

    def add(name: str, title: str, fn):
        path = "/" if name == "overview" else f"/{name}"

        @app.get(path, response_class=HTMLResponse, name=f"page_{name}")
        def html(request: Request):
            return render(request, name, title, with_db(lambda c: fn(c, request)))

        @app.get(f"/api/dev/{name}", name=f"api_{name}")
        def api(request: Request):
            return JSONResponse(json.loads(json.dumps(with_db(lambda c: fn(c, request)), default=str)))

    for name, (title, fn) in pages.items():
        add(name, title, fn)

    @app.get("/decisions/{did}", response_class=HTMLResponse)
    def decision_page(request: Request, did: int):
        d = with_db(lambda c: readmodel.decision(c, did) or {})
        if not d or d.get("empty"):
            raise HTTPException(404, "no such decision")
        return render(request, "decision", f"Decision {did}", d)

    @app.get("/api/dev/decisions/{did}")
    def decision_api(did: int):
        d = with_db(lambda c: readmodel.decision(c, did) or {})
        if not d or d.get("empty"):
            raise HTTPException(404, "no such decision")
        return JSONResponse(json.loads(json.dumps(d, default=str)))

    @app.get("/content", response_class=HTMLResponse)
    def content_page(request: Request):
        return render(request, "content", "Content", readmodel.content_check(content_dir, settings_path))

    @app.get("/api/dev/content")
    def content_api():
        return readmodel.content_check(content_dir, settings_path)

    return app


app = create_app()

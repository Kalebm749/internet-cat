"""Read-only FastAPI app: one page and a small JSON API. Never writes, never calls the LLM."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cat import db, logs
from cat.config import Config, load_config
from cat.web import views

HERE = Path(__file__).parent


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config(os.environ.get("CAT_CONFIG_DIR"),
                             {"db_path": os.environ["CAT_DB"]} if os.environ.get("CAT_DB") else None)
    if logs.structured_enabled():
        logs.setup_logging("web", cfg)
    app = FastAPI(title="Internet Cat", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    def open_db() -> sqlite3.Connection | None:
        """A fresh read-only connection per request. None while the engine has never run."""
        path = cfg.db_path
        if not path.exists():
            return None
        try:
            return db.connect(path, readonly=True)
        except sqlite3.OperationalError:
            return None

    def close(conn) -> None:
        if conn is not None:
            conn.close()

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, before: int | None = None, all_keepsakes: int = 0):
        conn = open_db()
        try:
            state = views.build_state(conn, cfg)
            if before is not None:            # no-JS "read older pages"
                entries, nxt = views.diary_view(conn, cfg, before=before)
                state["diary"], state["diary_next_before"] = entries, nxt
            if all_keepsakes:                 # no-JS "more keepsakes"
                state["keepsakes"], state["keepsakes_has_more"] = views.keepsakes_view(
                    conn, cfg, 0, cfg.settings["web_ui"]["keepsakes_all_limit"])
        finally:
            close(conn)
        return templates.TemplateResponse(request, "index.html", {"s": state, "before": before})

    @app.get("/biography", response_class=HTMLResponse)
    def biography(request: Request):
        conn = open_db()
        try:
            b = views.biography_view(conn, cfg)
        finally:
            close(conn)
        return templates.TemplateResponse(request, "biography.html", {"b": b})

    @app.get("/api/biography")
    def api_biography():
        conn = open_db()
        try:
            return JSONResponse(views.biography_view(conn, cfg))
        finally:
            close(conn)

    @app.get("/healthz")
    def healthz():
        """Liveness for the container healthcheck: the app is up (and whether the DB is readable)."""
        conn = open_db()
        try:
            ok = conn is not None and conn.execute("SELECT 1").fetchone() is not None
        except sqlite3.Error:
            ok = False
        finally:
            close(conn)
        return JSONResponse({"status": "ok", "db": ok}, headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    def api_state():
        conn = open_db()
        try:
            return JSONResponse(views.build_state(conn, cfg), headers={"Cache-Control": "no-store"})
        finally:
            close(conn)

    @app.get("/api/diary")
    def api_diary(before: int | None = None, limit: int = Query(20, ge=1)):
        conn = open_db()
        try:
            entries, nxt = views.diary_view(conn, cfg, before=before, limit=limit)
        finally:
            close(conn)
        return JSONResponse({"entries": entries, "next_before": nxt}, headers={"Cache-Control": "no-store"})

    @app.get("/api/keepsakes")
    def api_keepsakes(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1)):
        conn = open_db()
        try:
            items, more = views.keepsakes_view(conn, cfg, offset, min(limit, cfg.settings["web_ui"]["max_page_size"]))
        finally:
            close(conn)
        return JSONResponse({"keepsakes": items, "has_more": more}, headers={"Cache-Control": "no-store"})

    return app


app = create_app()

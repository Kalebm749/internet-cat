"""The public website (docs/rewrite-contracts.md §8): Nit now, its diary, correspondence and artwork.

Reads nit.db read-only and never calls a model. Its ONLY write is a visitor's letter into data/mail.db
(cat/mail.py), which the engine imports into the mailbox at the market stall.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cat import content as contentmod, logs, mail, readmodel, store, tuning as tuningmod
from cat.config import Config, load_config
from cat.healthcheck import heartbeat_age

HERE = Path(__file__).parent
LETTER_CODES = {"ok": "Your letter is in the mailbox at the market stall. Nit will read it when it chooses to.",
                "empty": "The letter is empty.", "long": "The letter is too long.",
                "blocked": "This letter cannot be delivered.", "slow": "You have sent enough letters for now.",
                "full": "The mailbox is full for now. Try again later.", "error": "The letter could not be sent."}
SECURITY_HEADERS = {"X-Frame-Options": "DENY", "Referrer-Policy": "same-origin", "X-Content-Type-Options": "nosniff"}


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config(os.environ.get("CAT_CONFIG_DIR"))
    if os.environ.get("CAT_DB"):
        cfg.settings["nit"]["db_path"] = os.environ["CAT_DB"]
    if os.environ.get("CAT_MAIL_DB"):
        cfg.settings["web"]["mail_db"] = os.environ["CAT_MAIL_DB"]
    if logs.structured_enabled():
        logs.setup_logging("web", cfg)
    t = tuningmod.load(cfg.config_dir / "settings.yaml")
    content = contentmod.load(tuningmod.path(t, "content_dir"))
    app = FastAPI(title="Nit", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    web = cfg.settings["web"]
    salt = secrets.token_hex(16)          # visitor ids are only for this process's rate limits

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    def open_db() -> sqlite3.Connection | None:
        return store.open_readonly(cfg.db_path)

    def gather(before: str | None = None) -> dict:
        conn = open_db()
        if conn is None:
            return {"now": None, "diary": [], "next": None, "who": None, "knows": []}
        try:
            entries, nxt = readmodel.diary(conn, content, before=before, limit=web["diary_page"])
            return {"now": readmodel.now(conn, content, t, heartbeat_age(cfg.heartbeat_path)), "diary": entries,
                    "next": nxt, "who": readmodel.identity(conn), "knows": readmodel.knowledge(conn, content)}
        finally:
            conn.close()

    def artwork_rows() -> list[dict]:
        conn = open_db()
        if conn is None:
            return []
        try:
            row = conn.execute("SELECT value FROM facts WHERE key = 'art_room.artworks'").fetchone()
            return sorted(json.loads(row[0]), key=lambda x: x.get("created_at", ""), reverse=True) if row else []
        finally:
            conn.close()

    def correspondence_rows() -> list[dict]:
        life_conn = open_db()
        mail_conn = mail.open_for_reading(cfg.mail_db_path)
        try:
            records = {}
            key = content.world.get("mailbox")
            if life_conn and key:
                location = key.split(".", 1)[0]
                for fact_key in (key, f"{location}.answered", f"{location}.discarded"):
                    row = life_conn.execute("SELECT value FROM facts WHERE key = ?", (fact_key,)).fetchone()
                    if row:
                        for item in json.loads(row[0]):
                            if isinstance(item, dict):
                                records[str(item.get("id", ""))] = item
            incoming = mail_conn.execute("SELECT id, received_at, sender, text FROM letters ORDER BY received_at DESC, id DESC").fetchall() if mail_conn else []
            result = []
            for item in incoming:
                saved = records.get(mail.letter_key(item["id"]), {})
                replied = saved.get("status") == "answered" and isinstance(saved.get("reply"), str)
                discarded = saved.get("status") == "discarded"
                result.append({"id": item["id"], "received_at": item["received_at"], "sender": item["sender"],
                               "text": item["text"], "reply": saved.get("reply") if replied else None,
                               "answered_at": saved.get("answered_at") if replied else None,
                               "discarded_at": saved.get("discarded_at") if discarded else None,
                               "status": "answered" if replied else "discarded" if discarded else "unanswered"})
            return result
        finally:
            if life_conn:
                life_conn.close()
            if mail_conn:
                mail_conn.close()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "web"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, before: str | None = None, letter: str | None = None):
        s = gather(before)
        return templates.TemplateResponse(request, "index.html", {
            **s, "before": before, "letter_message": LETTER_CODES.get(letter or ""), "letter_ok": letter == "ok",
            "max_chars": web["mail_max_chars"], "name_max": web["mail_name_max"], "refresh_s": web["refresh_seconds"]})

    @app.get("/artwork", response_class=HTMLResponse)
    def artwork_page(request: Request):
        return templates.TemplateResponse(request, "artwork.html", {"artworks": artwork_rows()})

    @app.get("/artwork/{artifact_id}", response_class=HTMLResponse)
    def artwork_detail(request: Request, artifact_id: str):
        item = next((a for a in artwork_rows() if a.get("id") == artifact_id), None)
        if item is None:
            return Response(status_code=404)
        return templates.TemplateResponse(request, "artwork_detail.html", {"artwork": item})

    @app.get("/api/artwork")
    def api_artwork():
        return {"artworks": [{k: v for k, v in a.items() if k != "svg"} for a in artwork_rows()]}

    @app.get("/artwork/image/{artifact_id}.svg")
    def artwork_image(artifact_id: str):
        item = next((a for a in artwork_rows() if a.get("id") == artifact_id), None)
        if item is None or not item.get("svg"):
            return Response(status_code=404)
        return Response(item["svg"], media_type="image/svg+xml",
                        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})

    @app.get("/letters", response_class=HTMLResponse)
    def letters_page(request: Request):
        return templates.TemplateResponse(request, "letters.html", {"letters": correspondence_rows()})

    @app.get("/api/letters")
    def api_letters():
        return {"letters": correspondence_rows()}

    @app.get("/api/now")
    def api_now(after: str | None = None):
        conn = open_db()
        if conn is None:
            return JSONResponse({"now": None, "entries": []})
        try:
            entries, _ = readmodel.diary(conn, content, after=after, limit=web["diary_page"]) if after else ([], None)
            return {"now": readmodel.now(conn, content, t, heartbeat_age(cfg.heartbeat_path)), "entries": entries}
        finally:
            conn.close()

    @app.get("/api/diary")
    def api_diary(before: str | None = None):
        conn = open_db()
        if conn is None:
            return {"entries": [], "next": None}
        try:
            entries, nxt = readmodel.diary(conn, content, before=before, limit=web["diary_page"])
            return {"entries": entries, "next": nxt}
        finally:
            conn.close()

    @app.get("/places", response_class=HTMLResponse)
    def places_page(request: Request):
        conn = open_db()
        try:
            data = {"places": readmodel.places(conn, content), "wishes": readmodel.wishes(conn, content),
                    "now": readmodel.now(conn, content, t, None)} if conn else {"places": [], "wishes": [], "now": None}
        finally:
            if conn:
                conn.close()
        return templates.TemplateResponse(request, "places.html", data)

    @app.get("/api/places")
    def api_places():
        conn = open_db()
        if conn is None:
            return {"places": [], "wishes": []}
        try:
            return {"places": readmodel.places(conn, content), "wishes": readmodel.wishes(conn, content)}
        finally:
            conn.close()

    @app.post("/letters")
    def send_letter(request: Request, text: str = Form(""), sender: str = Form(""), website: str = Form("")):
        if website:                                    # the honeypot: people never fill it in
            return RedirectResponse("/?letter=ok#letter", status_code=303)
        visitor = mail.visitor_id(request.client.host if request.client else "?", salt)
        try:
            conn = mail.open_for_writing(cfg.mail_db_path)
            try:
                res = mail.submit(conn, web, sender, text, visitor)
            finally:
                conn.close()
        except sqlite3.Error:
            return RedirectResponse("/?letter=error#letter", status_code=303)
        return RedirectResponse(f"/?letter={res.code}#letter", status_code=303)

    return app


app = create_app()

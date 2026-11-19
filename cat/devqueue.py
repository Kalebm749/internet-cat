"""The dev dashboard's small writable database (data/devdash.db).

The dashboard NEVER writes game state. It inserts rows into `dev_commands`; the engine, the only
writer of `cat.db`, polls the queue, runs the commands and records the result here. This file also
holds `playground_runs` (prompt replays, never applied to the world), `engine_status` (what the
engine publishes about itself) and `engine_config` (the config as the engine loaded it)."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

COMMANDS = ("pause", "resume", "step", "set_interval", "force_sleep", "reload_settings",
            "inject_event", "snapshot")

SCHEMA = """
CREATE TABLE IF NOT EXISTS dev_commands(
  id INTEGER PRIMARY KEY, created_at TEXT, command TEXT, args JSON,
  status TEXT,            -- pending | running | done | failed | rejected
  result TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS playground_runs(
  id INTEGER PRIMARY KEY, created_at TEXT, group_id TEXT, source_call_id INT, label TEXT,
  model TEXT, options JSON, system_prompt TEXT, user_prompt TEXT, schema JSON,
  raw_response TEXT, parsed JSON, valid_json INT, error TEXT,
  prompt_tokens INT, completion_tokens INT, load_ms INT, prompt_eval_ms INT, eval_ms INT, total_ms INT);

CREATE TABLE IF NOT EXISTS engine_status(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS engine_config(name TEXT PRIMARY KEY, content TEXT, raw_sha TEXT, loaded_at TEXT);
"""


class CommandError(ValueError):
    """The command or its arguments are invalid."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=10)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def validate(command: str, args: dict | None, allowed_tags: list[str] | None = None) -> dict:
    """Check a command and return its cleaned args. Raises CommandError."""
    if command not in COMMANDS:
        raise CommandError(f"unknown command '{command}' (known: {', '.join(COMMANDS)})")
    args = dict(args or {})
    if command == "set_interval":
        sec = args.get("seconds")
        if sec is not None:
            if isinstance(sec, bool) or not isinstance(sec, (int, float)) or sec < 0 or sec > 86400:
                raise CommandError("seconds must be a number between 0 and 86400 (or null to reset)")
        ticks = args.get("ticks")
        if ticks is not None and (isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 1):
            raise CommandError("ticks must be a whole number >= 1")
        return {"seconds": sec, "ticks": ticks}
    if command == "inject_event":
        text = str(args.get("text") or "").strip()
        if not text or len(text) > 300:
            raise CommandError("text is required (max 300 characters)")
        out: dict[str, Any] = {"text": text}
        if args.get("area"):
            out["area"] = str(args["area"])
        sp = args.get("spawn")
        if sp:
            name = str(sp.get("name") or "").strip()
            if not name or len(name) > 60:
                raise CommandError("spawn.name is required (max 60 characters)")
            tags = sp.get("tags") or []
            if not isinstance(tags, list):
                raise CommandError("spawn.tags must be a list")
            out["spawn"] = {"name": name, "tags": [str(t) for t in tags],
                            "area": str(sp["area"]) if sp.get("area") else None}
        return out
    return {}


class DevQueue:
    """Thin wrapper over devdash.db used by both the engine and the dashboard."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.conn = connect(self.path)

    def close(self) -> None:
        self.conn.close()

    # ----- commands -----
    def enqueue(self, command: str, args: dict | None = None, allowed_tags: list[str] | None = None) -> int:
        clean = validate(command, args, allowed_tags)
        cur = self.conn.execute(
            "INSERT INTO dev_commands(created_at,command,args,status,updated_at) VALUES(?,?,?,?,?)",
            (_now(), command, json.dumps(clean), "pending", _now()))
        return cur.lastrowid

    def pending(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM dev_commands WHERE status='pending' ORDER BY id").fetchall()

    def mark(self, cmd_id: int, status: str, result: str | None = None) -> None:
        self.conn.execute("UPDATE dev_commands SET status=?, result=COALESCE(?, result), updated_at=? WHERE id=?",
                          (status, result, _now(), cmd_id))

    def recent(self, limit: int = 20, offset: int = 0) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM dev_commands ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))]

    # ----- what the engine publishes -----
    def set_status(self, values: dict[str, Any]) -> None:
        now = _now()
        self.conn.executemany(
            "INSERT INTO engine_status(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [(k, json.dumps(v), now) for k, v in values.items()])

    def get_status(self) -> dict[str, Any]:
        out = {}
        for r in self.conn.execute("SELECT key, value, updated_at FROM engine_status"):
            out[r["key"]] = json.loads(r["value"])
            out["_updated_at"] = max(out.get("_updated_at", ""), r["updated_at"])
        return out

    def set_config(self, name: str, content: str, raw_sha: str) -> None:
        self.conn.execute(
            "INSERT INTO engine_config(name,content,raw_sha,loaded_at) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET content=excluded.content, raw_sha=excluded.raw_sha, "
            "loaded_at=excluded.loaded_at", (name, content, raw_sha, _now()))

    def get_config(self) -> dict[str, dict]:
        return {r["name"]: dict(r) for r in self.conn.execute("SELECT * FROM engine_config")}


def epoch() -> float:
    return time.time()

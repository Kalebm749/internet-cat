"""Visitor letters (docs/rewrite-contracts.md §4.8).

    website form -> data/mail.db (written ONLY by the website) -> the engine imports new letters -> the mailbox fact

The website checks a letter (length, control characters, an optional blocklist, rate limits per visitor and for
everyone) and stores it. The engine reads mail.db read-only, appends each new letter to the mailbox fact named by
content/world.yaml `mailbox`, and records what it imported in nit.db (`mail_imports`), so each database keeps a
single writer. Letters go straight to the mailbox: there is no review. A letter is open-ended: it may ask Nit
anything; it reaches the model only as a visitor's quoted words.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cat import db

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS letters (
        id          INTEGER PRIMARY KEY,
        received_at TEXT NOT NULL,          -- real time (ISO)
        sender      TEXT NOT NULL,
        text        TEXT NOT NULL,
        visitor     TEXT NOT NULL           -- a salted hash of the visitor's address, for rate limits only
    )""",
    "CREATE INDEX IF NOT EXISTS idx_letters_time ON letters(received_at)",
]
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def open_for_writing(path: Path | str) -> sqlite3.Connection:
    conn = db.connect(path)
    for stmt in SCHEMA:
        conn.execute(stmt)
    return conn


def open_for_reading(path: Path | str) -> sqlite3.Connection | None:
    if not Path(path).exists():
        return None
    try:
        return db.connect(path, readonly=True)
    except sqlite3.OperationalError:
        return None


def visitor_id(address: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}|{address}".encode()).hexdigest()[:16]


def clean(text: str) -> str:
    text = CONTROL.sub("", text.replace("\r\n", "\n")).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


@dataclass
class Result:
    ok: bool
    code: str                 # ok | empty | long | blocked | slow | full
    message: str
    id: int | None = None


def submit(conn: sqlite3.Connection, web: dict, sender: str, text: str, visitor: str,
           now: datetime | None = None) -> Result:
    """Check and store one letter. `web` is settings `web`."""
    now = now or datetime.now()
    text, sender = clean(text or ""), clean(sender or "")[: web["mail_name_max"]] or "someone"
    if not text:
        return Result(False, "empty", "The letter is empty.")
    if len(text) > web["mail_max_chars"]:
        return Result(False, "long", f"The letter is too long (at most {web['mail_max_chars']} characters).")
    lowered = f" {text.lower()} {sender.lower()} "
    if any(re.search(rf"\b{re.escape(w.lower())}\b", lowered) for w in web["mail_blocklist"]):
        return Result(False, "blocked", "This letter cannot be delivered.")
    hour_ago = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    mine = conn.execute("SELECT COUNT(*) FROM letters WHERE visitor = ? AND received_at >= ?",
                        (visitor, hour_ago)).fetchone()[0]
    if mine >= web["mail_per_ip_per_hour"]:
        return Result(False, "slow", "You have sent enough letters for now. Try again later.")
    everyone = conn.execute("SELECT COUNT(*) FROM letters WHERE received_at >= ?", (hour_ago,)).fetchone()[0]
    if everyone >= web["mail_per_hour"]:
        return Result(False, "full", "The mailbox is full for now. Try again later.")
    with db.transaction(conn):
        cur = conn.execute("INSERT INTO letters(received_at, sender, text, visitor) VALUES (?, ?, ?, ?)",
                           (now.isoformat(timespec="seconds"), sender, text, visitor))
    return Result(True, "ok", "Your letter is in the mailbox at the market stall.", cur.lastrowid)


def new_letters(mail: sqlite3.Connection, after_id: int, limit: int = 20) -> list[sqlite3.Row]:
    return mail.execute("SELECT id, received_at, sender, text FROM letters WHERE id > ? ORDER BY id LIMIT ?",
                        (after_id, limit)).fetchall()


def letter_key(letter_id: int) -> str:
    """The id a letter has in the mailbox fact."""
    return f"L{letter_id}"

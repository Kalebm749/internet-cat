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
import hmac
import re
import secrets
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
    # a very simple account: a unique name, claimed by the first letter that uses it and protected by a passphrase
    """CREATE TABLE IF NOT EXISTS accounts (
        id          INTEGER PRIMARY KEY,
        handle      TEXT NOT NULL,           -- as the owner wrote it
        handle_key  TEXT NOT NULL UNIQUE,    -- lowercase: names are unique ignoring case
        salt        TEXT NOT NULL,
        secret_hash TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS denials (visitor TEXT NOT NULL, at TEXT NOT NULL)",
]
HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
HASH_ROUNDS = 150_000
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def open_for_writing(path: Path | str) -> sqlite3.Connection:
    conn = db.connect(path)
    for stmt in SCHEMA:
        conn.execute(stmt)
    if "account_id" not in {r[1] for r in conn.execute("PRAGMA table_info(letters)")}:
        conn.execute("ALTER TABLE letters ADD COLUMN account_id INTEGER")      # letters before accounts have none
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
    code: str                 # ok | empty | long | blocked | slow | full | name | secret | denied
    message: str
    id: int | None = None


def _hash(secret: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, HASH_ROUNDS).hex()


def submit(conn: sqlite3.Connection, web: dict, sender: str, text: str, visitor: str,
           now: datetime | None = None, secret: str = "") -> Result:
    """Check and store one letter from a named visitor. `web` is settings `web`.

    The name is the visitor's account: unique (ignoring case), claimed by the first letter that uses it, and from then
    on only a letter with the same passphrase may use it. So Nit can remember who a name is and nobody can write as
    someone else. No e-mail, no recovery: a lost passphrase means a new name."""
    now = now or datetime.now()
    text, sender, secret = clean(text or ""), clean(sender or ""), (secret or "").strip()
    if not text:
        return Result(False, "empty", "The letter is empty.")
    if not (web["mail_name_min"] <= len(sender) <= web["mail_name_max"] and HANDLE.match(sender)):
        return Result(False, "name", f"Choose a name of {web['mail_name_min']} to {web['mail_name_max']} letters, "
                                     "numbers, dashes or underscores.")
    if len(text) > web["mail_max_chars"]:
        return Result(False, "long", f"The letter is too long (at most {web['mail_max_chars']} characters).")
    lowered = f" {text.lower()} {sender.lower()} "
    if any(re.search(rf"\b{re.escape(w.lower())}\b", lowered) for w in web["mail_blocklist"]):
        return Result(False, "blocked", "This letter cannot be delivered.")
    hour_ago = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    if conn.execute("SELECT COUNT(*) FROM denials WHERE visitor = ? AND at >= ?", (visitor, hour_ago)).fetchone()[0] >= 8:
        return Result(False, "slow", "Too many wrong passphrases. Try again later.")
    acct = conn.execute("SELECT * FROM accounts WHERE handle_key = ?", (sender.lower(),)).fetchone()
    if acct is not None:
        ok = hmac.compare_digest(_hash(secret, bytes.fromhex(acct["salt"])), acct["secret_hash"])
        if not ok:
            with db.transaction(conn):
                conn.execute("INSERT INTO denials(visitor, at) VALUES (?, ?)", (visitor, now.isoformat(timespec="seconds")))
            return Result(False, "denied", "That name is taken, or the passphrase does not match it.")
    elif len(secret) < web["mail_secret_min"]:
        return Result(False, "secret", f"A new name needs a passphrase of at least {web['mail_secret_min']} characters: "
                                       "it is how you prove the name is yours when you write again.")
    mine = conn.execute("SELECT COUNT(*) FROM letters WHERE visitor = ? AND received_at >= ?",
                        (visitor, hour_ago)).fetchone()[0]
    if mine >= web["mail_per_ip_per_hour"]:
        return Result(False, "slow", "You have sent enough letters for now. Try again later.")
    if acct is not None:
        by_name = conn.execute("SELECT COUNT(*) FROM letters WHERE account_id = ? AND received_at >= ?",
                               (acct["id"], hour_ago)).fetchone()[0]
        if by_name >= web["mail_per_ip_per_hour"]:
            return Result(False, "slow", "You have sent enough letters for now. Try again later.")
    everyone = conn.execute("SELECT COUNT(*) FROM letters WHERE received_at >= ?", (hour_ago,)).fetchone()[0]
    if everyone >= web["mail_per_hour"]:
        return Result(False, "full", "The mailbox is full for now. Try again later.")
    with db.transaction(conn):
        if acct is None:                                  # the first letter under a name claims it
            salt = secrets.token_bytes(16)
            cur = conn.execute("INSERT INTO accounts(handle, handle_key, salt, secret_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                               (sender, sender.lower(), salt.hex(), _hash(secret, salt), now.isoformat(timespec="seconds")))
            account_id, handle = cur.lastrowid, sender
        else:
            account_id, handle = acct["id"], acct["handle"]
        cur = conn.execute("INSERT INTO letters(received_at, sender, text, visitor, account_id) VALUES (?, ?, ?, ?, ?)",
                           (now.isoformat(timespec="seconds"), handle, text, visitor, account_id))
    return Result(True, "ok", "Your letter is in the mailbox at the market stall.", cur.lastrowid)


def new_letters(mail: sqlite3.Connection, after_id: int, limit: int = 20) -> list[sqlite3.Row]:
    return mail.execute("SELECT id, received_at, sender, text FROM letters WHERE id > ? ORDER BY id LIMIT ?",
                        (after_id, limit)).fetchall()


def letter_key(letter_id: int) -> str:
    """The id a letter has in the mailbox fact."""
    return f"L{letter_id}"

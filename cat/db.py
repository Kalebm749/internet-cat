"""SQLite helpers shared by every database (nit.db, mail.db). The schema of the life is in cat/store.py.

The engine is the only writer of nit.db; the website and the dev dashboard open it read-only."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


def connect(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    """Open a database. isolation_level=None: transactions are explicit, via `transaction()`."""
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=10,
                               check_same_thread=False)
    else:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing block. A crash or exception leaves the previous state intact."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def backup(conn: sqlite3.Connection, dest_dir: Path, label: str, keep: int = 10, prefix: str = "nit") -> Path:
    """A consistent copy (SQLite online backup API), integrity-checked; keeps the newest `keep` with this label."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{prefix}-{datetime.now():%Y-%m-%d-%H%M%S}-{label}.db"
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
        if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup failed its integrity check")
    finally:
        out.close()
    for old in sorted(dest_dir.glob(f"{prefix}-*-{label}.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return dest


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)

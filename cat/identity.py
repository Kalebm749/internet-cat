"""The character sheet as numbered versions (docs/rewrite-contracts.md §2.1, §5.6).

At the first start `content/character.yaml` becomes version 1. From then on the database is the source of
truth for the mutable part (traits, tendencies, background, likes, dislikes): only character development
changes it, one small change at a time, each a new version with its reason and evidence. The author's part
(name, pronoun, core) always follows the content file: when the author edits it, a new version records that.

This module stores versions and applies one change safely (`apply_change`). Whether a change is allowed
(evidence, size, conflicts) is decided by the sleep stage that proposes it (step 5).
"""
from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from cat.content import Content

AUTHOR_KEYS = ("name", "pronoun", "core", "temperament", "holds")
MUTABLE_KEYS = ("traits", "tendencies", "background", "likes", "dislikes", "acquired")
CHANGE_KINDS = ("add_trait", "adjust_trait", "remove_trait", "add_tendency", "adjust_tendency",
                "remove_tendency", "background_note", "add_acquired", "drop_acquired")
ACQUIRED = ("habits", "fears", "interests")      # what it has added to itself, each with the evidence it came from


class IdentityError(ValueError):
    pass


@dataclass
class Version:
    version: int
    sheet: dict
    created_at: str
    reason: str
    change: dict | None = None
    evidence: list = field(default_factory=list)


@dataclass
class SyncReport:
    version: int
    created: bool = False                                         # version 1 was written now
    author_changes: list[str] = field(default_factory=list)      # author keys that changed (new version)
    ignored_seed_changes: list[str] = field(default_factory=list)  # mutable keys edited in the file after spawn


def _plain(v: Any) -> Any:
    """Content dicts remember YAML lines; store plain JSON values."""
    return json.loads(json.dumps(v))


def seed_sheet(content: Content) -> dict:
    ch = content.character
    sheet = {k: _plain(ch.get(k)) for k in AUTHOR_KEYS if k not in ("temperament", "holds")}
    sheet["temperament"] = _plain(ch.get("temperament") or {})
    sheet["holds"] = _plain(ch.get("holds") or [])
    sheet.update({"traits": _plain(ch.get("traits") or {}), "tendencies": _plain(ch.get("tendencies") or {}),
                  "background": ch.get("background") or "", "likes": _plain(ch.get("likes") or {}),
                  "dislikes": _plain(ch.get("dislikes") or {}), "acquired": {k: [] for k in ACQUIRED}})
    return sheet


def _row(r: sqlite3.Row) -> Version:
    return Version(version=r["version"], sheet=json.loads(r["sheet"]), created_at=r["created_at"],
                   reason=r["reason"], change=json.loads(r["change"]) if r["change"] else None,
                   evidence=json.loads(r["evidence"]))


def current(conn: sqlite3.Connection) -> Version | None:
    r = conn.execute("SELECT * FROM character_versions ORDER BY version DESC LIMIT 1").fetchone()
    return _row(r) if r else None


def history(conn: sqlite3.Connection) -> list[Version]:
    return [_row(r) for r in conn.execute("SELECT * FROM character_versions ORDER BY version")]


def _insert(conn: sqlite3.Connection, version: int, sheet: dict, sim_time: str, reason: str,
            change: dict | None = None, evidence: list | None = None) -> Version:
    conn.execute("INSERT INTO character_versions(version, sheet, created_at, reason, change, evidence) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (version, json.dumps(sheet, ensure_ascii=False), sim_time, reason,
                  json.dumps(change, ensure_ascii=False) if change else None,
                  json.dumps(evidence or [], ensure_ascii=False)))
    return Version(version, sheet, sim_time, reason, change, list(evidence or []))


def ensure(conn: sqlite3.Connection, content: Content, sim_time: str) -> SyncReport:
    """Write version 1 if there is none; record author edits of name/pronoun/core as a new version.
    Edits to the mutable part of the file after spawn are reported and ignored. Run inside a transaction."""
    seed = seed_sheet(content)
    cur = current(conn)
    if cur is None:
        _insert(conn, 1, seed, sim_time, "spawn")
        return SyncReport(version=1, created=True)
    rep = SyncReport(version=cur.version)
    changed = [k for k in AUTHOR_KEYS if cur.sheet.get(k) != seed[k]]
    if changed:
        sheet = copy.deepcopy(cur.sheet)
        for k in changed:
            sheet[k] = seed[k]
        v = _insert(conn, cur.version + 1, sheet, sim_time, "author edit: " + ", ".join(changed))
        rep.version, rep.author_changes = v.version, changed
    first = history(conn)[0]
    rep.ignored_seed_changes = [k for k in MUTABLE_KEYS if first.sheet.get(k) != seed[k]]
    return rep


def _strength(v: Any) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1:
        raise IdentityError(f"strength must be a number between 0 and 1, got {v!r}")
    return float(v)


def apply_change(sheet: dict, change: dict) -> dict:
    """Return a new sheet with one change applied. Never touches name, pronoun or core.

    change = {kind, target, value}:
      add_trait / adjust_trait       target: trait name, value: strength 0..1
      remove_trait                   target: trait name
      add_tendency / adjust_tendency target: tendency words, value: strength 0..1
      remove_tendency                target: tendency words
      background_note                value: one sentence appended to the background
    """
    kind = change.get("kind")
    if kind not in CHANGE_KINDS:
        raise IdentityError(f"unknown change kind {kind!r}")
    new = copy.deepcopy(sheet)
    target = change.get("target")
    if kind in ("add_acquired", "drop_acquired"):
        value = change.get("value")
        if target not in ACQUIRED or not isinstance(value, dict) or not value.get("key") or not value.get("text"):
            raise IdentityError(f"{kind} needs a target in {ACQUIRED} and a value with key and text")
        items = new.setdefault("acquired", {k: [] for k in ACQUIRED}).setdefault(target, [])
        have = [i for i in items if i.get("key") == value["key"]]
        if kind == "add_acquired":
            if have:
                raise IdentityError(f"'{value['key']}' is already among its {target}")
            items.append({"key": value["key"], "text": value["text"], "since": change.get("since")})
        else:
            if not have:
                raise IdentityError(f"'{value['key']}' is not among its {target}")
            items[:] = [i for i in items if i.get("key") != value["key"]]
    elif kind == "background_note":
        note = change.get("value")
        if not isinstance(note, str) or not note.strip():
            raise IdentityError("background_note needs a sentence")
        new["background"] = (new.get("background", "").rstrip() + " " + note.strip()).strip()
    else:
        section, noun = ("traits", "trait") if kind.endswith("_trait") else ("tendencies", "tendency")
        table = new.setdefault(section, {})
        if not isinstance(target, str) or not target.strip():
            raise IdentityError(f"{kind} needs a target")
        if kind.startswith("add_"):
            if target in table:
                raise IdentityError(f"{noun} '{target}' already exists")
            table[target] = _strength(change.get("value"))
        elif kind.startswith("adjust_"):
            if target not in table:
                raise IdentityError(f"no {noun} '{target}'")
            table[target] = _strength(change.get("value"))
        else:
            if target not in table:
                raise IdentityError(f"no {noun} '{target}'")
            del table[target]
    for k in AUTHOR_KEYS:
        if new.get(k) != sheet.get(k):
            raise IdentityError(f"a change may not touch '{k}'")
    return new


def commit_change(conn: sqlite3.Connection, change: dict, reason: str, evidence: list, sim_time: str) -> Version:
    """Apply one change to the current version and store the result as a new version (inside a transaction)."""
    cur = current(conn)
    if cur is None:
        raise IdentityError("no character yet")
    return _insert(conn, cur.version + 1, apply_change(cur.sheet, change), sim_time, reason, change, evidence)

"""World state (docs/rewrite-contracts.md §4.2): the current value of every fact.

The world is locations + facts + the clock. A `WorldState` holds the facts in memory; `apply` runs world
ops all-or-nothing (cat/ops.py) and marks facts the creature changed as `touched`; `regrow` advances
resources over sim time; `load`/`save` keep them in `nit.db`. On load the stored facts are brought in step
with the content: new facts get their defaults, undeclared facts are removed, a fact the creature never
touched follows its default when the content changed that default (otherwise it keeps its value, e.g. the
weather or a regrown resource), and a touched fact keeps its value unless it no longer fits the fact's type.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from cat import clock, ops
from cat.content import Content, FactSpec

log = logging.getLogger("cat.worldstate")


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)     # untouched facts that followed a new default
    reset: list[str] = field(default_factory=list)       # touched facts whose value no longer fit, reset

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.updated or self.reset)


def _clamp(spec: FactSpec, v: Any) -> Any:
    if spec.type in ("int", "float"):
        if spec.min is not None:
            v = max(spec.min, v)
        if spec.max is not None:
            v = min(spec.max, v)
        return int(round(v)) if spec.type == "int" else float(v)
    if spec.type == "list" and spec.max_len is not None and len(v) > spec.max_len:
        return v[len(v) - spec.max_len:]
    return v


class WorldState:
    def __init__(self, content: Content, values: Mapping[str, Any], touched: set[str] | None = None,
                 seq: Mapping[str, int] | None = None):
        self.content = content
        self.values: dict[str, Any] = dict(values)
        self.touched: set[str] = set(touched or ())
        self.seq: dict[str, int] = dict(seq or {})
        self.dirty: set[str] = set()          # changed since the last save

    @classmethod
    def fresh(cls, content: Content) -> "WorldState":
        ws = cls(content, {k: copy.deepcopy(s.default) for k, s in content.facts.items()})
        ws.dirty = set(ws.values)
        return ws

    def get(self, key: str) -> Any:
        return self.values[key]

    def visible(self, location_id: str) -> dict[str, Any]:
        """Facts of a location (and the world) the creature may see, by their `visible` setting."""
        out = {}
        for k, spec in self.content.facts.items():
            if spec.visible == "none" or not (k.startswith(location_id + ".") or k.startswith("world.")):
                continue
            v = self.values[k]
            if spec.visible == "summary" and isinstance(v, list):
                v = {"count": len(v)}
            out[k] = v
        return out

    # ---------------------------------------------------------------- changes

    def apply(self, world_ops: list[Mapping], vars: dict[str, Any], rng: random.Random,
              by: str = "creature") -> ops.Applied:
        """Apply world ops all-or-nothing (raises ops.OpError and changes nothing on failure).
        `by='creature'` marks changed facts as touched; `by='world'` (events, regrowth) does not."""
        res = ops.apply_all(world_ops, self.values, self.content.facts, vars, rng, self.seq)
        self.values = res.facts
        self.seq = res.seq
        self.dirty.update(res.changed)
        if by == "creature":
            self.touched.update(res.changed)
        return res

    def append_raw(self, key: str, item: Any, by: str = "world") -> None:
        """Append an outside value to a list fact WITHOUT filling placeholders (a visitor's letter may contain
        braces; it must never be read as a template). max_len still applies."""
        spec = self.content.facts[key]
        if spec.type != "list":
            raise ops.OpError(f"append_raw: fact '{key}' is not a list")
        self.values[key] = _clamp(spec, self.values[key] + [copy.deepcopy(item)])
        self.dirty.add(key)
        if by == "creature":
            self.touched.add(key)

    def regrow(self, since: str, until: str) -> dict[str, float]:
        """Advance every regrowing fact from sim time `since` to `until`. Returns key -> amount added."""
        a, b = clock.parse(since), clock.parse(until)
        added: dict[str, float] = {}
        if b <= a:
            return added
        for k, spec in self.content.facts.items():
            r = spec.regrow
            if not r:
                continue
            n = _crossings(r, a, b)
            if n:
                before = self.values[k]
                self.values[k] = _clamp(spec, before + n * r["amount"])
                if self.values[k] != before:
                    added[k] = self.values[k] - before
                    self.dirty.add(k)
        return added

    # ---------------------------------------------------------------- persistence

    @classmethod
    def load(cls, conn: sqlite3.Connection, content: Content) -> tuple["WorldState", SyncReport]:
        """Read facts from the DB and bring them in step with the content. Call `save` afterwards to
        store the sync (the caller decides the transaction)."""
        rows = {r["key"]: r for r in conn.execute("SELECT key, value, base, touched, seq FROM facts")}
        rep = SyncReport()
        values, touched, seq = {}, set(), {}
        for k, spec in content.facts.items():
            row = rows.get(k)
            if row is None:
                values[k] = copy.deepcopy(spec.default)
                rep.added.append(k)
                continue
            v = json.loads(row["value"])
            seq[k] = row["seq"]
            if row["touched"]:
                if ops.type_ok(spec, v):
                    values[k] = _clamp(spec, v)
                    touched.add(k)
                else:
                    values[k] = copy.deepcopy(spec.default)
                    rep.reset.append(k)
                    log.warning("fact %s: stored value no longer fits %s; reset to its default", k, spec.type)
            elif json.loads(row["base"]) != spec.default or not ops.type_ok(spec, v):
                values[k] = copy.deepcopy(spec.default)
                rep.updated.append(k)
            else:
                values[k] = _clamp(spec, v)
                if values[k] != v:
                    rep.updated.append(k)
        rep.removed = sorted(set(rows) - set(content.facts))
        ws = cls(content, values, touched, seq)
        # rows whose stored base differs (a default changed back and forth) are rewritten too
        ws.dirty = set(rep.added) | set(rep.updated) | set(rep.reset) | {
            k for k, row in rows.items() if k in content.facts and json.loads(row["base"]) != content.facts[k].default}
        ws._removed = rep.removed
        return ws, rep

    def save(self, conn: sqlite3.Connection, all: bool = False) -> int:
        """Write changed facts (or all of them) and delete removed ones. Run inside a transaction."""
        keys = sorted(self.values) if all else sorted(self.dirty)
        for k in keys:
            conn.execute(
                "INSERT INTO facts(key, value, base, touched, seq) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, base=excluded.base, "
                "touched=excluded.touched, seq=excluded.seq",
                (k, _dump(self.values[k]), _dump(self.content.facts[k].default), int(k in self.touched),
                 self.seq.get(k, 0)))
        for k in getattr(self, "_removed", []):
            conn.execute("DELETE FROM facts WHERE key = ?", (k,))
        self._removed = []
        self.dirty.clear()
        return len(keys)


def _dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _crossings(r: Mapping[str, Any], a: datetime, b: datetime) -> int:
    """How many regrowth moments fall in (a, b]."""
    if "every_hours" in r:
        step = r["every_hours"] * 3600
        epoch = datetime(2000, 1, 1)
        return math.floor((b - epoch).total_seconds() / step) - math.floor((a - epoch).total_seconds() / step)
    n = 0
    day = datetime(a.year, a.month, a.day)
    while day <= b:
        for h in r["at_hours"]:
            t = day.replace(hour=int(h), minute=int(round((h - int(h)) * 60)))
            if a < t <= b:
                n += 1
        day = day.fromordinal(day.toordinal() + 1)
    return n

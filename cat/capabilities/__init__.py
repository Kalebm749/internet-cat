"""Capabilities (docs/rewrite-contracts.md §4.6): code features actions can use (reading a letter, drawing,
writing, web search ...). Each is a module named by `handler` in content/capabilities.yaml that defines
`CAPABILITY`, an object with:

    name: str
    run(ctx: CapContext, inputs: dict) -> CapabilityResult     # may read the world, call an LLM or the network;
                                                                # never writes the database
A capability is usable when it is enabled, its module loads, and it is under its `limits.per_day` (sim day).
`run` happens outside the database transaction; its result's world ops and creature effects are applied and
validated like any other effect.
"""
from __future__ import annotations

import importlib
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol

from cat.content import Content, ContentError

log = logging.getLogger("cat.capabilities")


@dataclass
class CapContext:
    content: Content
    facts: dict                  # current world facts (read only)
    location: str
    sim_time: str
    texts: dict                  # config/prompts/world.yaml: fixed wording
    backend: Any = None          # the LLM backend, for capabilities that call a model
    tuning: dict | None = None
    artifact: Any = None         # artifact(id) -> the stored version (dict with meta and data) or None; read only
    temperament: dict | None = None   # the creature's temperament numbers (subjective judgements depend on them)


@dataclass
class CapabilityResult:
    ok: bool = True
    text: str = ""                                          # what the creature perceives
    fields: dict = field(default_factory=dict)              # available to content as {capability.<field>}
    artifact: dict | None = None                            # something made, kept and shown (later steps)
    world: list = field(default_factory=list)               # extra world ops (validated when applied)
    creature: dict = field(default_factory=dict)            # extra creature effects
    inner: dict = field(default_factory=dict)               # extra inner effects (needs and affect, cat/inner.py)
    external: bool | None = None                            # override handler default for this particular result


class Capability(Protocol):
    name: str
    external: bool        # True: its result is someone else's words (a letter), never evidence of who it is

    def run(self, ctx: CapContext, inputs: dict) -> CapabilityResult: ...


class Registry:
    """Loads the handlers of enabled capabilities that some action uses. A handler that cannot be loaded is a
    content error at start-up, not a surprise later."""

    def __init__(self, content: Content):
        self.content = content
        self.handlers: dict[str, Capability] = {}
        used = {a.capability for a in content.actions.values() if a.capability}
        probs = []
        for name in sorted(used):
            spec = content.capabilities.get(name) or {}
            if not spec.get("enabled"):
                continue
            try:
                mod = importlib.import_module(spec["handler"])
                self.handlers[name] = mod.CAPABILITY
            except (ImportError, AttributeError) as e:
                probs.append(f"capabilities.yaml:1: capability '{name}': handler {spec['handler']!r} cannot be loaded ({e})")
        if probs:
            raise ContentError(probs)

    def is_external(self, name: str | None) -> bool:
        return bool(name) and bool(getattr(self.handlers.get(name), "external", False))

    def limit(self, name: str) -> int | None:
        return ((self.content.capabilities.get(name) or {}).get("limits") or {}).get("per_day")

    def uses(self, conn: sqlite3.Connection, name: str, day: str) -> int:
        r = conn.execute("SELECT uses FROM capability_usage WHERE capability = ? AND day = ?", (name, day)).fetchone()
        return r[0] if r else 0

    def available(self, conn: sqlite3.Connection, name: str, day: str) -> tuple[bool, str]:
        if name not in self.handlers:
            return False, "disabled"
        lim = self.limit(name)
        if lim is not None and self.uses(conn, name, day) >= lim:
            return False, f"used {lim} times today"
        return True, ""

    def run(self, name: str, ctx: CapContext, inputs: dict) -> CapabilityResult:
        try:
            res = self.handlers[name].run(ctx, inputs)
        except Exception as e:                    # a capability failing must never break the creature
            log.warning("capability %s failed: %s", name, e, exc_info=True)
            return CapabilityResult(ok=False, text=ctx.texts["capability_failed"])
        return res

    def record_use(self, conn: sqlite3.Connection, name: str, day: str) -> None:
        conn.execute("INSERT INTO capability_usage(capability, day, uses) VALUES (?, ?, 1) "
                     "ON CONFLICT(capability, day) DO UPDATE SET uses = uses + 1", (name, day))

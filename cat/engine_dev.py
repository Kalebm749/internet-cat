"""Dashboard commands, injected events and the world director. Part of `cat.engine.Engine` (a mixin: the methods use the engine's state)."""
from __future__ import annotations

import json
import logging
import random
import signal
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from typing import Callable

from cat import brain, character, clock, db, downtime, effects, impulses, journal, logs, sleep
from cat import harms
from cat import moves as MV
from cat import drawings as DW
from cat import director as DR
from cat import habits as HB
from cat import projects as PJ
from cat import prompts as P
from cat.config import Config, load_config
from cat.devcontrol import DevControl
from cat.devqueue import CommandError
from cat.db import dumps
from cat.healthcheck import write_heartbeat
from cat.llm import BackendError, LLMBackend
from cat.memory import Memory, build_query, importance_for, notable_reasons
from cat.trace import Tracer, prune
from cat.world import WorldState

log = logging.getLogger("cat.engine")
_now_real = clock.now_real


class DevMixin:
    # ---------- dev commands that need engine internals ----------
    def force_sleep_cycle(self) -> str:
        """Run the sleep cycle now (consolidate, drift tendencies, learn preferences) without
        waiting for the creature to be tired. Needs the brain online; attaches to the latest tick."""
        last = self.conn.execute("SELECT MAX(id) FROM ticks").fetchone()[0]
        if not last:
            raise RuntimeError("no ticks yet: nothing to consolidate")
        self._online_cache = None
        if not self._online():
            raise RuntimeError("brain is offline: a sleep cycle needs it")
        self.tracer.begin()
        with db.transaction(self.conn):
            db.set_meta(self.conn, "sleep_pending", 1)
            db.set_meta(self.conn, "sleep_tick_id", last)
            if not db.get_meta(self.conn, "sleep_started"):
                db.set_meta(self.conn, "sleep_started", self.state["sim_time"])
        self._try_sleep_cycle()
        done = db.get_meta(self.conn, "sleep_pending") == "0"
        self.tracer.flush(self.conn, last, "ok")
        if not done:
            raise RuntimeError("postponed: the brain call failed (see the LLM calls page); it stays pending")
        row = self.conn.execute("SELECT id, summary, new_memories, tendency_changes FROM sleep_log "
                                "ORDER BY id DESC LIMIT 1").fetchone()
        return (f"sleep cycle done (night #{row['id']}): {len(json.loads(row['new_memories']))} new memories, "
                f"{len(json.loads(row['tendency_changes']))} tendency changes")

    def reload_settings(self) -> list[str]:
        """Re-read the YAML (all four files are validated). Settings and prompts are applied live; a
        change to character.yaml / world.yaml needs a restart. Invalid YAML raises and changes nothing."""
        new = load_config(self.cfg.config_dir, self.cfg.overrides)
        old = dict(self.cfg.settings)
        changed = sorted(k for k in set(old) | set(new.settings) if old.get(k) != new.settings.get(k))
        self.cfg.settings.clear()
        self.cfg.settings.update(new.settings)             # same dict object: every component sees it
        self.cfg.prompts.clear()
        self.cfg.prompts.update(new.prompts)               # prompt text is live too
        self.cfg.moves.clear()
        self.cfg.moves.update(new.moves)                   # and so are the moves
        self.dev.publish_config(("settings", "prompts", "moves"))
        log.info("settings reloaded; changed: %s", changed, extra={"event": "settings_reloaded",
                                                                   "extra": {"changed": changed}})
        return changed

    def snapshot_db(self) -> Path:
        """Back up the database now (SQLite online backup API, safe while running)."""
        dest_dir = Path(self.cfg.db_path).parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"cat-{datetime.now():%Y-%m-%d-%H%M%S}-snapshot.db"
        out = sqlite3.connect(dest)
        try:
            self.conn.backup(out)
            if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("snapshot failed its integrity check")
        finally:
            out.close()
        return dest

    def queue_injected_event(self, args: dict) -> None:
        """Add a custom world event to the next tick (text, optional object to spawn)."""
        areas = self.cfg.world["areas"]
        if args.get("area") and args["area"] not in areas:
            raise CommandError(f"unknown area '{args['area']}' (areas: {', '.join(areas)})")
        sp = args.get("spawn")
        if sp:
            sp = {**sp, "area": sp.get("area") or args.get("area") or self.state["area"]}
            if sp["area"] not in areas:
                raise CommandError(f"unknown spawn area '{sp['area']}'")
            allowed = self.cfg.settings["allowed_tags"]
            sp["tags"] = [t for t in sp["tags"] if t in allowed]
            if len(sp["name"]) > self.cfg.settings["name_max_length"]:
                raise CommandError("spawn name too long")
        self.injected.append({"text": args["text"], "area": args.get("area"), "spawn": sp})

    def _director_happening(self, fired: list[dict], st: dict, now: str, dt) -> list[dict]:
        """The world director: now and then an LLM proposes one small happening in this area. It runs
        every `director.every_ticks` action ticks, or `stale_every_ticks` when the place has gone stale;
        never while asleep or exhausted, never with the brain offline, and it counts toward
        `events_max_per_tick`. A failed call is only a missed happening: the tick carries on.
        It never sees the entity's needs or mood. Code validates the proposal and applies it as an event."""
        cfg, s = self.cfg, self.cfg.settings
        d = s["director"]
        if not d["enabled"] or len(fired) >= s["events_max_per_tick"]:
            return []
        if st["is_asleep"] or (clock.is_night(dt, s["phases"]) and st["energy"] < s["sleep"]["fall_asleep_energy_below"]):
            return []
        gap = int(db.get_meta(self.conn, "director_gap", "0") or 0)
        due, why = DR.due(cfg, gap, self._stale_here())
        if not due or not self._online():
            return []
        area = st["area"]
        rec: dict = {"trigger": why, "gap": gap}
        out: dict = {"action": "failed"}
        with self.tracer.stage("director"):
            recent = [r["text"] for r in self.conn.execute(
                "SELECT text FROM events ORDER BY id DESC LIMIT ?", (d["avoid_repeat"],))][::-1] \
                if d["avoid_repeat"] else []
            acts = [r["action"] for r in self.conn.execute(
                "SELECT action FROM ticks WHERE kind='tick' ORDER BY id DESC LIMIT ?",
                (d["recent_actions_shown"],))][::-1] if d["recent_actions_shown"] else []
            msgs = DR.messages(cfg, area_id=area, sim_time=now, weather=self.world.weather,
                               objects=[o["name"] for o in self.world.here(area)],
                               recent_events=recent, recent_actions=acts, stale=self._stale_here())
            try:
                raw = DR.propose(self.backend, cfg, msgs)
                out = DR.validate(cfg, raw, recent, len(self.world.here(area)))
                rec["raw"] = raw
            except BackendError as e:
                log.warning("world director unavailable (%s): no happening this time", e)
                out = {"action": "failed", "reason": str(e)}
        db.set_meta(self.conn, "director_gap", 0)
        self.tracer.set_director({**rec, **out})
        if out["action"] != "happened":
            return []
        record = {"text": out["text"], "area": area, "mood": out["mood"], "spawned": None,
                  "drains": out["drains"], "director": True}
        if out["weather"] and out["weather"] != self.world.weather:
            self.world.weather = out["weather"]
        if out["spawn"]:
            o = self.world.add_object(out["spawn"]["name"], out["spawn"]["tags"], area, "director", now)
            record["spawned"] = o["id"]
            record["spawn_info"] = {"op": "create", "source": "director", "object_id": o["id"],
                                    "area": o["area"], "name": o["name"], "label": o["name"]}
        return [record]

    def _take_injected(self, now: str, rolls: list[dict]) -> list[dict]:
        """Turn queued dev events into fired events (same shape roll_events returns)."""
        out = []
        while self.injected:
            ev = self.injected.pop(0)
            rec = {"text": ev["text"], "area": ev["area"], "mood": 0.0, "spawned": None, "injected": True}
            sp = ev["spawn"]
            if sp and len(self.world.here(sp["area"])) < self.cfg.settings["max_objects_per_area"]:
                o = self.world.add_object(sp["name"], sp["tags"], sp["area"], "event", now)
                rec["spawned"] = o["id"]
                rec["spawn_info"] = {"op": "create", "source": "event", "object_id": o["id"],
                                     "area": o["area"], "name": o["name"], "label": o["name"]}
            rolls.append({"event_text": ev["text"], "chance": None, "roll": None, "fired": 1,
                          "filtered_reason": "injected from the dev dashboard"
                          + ("" if not sp or rec["spawned"] else " (spawn skipped: area full)")})
            out.append(rec)
        return out

    def install_signal_handlers(self) -> None:
        def _stop(signum, frame):
            log.info("signal %s: stopping after this tick", signum)
            self.stop_requested = True
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

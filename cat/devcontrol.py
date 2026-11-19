"""Runs dev-dashboard commands inside the engine (the only writer of game state).

The dashboard inserts rows into dev.db's `dev_commands`; the engine polls them at the start of every
loop iteration (and every `dev_poll_seconds` while waiting or paused), runs them here and records
the result. It also publishes `engine_status` and the loaded config back to dev.db."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from cat.devqueue import CommandError, DevQueue, validate

if TYPE_CHECKING:
    from cat.engine import Engine

log = logging.getLogger("cat.dev")
CONFIG_FILES = ("settings", "character", "world")


class DevControl:
    def __init__(self, engine: "Engine"):
        self.e = engine
        self.q: DevQueue | None = None
        self._last_publish = 0.0
        if engine.cfg.settings["dev_commands_enabled"]:
            try:
                self.q = DevQueue(engine.cfg.dev_db_path)
            except Exception as ex:          # e.g. read-only data dir: the creature must still live
                log.warning("dev command queue unavailable (%s); dev controls disabled", ex)

    @property
    def enabled(self) -> bool:
        return self.q is not None

    # ---------- commands ----------
    def process(self) -> int:
        """Run every pending command. Never raises. Returns how many were handled."""
        if self.q is None:
            return 0
        n = 0
        try:
            pending = self.q.pending()
        except Exception:
            log.warning("could not read the dev command queue", exc_info=True)
            return 0
        for cmd in pending:
            n += 1
            try:
                self.q.mark(cmd["id"], "running")
                status, result = self._execute(cmd)
            except Exception as ex:
                log.warning("dev command %s failed", cmd["command"], exc_info=True)
                status, result = "failed", f"{type(ex).__name__}: {ex}"
            try:
                self.q.mark(cmd["id"], status, result)
            except Exception:
                log.warning("could not record dev command result", exc_info=True)
            log.info("dev command %s -> %s: %s", cmd["command"], status, result,
                     extra={"event": "dev_command", "extra": {"id": cmd["id"], "command": cmd["command"],
                                                              "status": status, "result": result}})
        return n

    def _execute(self, cmd) -> tuple[str, str]:
        e, name = self.e, cmd["command"]
        try:
            args = validate(name, json.loads(cmd["args"] or "{}"))
        except (CommandError, ValueError) as ex:
            return "rejected", str(ex)
        if name == "pause":
            if e.paused:
                return "done", "already paused"
            e.paused = True
            e._refresh_alive()                    # a pause is not downtime
            return "done", "paused: the loop waits for step or resume"
        if name == "resume":
            if not e.paused:
                return "done", "was not paused"
            e.paused = False
            e.step_pending = False
            return "done", "resumed"
        if name == "step":
            if not e.paused:
                return "rejected", "step only works while paused: pause first"
            if e.step_pending:
                return "rejected", "a step is already queued"
            e.step_pending, e.step_cmd_id = True, cmd["id"]
            return "running", "waiting for the tick to run"
        if name == "set_interval":
            e.interval_override, e.interval_override_ticks = args["seconds"], args["ticks"]
            if args["seconds"] is None:
                return "done", "interval back to settings.yaml"
            tail = f" for {args['ticks']} tick(s)" if args["ticks"] else " until reset or restart"
            return "done", f"tick interval is now {args['seconds']}s{tail}"
        if name == "force_sleep":
            return "done", e.force_sleep_cycle()
        if name == "reload_settings":
            changed = e.reload_settings()
            return "done", ("settings reloaded; changed: " + ", ".join(changed)) if changed else "settings reloaded; nothing changed"
        if name == "inject_event":
            e.queue_injected_event(args)
            return "done", "queued: it will happen on the next tick"
        if name == "snapshot":
            path = e.snapshot_db()
            return "done", f"{path} ({path.stat().st_size // 1024} KB)"
        return "rejected", f"unhandled command {name}"

    def finish_step(self, cmd_id: int | None, info: dict | None, error: str | None = None) -> None:
        if self.q is None or cmd_id is None:
            return
        try:
            if info is not None:
                self.q.mark(cmd_id, "done", f"ran tick #{info['id']} ({info['kind']})")
            else:
                self.q.mark(cmd_id, "failed", error or "the tick raised an error")
        except Exception:
            log.warning("could not record step result", exc_info=True)

    # ---------- what the engine publishes ----------
    def publish(self, force: bool = False, state: str | None = None) -> None:
        """Write engine_status (rate-limited unless forced). Never raises."""
        if self.q is None:
            return
        now = time.time()
        if not force and now - self._last_publish < 1.0:
            return
        self._last_publish = now
        e = self.e
        st = e.state
        try:
            if state is None:
                state = "paused" if e.paused else "asleep" if st["is_asleep"] else \
                    "running" if st["brain_online"] else "dozing"
            self.q.set_status({
                "state": state, "paused": e.paused, "step_pending": e.step_pending,
                "sim_time": st["sim_time"], "area": st["area"], "brain_online": bool(st["brain_online"]),
                "interval_seconds": e.effective_interval(),
                "interval_override": e.interval_override, "interval_override_ticks": e.interval_override_ticks,
                "last_tick_id": e.last_tick_id, "last_tick_ms": e.last_tick_ms,
                "downtime_policy": e.cfg.settings["downtime_policy"], "trace_level": e.cfg.settings["trace_level"],
                "pid": __import__("os").getpid(), "started_at": e.started_at, "heartbeat": now,
                "injected_queued": len(e.injected)})
        except Exception:
            log.warning("could not publish engine status", exc_info=True)

    def publish_config(self, names: tuple[str, ...] = CONFIG_FILES) -> None:
        """Record the config exactly as the engine loaded it, plus a hash of the raw files. After a
        live reload only `settings` is republished: character/world stay as loaded until a restart,
        so the dashboard keeps flagging them as changed on disk."""
        if self.q is None:
            return
        cfg = self.e.cfg
        try:
            for name in names:
                data = getattr(cfg, name) if name != "settings" else cfg.settings
                raw = (Path(cfg.config_dir) / f"{name}.yaml")
                sha = hashlib.sha256(raw.read_bytes()).hexdigest() if raw.exists() else ""
                self.q.set_config(name, yaml.safe_dump(data, sort_keys=False, allow_unicode=True), sha)
        except Exception:
            log.warning("could not publish config snapshot", exc_info=True)

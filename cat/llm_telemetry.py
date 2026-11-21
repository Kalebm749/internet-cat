"""Small cross-process status file for the engine and read-only developer dashboard."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from cat.healthcheck import write_heartbeat


class LLMCallMonitor:
    """Persist call summaries for the dev dashboard, with what went in and came out of each call (the `io` field,
    switched off by settings `llm_trace_io: false`). The dashboard is loopback or password-protected and read-only."""

    def __init__(self, path: Path, heartbeat_path: Path | None = None):
        self.path = path
        self.heartbeat_path = heartbeat_path
        self._lock = threading.Lock()
        self._active: dict[str, dict] = {}
        self._recent: list[dict] = []
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None

    def __call__(self, event: dict[str, Any]) -> None:
        try:
            with self._lock:
                row = dict(event)
                call_id = str(row.get("id", ""))
                if row.get("status") == "running":
                    self._active[call_id] = row
                    self._start_heartbeat()
                else:
                    previous = self._active.pop(call_id, {})
                    self._recent.insert(0, {**previous, **row})
                    self._recent = self._recent[:30]
                    if not self._active:
                        self._stop_heartbeat()
                payload = {"updated_at": datetime.now().isoformat(timespec="seconds"),
                           "active": list(self._active.values()), "recent": self._recent}
                self._write(payload)
        except Exception:
            # Telemetry must never stall or fail Nit.
            return

    def _start_heartbeat(self) -> None:
        if self.heartbeat_path is None:
            return
        if (self._heartbeat_stop is not None and not self._heartbeat_stop.is_set()
                and self._heartbeat_thread is not None and self._heartbeat_thread.is_alive()):
            return
        stop = threading.Event()
        self._heartbeat_stop = stop

        def keep_alive():
            while not stop.wait(10):
                write_heartbeat(self.heartbeat_path)

        thread = threading.Thread(target=keep_alive, name="llm-heartbeat", daemon=True)
        self._heartbeat_thread = thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
            self._heartbeat_stop = None
            self._heartbeat_thread = None

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        os.replace(tmp, self.path)

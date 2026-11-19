"""Structured logging for every service: JSON lines to stdout (docker logs) AND to
<log_dir>/<service>.jsonl with size-based rotation.

Fields: ts, level, service, tick_id (if inside a tick), event, message, extra.
Log with `log.info("msg", extra={"event": "name", "extra": {"k": "v"}})`; `event` defaults to the
logger name. Switched on by `setup_logging` (the containers set CAT_STRUCTURED_LOGS=1)."""
from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime
from typing import IO

from cat.config import Config

_tick_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("tick_id", default=None)
_MARK = "_cat_structured"


def set_tick_id(value: int | None) -> None:
    _tick_id.set(value)


def structured_enabled() -> bool:
    return os.environ.get("CAT_STRUCTURED_LOGS", "") not in ("", "0", "false")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        extra = dict(getattr(record, "extra", None) or {})
        if record.exc_info:
            extra["exception"] = self.formatException(record.exc_info)
        tick = getattr(record, "tick_id", None)
        out = {"ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
               "level": record.levelname, "service": self.service,
               "tick_id": tick if tick is not None else _tick_id.get(),
               "event": getattr(record, "event", None) or record.name,
               "message": record.getMessage(), "extra": extra}
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(service: str, cfg: Config, stream: IO[str] | None = None) -> logging.Logger:
    """Install the JSON handlers on the root logger (replacing ones we installed before)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _MARK, False):
            root.removeHandler(h)
            h.close()
    fmt = JsonFormatter(service)
    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stdout)]
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            cfg.log_dir / f"{service}.jsonl", maxBytes=cfg.settings["log_max_bytes"],
            backupCount=cfg.settings["log_backup_count"], encoding="utf-8"))
    except OSError as e:                      # a read-only or missing data dir must not stop the service
        print(f"warning: cannot write log files in {cfg.log_dir}: {e}", file=sys.stderr)
    for h in handlers:
        h.setFormatter(fmt)
        setattr(h, _MARK, True)
        root.addHandler(h)
    root.setLevel(cfg.settings["log_level"])
    return root

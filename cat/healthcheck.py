"""Container healthchecks and the engine heartbeat.

  python -m cat.healthcheck engine          # is the engine loop alive? (heartbeat file age)
  python -m cat.healthcheck http <url>      # does the URL answer 2xx? (used for web)

The heartbeat is a small file the engine rewrites every loop iteration (not a DB write), so the
engine stays the only writer of game state and the check works while it is paused or dozing."""
from __future__ import annotations

import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from cat.config import Config, load_config


def write_heartbeat(path: Path) -> None:
    """Atomically stamp the file with the current time. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(datetime.now().isoformat(timespec="seconds"))
        os.replace(tmp, path)
    except OSError:
        pass


def heartbeat_age(path: Path) -> float | None:
    """Seconds since the last heartbeat, or None if there is none."""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def engine_limit(cfg: Config) -> float:
    return max(cfg.settings["healthcheck"]["engine_max_age_seconds"],
               3 * cfg.settings["tick_interval_seconds"])


def check_engine(cfg: Config) -> tuple[bool, str]:
    age = heartbeat_age(cfg.heartbeat_path)
    limit = engine_limit(cfg)
    if age is None:
        return False, f"no heartbeat at {cfg.heartbeat_path}"
    if age > limit:
        return False, f"heartbeat is {age:.0f}s old (limit {limit:.0f}s)"
    return True, f"heartbeat {age:.0f}s old"


def check_http(url: str, timeout: float) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300, f"HTTP {r.status}"
    except Exception as e:  # any failure = unhealthy
        return False, f"{type(e).__name__}: {e}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("engine", "http"):
        print(__doc__)
        return 2
    cfg = load_config()
    if argv[0] == "engine":
        ok, msg = check_engine(cfg)
    else:
        if len(argv) < 2:
            print("usage: python -m cat.healthcheck http <url>")
            return 2
        ok, msg = check_http(argv[1], cfg.settings["healthcheck"]["http_timeout_seconds"])
    print(("healthy: " if ok else "UNHEALTHY: ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

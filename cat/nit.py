"""Run the new engine (rewrite step 4). The old engine (`python -m cat.main`) is untouched until it is replaced.

    .venv/bin/python -m cat.nit --mock --fast --hours 24          # a mock sim day in seconds, into data/nit-mock.db
    .venv/bin/python -m cat.nit --ollama-host http://127.0.0.1:11434   # real time, real brain, data/nit.db
"""
from __future__ import annotations

import argparse
import logging
import sys

from cat import content, store, tuning
from cat.config import load_config
from cat.llm import MockBackend, OllamaBackend
from cat.loop import FakeClock, Loop


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", action="store_true", help="the mock brain instead of Ollama")
    ap.add_argument("--ollama-host")
    ap.add_argument("--db", help="database (default: settings nit.db_path; data/nit-mock.db with --mock)")
    ap.add_argument("--fast", action="store_true", help="do not wait: drive real time with a fake clock")
    ap.add_argument("--hours", type=float, help="stop after this many sim hours")
    ap.add_argument("--seed", type=int)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    t = tuning.load()
    c = content.load(tuning.path(t, "content_dir"))
    db = a.db or ("data/nit-mock.db" if a.mock else str(tuning.path(t, "db_path")))
    conn = store.connect(db)
    backend = MockBackend(seed=a.seed or 0) if a.mock else OllamaBackend(
        load_config(overrides={"ollama_host": a.ollama_host} if a.ollama_host else None))
    fc = FakeClock() if a.fast else None
    loop = Loop(conn, c, t, backend, clock=fc or __import__("time").monotonic, seed=a.seed)
    steps = None
    if a.hours:
        steps = int(a.hours * 60 * 60 / (float(t["time_scale"]) * float(t["loop"]["step_seconds"])))
    try:
        loop.run(steps, sleep=fc.sleep if fc else __import__("time").sleep)
    except KeyboardInterrupt:
        pass
    print(f"stopped at {loop.life.sim_time} in {loop.life.location}; database {db}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

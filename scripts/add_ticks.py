"""Append N mock ticks to an existing database (optionally with the brain 'offline'), e.g. to
watch the website update live:  scripts/add_ticks.py --db data/seed.db --ticks 3 [--offline]"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat.config import load_config, sibling_overrides  # noqa: E402
from cat.engine import Engine  # noqa: E402
from cat.llm import MockBackend  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--ticks", type=int, default=1)
    ap.add_argument("--offline", action="store_true", help="brain offline: the ticks are dozes")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--config-dir", default=None)
    a = ap.parse_args()
    cfg = load_config(a.config_dir, sibling_overrides(a.db))
    b = MockBackend(seed=a.seed or random.randrange(10 ** 6))
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.online = not a.offline
    Engine(cfg, b, rng=random.Random(a.seed)).run(ticks=a.ticks, interval=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

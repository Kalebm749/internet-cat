"""Engine entrypoint: loads config, runs the tick loop."""
from __future__ import annotations

import argparse
import logging
import random

from cat import clock, db
from cat.config import load_config
from cat.engine import Engine
from cat.llm import LLMBackend, MockBackend


def make_backend(cfg, mock: bool, seed: int | None) -> LLMBackend:
    if mock:
        b = MockBackend(seed=seed or 0)
        b.need_ids = list(cfg.character["needs"])
        b.tendency_names = list(cfg.character["tendencies"])
        return b
    from cat.llm import OllamaBackend
    return OllamaBackend(cfg)


def print_tick(cfg, first_dt):
    def _p(i: dict) -> None:
        dt = clock.parse(i["sim_time"])
        area = cfg.world["areas"][i["area"]]["name"]
        what = f"{i['action']} -> {i['outcome']}" if i["action"] else i["outcome"]
        extra = f"  [{'; '.join(i['fired'])}]" if i["fired"] else ""
        if not i["brain_online"]:
            extra += "  (brain offline)"
        print(f"#{i['id']:<4} {clock.day_label(first_dt, dt)} {clock.time_label(dt):>8} | "
              f"{area} | {what} | mood {i['mood']}, energy {i['energy']:.0f}{extra}")
    return _p


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cat.main", description="Internet Cat engine")
    p.add_argument("--mock", action="store_true", help="use MockBackend (no Ollama needed)")
    p.add_argument("--ticks", type=int, default=None, help="stop after N ticks")
    p.add_argument("--interval", type=float, default=None,
                   help="real seconds between ticks (default: settings; 0 with --mock)")
    p.add_argument("--seed", type=int, default=None, help="random seed (reproducible mock runs)")
    p.add_argument("--config-dir", default=None)
    p.add_argument("--db", default=None, help="override db_path")
    p.add_argument("-q", "--quiet", action="store_true", help="don't print each tick")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config_dir, {"db_path": args.db} if args.db else None)
    backend = make_backend(cfg, args.mock, args.seed)
    engine = Engine(cfg, backend, rng=random.Random(args.seed))
    engine.install_signal_handlers()
    interval = args.interval if args.interval is not None else (0 if args.mock else None)
    n = engine.run(ticks=args.ticks, interval=interval,
                   on_tick=None if args.quiet else print_tick(cfg, engine.first_dt))
    print(f"ran {n} ticks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Engine entrypoint (tick loop lands in M2)."""
from __future__ import annotations

import argparse

from cat import db
from cat.config import load_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cat.main", description="Internet Cat engine")
    p.add_argument("--mock", action="store_true", help="use MockBackend (no Ollama needed)")
    p.add_argument("--ticks", type=int, default=None, help="stop after N ticks")
    p.add_argument("--config-dir", default=None)
    p.add_argument("--db", default=None, help="override db_path")
    args = p.parse_args(argv)
    cfg = load_config(args.config_dir, {"db_path": args.db} if args.db else None)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    print(f"config ok for '{cfg.character['name']}', db ready at {cfg.db_path} "
          "(tick loop not implemented yet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

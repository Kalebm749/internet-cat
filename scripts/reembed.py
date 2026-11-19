"""Re-embed all memories after changing `embed_model` in settings.yaml.
Usage: .venv/bin/python scripts/reembed.py [--mock]"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat import db  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.llm import MockBackend, OllamaBackend  # noqa: E402
from cat.memory import reembed_all  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    backend = MockBackend() if a.mock else OllamaBackend(cfg)
    conn = db.connect(cfg.db_path)
    n = reembed_all(conn, backend, cfg.settings["embed_model"])
    print(f"re-embedded {n} memories with {cfg.settings['embed_model']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify the Ollama server the engine will use: tags, one brain-style call, one embedding.
Usage: .venv/bin/python scripts/check_ollama.py [--host http://IP:11434]"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat.config import load_config  # noqa: E402
from cat.llm import BackendError, OllamaBackend  # noqa: E402
from cat import brain  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host")
    a = ap.parse_args()
    cfg = load_config(overrides={"ollama_host": a.host} if a.host else None)
    b = OllamaBackend(cfg)
    print(f"host: {cfg.settings['ollama_host']}")
    print("online:", b.is_online())
    if not b.is_online():
        return 1
    try:
        t = time.time()
        out = b.chat_json("brain", cfg.settings["brain_model"],
                          [{"role": "system", "content": "You are a cat. Reply with JSON only."},
                           {"role": "user", "content": "You are on a sunny windowsill. What do you do?"}],
                          brain.BRAIN_SCHEMA)
        print(f"chat ok in {time.time() - t:.1f}s:", out)
        t = time.time()
        v = b.embed(cfg.settings["embed_model"], ["a sunny windowsill"])[0]
        print(f"embed ok in {time.time() - t:.1f}s: {len(v)} dims")
    except BackendError as e:
        print("FAILED:", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

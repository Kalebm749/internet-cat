"""Verify the Ollama server the engine will use: tags, one decision-style call, one embedding.
Usage: .venv/bin/python scripts/check_ollama.py [--host http://IP:11434]"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat.config import load_config  # noqa: E402
from cat.llm import BackendError, OllamaBackend  # noqa: E402



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
        models = cfg.settings["nit"]["models"]
        out = b.chat_json("decide", models["decision_model"],
                          [{"role": "system", "content": "You live by an old fountain. Reply with JSON only."},
                           {"role": "user", "content": "The water moves with no wind. What do you think, and do?"}],
                          {"type": "object", "properties": {"thought": {"type": "string"}, "action": {"type": "string",
                           "enum": ["sit_and_reflect", "inspect"]}}, "required": ["thought", "action"]})
        print(f"chat ok in {time.time() - t:.1f}s:", out)
        t = time.time()
        v = b.embed(cfg.settings["nit"]["models"]["embed_model"], ["an old stone fountain"])[0]
        print(f"embed ok in {time.time() - t:.1f}s: {len(v)} dims")
    except BackendError as e:
        print("FAILED:", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

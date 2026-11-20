"""One-off: pull the models named in settings.yaml into Ollama (python -m cat.pullmodels).
Safe to re-run: models that are already present are skipped (use --force to pull anyway)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Callable, TextIO

import httpx

from cat.config import load_config
from cat.llm import model_present, wanted_models


def pull(client: httpx.Client, host: str, model: str, out: TextIO) -> bool:
    """Stream /api/pull, printing progress. Returns True on success."""
    last = ""
    with client.stream("POST", f"{host}/api/pull", json={"model": model, "stream": True}) as r:
        if r.status_code != 200:
            out.write(f"  {model}: HTTP {r.status_code} {r.read()[:200]!r}\n")
            return False
        for line in r.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if "error" in msg:
                out.write(f"  {model}: ERROR {msg['error']}\n")
                return False
            status = msg.get("status", "")
            if msg.get("total"):
                status += f" {100 * msg.get('completed', 0) // msg['total']}%"
            if status != last and (status.endswith("0%") or not status.split()[-1].endswith("%") or status.endswith("00%")):
                out.write(f"  {model}: {status}\n")
            last = status
            if status == "success":
                return True
    return last == "success"


def run(client: httpx.Client, host: str, settings: dict, force: bool = False,
        out: TextIO = sys.stdout) -> int:
    try:
        tags = client.get(f"{host}/api/tags", timeout=settings["health_timeout_seconds"]).json()
    except (httpx.HTTPError, ValueError) as e:
        out.write(f"cannot reach Ollama at {host}: {e}\n")
        return 1
    have = [m.get("name", "") for m in tags.get("models", [])]
    failed = 0
    for model in wanted_models(settings):
        if model_present(model, have) and not force:
            out.write(f"{model}: already present\n")
            continue
        out.write(f"{model}: pulling...\n")
        if not pull(client, host, model, out):
            failed += 1
    out.write("all models ready\n" if not failed else f"{failed} model(s) failed\n")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--host")
    a = ap.parse_args(argv)
    cfg = load_config(overrides={"ollama_host": a.host} if a.host else None)
    host = cfg.settings["ollama_host"].rstrip("/")
    with httpx.Client(timeout=httpx.Timeout(cfg.settings["pull_timeout_seconds"], connect=10)) as c:
        return run(c, host, cfg.settings, a.force)


if __name__ == "__main__":
    raise SystemExit(main())

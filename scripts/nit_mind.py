"""Show what the creature has in front of it, and one decision (rewrite step 3; nothing is executed or saved
beyond spawning a life when the database has none).

    .venv/bin/python scripts/nit_mind.py                         # a fresh life in memory, mock brain
    .venv/bin/python scripts/nit_mind.py --know make_wish --energy 0.2 --event "Something glints."
    .venv/bin/python scripts/nit_mind.py --ollama-host http://127.0.0.1:11434   # ask the real decision model
    .venv/bin/python scripts/nit_mind.py --db data/nit.db --prompt-only
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat import content, decision, knowledge, life, mind, store, tuning  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.llm import MockBackend, OllamaBackend  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=":memory:", help="nit.db to read (default: a fresh life in memory)")
    ap.add_argument("--ollama-host", help="ask this Ollama instead of the mock brain")
    ap.add_argument("--kind", choices=decision.KINDS, default="full")
    ap.add_argument("--know", action="append", default=[], help="pretend it knows this action (repeatable)")
    ap.add_argument("--location", help="pretend it is here")
    ap.add_argument("--energy", type=float)
    ap.add_argument("--hunger", type=float)
    ap.add_argument("--event", action="append", default=[], help="something it just perceived (repeatable)")
    ap.add_argument("--prompt-only", action="store_true")
    a = ap.parse_args()

    t = tuning.load()
    c = content.load(tuning.path(t, "content_dir"))
    conn = store.connect(a.db)
    lf = life.open_life(conn, c, t)
    known = knowledge.known_ids(conn) | set(a.know)
    if a.location:
        lf.location = a.location
    if a.energy is not None:
        lf.bodystate.energy = a.energy
    if a.hunger is not None:
        lf.bodystate.hunger = a.hunger
    activity = None
    if a.kind != "full":
        activity = decision.activity_view("sitting on the rim of the basin", lf.sim_time, 30, lf.sim_time)
        activity["elapsed"] = 30
    m = mind.build(lf, t, known=known, last_done=knowledge.minutes_since_done(conn, lf.sim_time),
                   perception=a.event, activity=activity, seed=time.time() if a.ollama_host else 0)
    prompts = decision.load_prompts()
    msgs, dropped = decision.messages(m, a.kind, prompts, t["decisions"]["prompt_tokens"])
    print("=== system ===\n" + msgs[0]["content"])
    print("\n=== user ===\n" + msgs[1]["content"])
    print(f"\n(about {decision.tokens(msgs[0]['content']) + decision.tokens(msgs[1]['content'])} tokens"
          + (f"; dropped: {dropped}" if dropped else "") + ")")
    if a.prompt_only:
        return 0
    if a.ollama_host:
        backend = OllamaBackend(load_config(overrides={"ollama_host": a.ollama_host}))
    else:
        backend = MockBackend()
    t0 = time.perf_counter()
    r = decision.decide(backend, t["models"]["decision_model"], m, a.kind, prompts, t)
    print(f"\n=== decision ({time.perf_counter() - t0:.1f} s, {len(r.attempts)} attempt(s)) ===")
    for i, at in enumerate(r.attempts, 1):
        if at["problems"]:
            print(f"attempt {i} rejected: {'; '.join(at['problems'])}")
    print(json.dumps(r.decision.__dict__, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

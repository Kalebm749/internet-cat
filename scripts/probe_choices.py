"""Ask the real decision model the same question many times, with and without parts of its prompt, and count what it does.

The point is to find *what in the prompt* makes it repeat: run it on the machine that has Ollama (in Docker: ``docker
compose exec engine python scripts/probe_choices.py``). The scene is the one from a fresh life that has just made a wish at the
fountain: it knows sit and reflect, look around, make a wish and look over its wishes, and one wish is open.

    probe_choices.py --repeat 12                       # baseline and each ablation, 12 asks each
    probe_choices.py --repeat 12 --only baseline,holds # just these
    probe_choices.py --words water,coin,moves          # how often its thoughts mention these

Ablations (each removes one thing from what it is shown): holds (the "hold that ..." clauses), repeat_line (the boundary about
not repeating itself), temperament (the "You tend to ..." clauses), inner (needs and feelings in words), place (the place's
description), events (what it just perceived), all (everything above).
Prints, per configuration: which actions it picked, how many distinct thoughts, and how often a thought mentions the words.
"""
from __future__ import annotations

import argparse
import collections
import copy
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat import content, decision, knowledge, life, mind, store, tuning  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.llm import MockBackend, OllamaBackend  # noqa: E402

ABLATIONS = ("holds", "repeat_line", "temperament", "inner", "place", "events")


def scene(ablate: set[str], seed: int, t: dict, c, conn):
    lf = life.open_life(conn, c, t)
    sheet = lf.character.sheet
    if {"holds", "all"} & ablate:
        sheet["holds"] = []
    if {"temperament", "all"} & ablate:
        sheet["temperament"] = {}
    if {"repeat_line", "all"} & ablate:
        sheet["core"]["boundaries"] = [b for b in sheet["core"]["boundaries"] if "repeat itself" not in b]
    lf.world.values["fountain.wishes"] = [{"id": "1", "text": "I want a place where I can climb up high and look around",
                                           "made": lf.sim_time, "status": "open"}]
    if {"place", "all"} & ablate:
        lf.content.locations["fountain"].description = "A place with a basin."
    known = knowledge.known_ids(conn) | {"sit_and_reflect", "make_wish", "assess_wishes"}
    events = [] if {"events", "all"} & ablate else ["Something glints for a moment deep in the water."]
    recent = [{"text": "07:13 You look carefully around, but find nothing new here."},
              {"text": "07:24 You make a wish."}]
    inner = None if {"inner", "all"} & ablate else {"novelty": 0.4, "curiosity": 0.5}
    words = [] if inner is None else ["You are curious."]
    m = mind.build(lf, t, known=known, perception=events, recent=recent, seed=seed, menu_size=t["decisions"]["menu_size"],
                   temperament_words=[] if {"temperament", "all"} & ablate else ["chase anything you have not met before"],
                   inner=words)
    return m


def run(config: str, ablate: set[str], repeat: int, backend, t: dict, prompts: dict, words: list[str]) -> dict:
    c = content.load(tuning.path(t, "content_dir"))
    picks, thoughts, hits, ms = collections.Counter(), [], 0, []
    for i in range(repeat):
        conn = store.connect(":memory:")
        m = scene(ablate, 1000 + i, t, c, conn)
        t0 = time.perf_counter()
        r = decision.decide(backend, t["models"]["decision_model"], m, "full", prompts, t)
        ms.append(time.perf_counter() - t0)
        picks[r.decision.action] += 1
        thoughts.append(r.decision.thought.strip().lower())
        hits += any(re.search(rf"\b{re.escape(w)}", r.decision.thought, re.I) for w in words)
    return {"config": config, "picks": dict(picks.most_common()), "distinct_thoughts": len(set(thoughts)),
            "mention_rate": round(hits / repeat, 2), "seconds_each": round(sum(ms) / len(ms), 1),
            "sample": thoughts[:3]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=10)
    ap.add_argument("--only", help="comma list of: baseline," + ",".join(ABLATIONS) + ",all")
    ap.add_argument("--words", default="water,coin,fountain,moves,glint", help="words to look for in its thoughts")
    ap.add_argument("--mock", action="store_true", help="the mock brain (only to check the script runs)")
    a = ap.parse_args()
    t = tuning.load()
    prompts = decision.load_prompts()
    backend = MockBackend(seed=1) if a.mock else OllamaBackend(load_config())
    if not a.mock and not backend.is_online():
        print("Ollama is not reachable (or a model is missing). Run this where the engine runs.")
        return 1
    configs = (a.only.split(",") if a.only else ["baseline", *ABLATIONS, "all"])
    words = [w.strip() for w in a.words.split(",") if w.strip()]
    print(f"asking {a.repeat} times per configuration; looking for: {', '.join(words)}\n")
    for cfg in configs:
        res = run(cfg, set() if cfg == "baseline" else {cfg}, a.repeat, backend, copy.deepcopy(t), prompts, words)
        print(f"{res['config']:<12} picks {res['picks']}  distinct thoughts {res['distinct_thoughts']}/{a.repeat}  "
              f"mention rate {res['mention_rate']}  ({res['seconds_each']} s each)")
        for s in res["sample"]:
            print(f"{'':<12}   \"{s[:110]}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Seed a database with ~100+ mock ticks (diary, a doze stretch, sleep cards, keepsakes,
preferences) so the website can be developed without the real brain.

  .venv/bin/python scripts/seed_db.py --db data/dev.db [--ticks 120] [--seed 1] [--force]
  .venv/bin/python -m cat.web --db data/dev.db
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat import db  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.engine import Engine  # noqa: E402
from cat.llm import MockBackend  # noqa: E402

# (action, outcome, reaction) - generic so they fit any placeholder world
LINES = [
    ("stretch out in the patch of sun", "She stretches until her toes curl, warm light on her fur.", "mmm. warm."),
    ("bat at something shiny", "A glint skitters across the floor and she pounces on it, missing by a whisker.", "so close!!"),
    ("sniff along the skirting board", "She finds a forgotten crumb and an interesting smell of old dust.", "mysteries everywhere."),
    ("watch the window", "Outside, the light shifts slowly and a bird hops along the ledge.", "i could watch this all day."),
    ("wash a paw", "She licks a paw and smooths her whiskers until they sit just so.", "tidy. very tidy."),
    ("curl up on something soft", "She kneads the cushion, turns three times and settles down, purring.", "this is the best spot."),
    ("explore the next room", "She slips through the doorway, tail high, and inspects every corner.", "new smells. good."),
    ("chase her own tail", "She spins in a dizzy circle and flops over, surprised to have lost.", "that was NOT my fault."),
    ("sit very still and listen", "A pipe ticks somewhere in the walls. Her ears swivel toward it.", "what was that."),
    ("knock a small thing off a shelf", "It tumbles, clatters and rolls away. She watches it go, satisfied.", "gravity works. noted."),
    ("nap in a sunbeam", "She dozes lightly with one ear still listening to the house.", "zzz... five more minutes."),
    ("investigate the smell of food", "She follows her nose and finds a tempting bowl, then licks her whiskers.", "yes. yes please."),
]
KEEPSAKES = [
    "I found the warmest spot in the house and it was mine for an afternoon.",
    "Something shiny skittered past and I nearly caught it. Next time.",
    "The window shows the best things if I'm patient enough.",
    "I knocked something off a shelf and nobody minded. Interesting.",
    "A bird landed so close I could count its feathers.",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/dev.db")
    ap.add_argument("--ticks", type=int, default=120)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--force", action="store_true", help="overwrite an existing database")
    a = ap.parse_args()

    cfg = load_config(overrides={"db_path": a.db})
    path = cfg.db_path
    if path.exists():
        if not a.force:
            print(f"{path} exists; pass --force to overwrite it (this deletes it).")
            return 1
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    rng = random.Random(a.seed)
    b = MockBackend(seed=a.seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    default_brain, default_narr = (lambda m, s: b._default("brain", m)), (lambda m, s: b._default("narrator", m))
    picked = {"line": LINES[0]}

    def brain(m, s):
        out = default_brain(m, s)
        picked["line"] = rng.choice(LINES)
        a_, _, r_ = picked["line"]
        return {**out, "action": a_, "reaction": r_, "thought": "", "intent": "just being a cat"}

    def narrator(m, s):
        out = default_narr(m, s)
        return {**out, "outcome": picked["line"][1], "duration": rng.choice([15, 20, 25, 30, 40])}

    b.handlers.update(brain=brain, narrator=narrator,
                      consolidate=lambda m, s: {"summary": "A long, soft day of small adventures.",
                                                "memories": [{"text": rng.choice(KEEPSAKES),
                                                              "importance": rng.choice(["low", "medium", "high"])}]})
    cfg.settings["tick_interval_seconds"] = 0
    engine = Engine(cfg, b, rng=random.Random(a.seed))

    outage = range(int(a.ticks * 0.45), int(a.ticks * 0.45) + 7)
    for i in range(a.ticks):
        b.online = i not in outage          # a short "brain offline" stretch -> a doze entry
        engine.tick()
    b.online = True

    def count(sql: str) -> int:
        return engine.conn.execute(sql).fetchone()[0]
    kinds = dict(engine.conn.execute("SELECT kind, COUNT(*) FROM ticks GROUP BY kind").fetchall())
    print(f"seeded {path}: {count('SELECT COUNT(*) FROM ticks')} ticks "
          f"({kinds.get('tick', 0)} diary entries, {kinds.get('doze', 0)} dozing, "
          f"{kinds.get('asleep', 0)} asleep), {count('SELECT COUNT(*) FROM sleep_log')} nights, "
          f"{count('SELECT COUNT(*) FROM memories WHERE kind=' + chr(39) + 'long' + chr(39))} keepsakes, "
          f"{count('SELECT COUNT(*) FROM preferences')} preferences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

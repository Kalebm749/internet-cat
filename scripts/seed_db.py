"""Seed a database so the website AND the dev dashboard can be developed without the real brain.

It runs the real engine with the mock brain for ~120 ticks (a brain-offline stretch, sleep nights,
keepsakes, preferences), then drives the real dev-command queue (a pause / step / resume, an
interval change, an injected event, a snapshot, a reload and ONE forced sleep cycle) and fills the
dashboard's own files (structured logs for several services, a sample playground comparison).

  .venv/bin/python scripts/seed_db.py --db data/seed.db --force [--ticks 120] [--seed 1]
  .venv/bin/python -m cat.web --db data/seed.db            # the public site   http://127.0.0.1:8080
  .venv/bin/python -m cat.devdash --db data/seed.db        # the dev dashboard http://127.0.0.1:8090

Everything lives next to the database: <dir>/devdash.db, <dir>/logs/, <dir>/backups/. `--db` on
`cat.web`, `cat.devdash`, `cat.main` and `add_ticks.py` finds them there too."""
import argparse
import io
import json
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat import db, logs  # noqa: E402
from cat.config import load_config, sibling_overrides  # noqa: E402
from cat.devqueue import DevQueue  # noqa: E402
from cat.engine import Engine  # noqa: E402
from cat.healthcheck import write_heartbeat  # noqa: E402
from cat.llm import MockBackend  # noqa: E402

# (action, outcome, reaction) - generic so they fit any placeholder world
LINES = [
    ("settle in the patch of sun", "She stays in the warm light until the afternoon feels slow and soft.", "mmm. warm."),
    ("chase something shiny", "A glint skitters across the floor and she nearly catches it.", "so close!!"),
    ("look along the skirting board", "She finds a forgotten crumb and an interesting smell of old dust.", "mysteries everywhere."),
    ("watch the window", "Outside, the light shifts slowly and a bird hops along the ledge.", "i could watch this all day."),
    ("tidy a corner", "She straightens what she can reach until it sits just so.", "tidy. very tidy."),
    ("settle on something soft", "She rearranges the cushion, turns about and settles down.", "this is the best spot."),
    ("explore the next room", "She slips through the doorway and inspects every corner.", "new things. good."),
    ("go round in circles", "She spins in a dizzy loop and stops, surprised to have lost the thread.", "that was NOT my fault."),
    ("sit very still and listen", "A pipe ticks somewhere in the walls. She turns toward the sound.", "what was that."),
    ("knock a small thing off a shelf", "It tumbles, clatters and rolls away. She watches it go, satisfied.", "gravity works. noted."),
    ("rest in a sunbeam", "She dozes lightly, still half listening to the house.", "zzz... five more minutes."),
    ("investigate the smell of food", "She follows the smell and finds a tempting bowl.", "yes. yes please."),
]
KEEPSAKES = [
    "I found the warmest spot in the house and it was mine for an afternoon.",
    "Something shiny skittered past and I nearly caught it. Next time.",
    "The window shows the best things if I'm patient enough.",
    "I knocked something off a shelf and nobody minded. Interesting.",
    "A bird landed so close I could count its feathers.",
]


def _service_lines(cfg, service: str, messages: list[tuple[str, str, int | None]]) -> None:
    """Write a few structured log lines for a service we don't actually run here (web, devdash)."""
    logger = logging.Logger(f"seed.{service}")
    handler = logging.FileHandler(cfg.log_dir / f"{service}.jsonl", encoding="utf-8")
    handler.setFormatter(logs.JsonFormatter(service))
    logger.addHandler(handler)
    for level, msg, tick in messages:
        logger.log(getattr(logging, level), msg, extra={"event": f"cat.{service}", "tick_id": tick})
    handler.close()


def seed(db_path: Path | str, ticks: int = 120, seed_value: int = 1, force: bool = False, quiet: bool = False) -> dict:
    """Create the database and everything the dashboard shows. Returns a summary dict."""
    db_path = Path(db_path).resolve()
    overrides = {**sibling_overrides(db_path), "tick_interval_seconds": 0}
    cfg = load_config(overrides=overrides)
    if db_path.exists() or cfg.dev_db_path.exists():
        if not force:
            raise FileExistsError(f"{db_path} (or its devdash.db) exists; pass --force to overwrite it (this deletes it).")
        for p in (db_path, cfg.dev_db_path):
            for suffix in ("", "-wal", "-shm"):
                Path(str(p) + suffix).unlink(missing_ok=True)
        for old in cfg.log_dir.glob("*.jsonl*") if cfg.log_dir.exists() else []:
            old.unlink()
    logs.setup_logging("engine", cfg, stream=io.StringIO() if quiet else sys.stdout)     # data/logs/engine.jsonl
    logging.getLogger().setLevel(logging.INFO)

    rng = random.Random(seed_value)
    b = MockBackend(seed=seed_value)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    default_brain, default_narr = (lambda m, s: b._default("brain", m)), (lambda m, s: b._default("narrator", m))
    picked = {"line": LINES[0]}

    def brain(m, s):
        out = default_brain(m, s)
        picked["line"] = rng.choice(LINES)
        a_, _, r_ = picked["line"]
        return {**out, "action": a_, "reaction": r_, "thought": "", "intent": "just passing the time"}

    def narrator(m, s):
        out = default_narr(m, s)
        return {**out, "outcome": picked["line"][1], "duration": rng.choice([15, 20, 25, 30, 40])}

    b.handlers.update(brain=brain, narrator=narrator,
                      consolidate=lambda m, s: {"summary": "A long, soft day of small adventures.",
                                                "memories": [{"text": rng.choice(KEEPSAKES), "importance": rng.choice(["low", "medium", "high"])}]})
    engine = Engine(cfg, b, rng=random.Random(seed_value))
    q = DevQueue(cfg.dev_db_path)

    outage = range(int(ticks * 0.45), int(ticks * 0.45) + 7)          # a brain-offline stretch => a collapsed doze entry
    for i in range(ticks):
        b.online = i not in outage
        engine.tick()
        if i == ticks // 2:                                           # exercise the dashboard's dev controls
            q.enqueue("set_interval", {"seconds": 5, "ticks": 2})
            q.enqueue("inject_event", {"text": "A neighbour appears on the fence.",
                                       "spawn": {"name": "a feather", "tags": ["toy", "movable"], "area": engine.state["area"]}})
            q.enqueue("pause")
            q.enqueue("step")
            engine.dev.process()                                      # paused; one step queued
            step_cmd, engine.step_pending, engine.step_cmd_id = engine.step_cmd_id, False, None
            engine.dev.finish_step(step_cmd, engine.tick())           # the stepped tick
            q.enqueue("resume")
            q.enqueue("snapshot")
            engine.dev.process()
            engine.interval_override = engine.interval_override_ticks = None
    b.online = True
    q.enqueue("reload_settings")
    q.enqueue("force_sleep")                                          # the one forced sleep cycle
    engine.dev.process()
    engine.dev.publish(force=True, state="stopped")

    # a sample playground comparison (clearly marked: no Ollama was used)
    call = db.connect(cfg.db_path, readonly=True).execute(
        "SELECT * FROM llm_calls WHERE kind='brain' ORDER BY id DESC LIMIT 1").fetchone()
    if call:
        gid = "seedsample01"
        for model, ms in (("qwen3:8b", 1840), ("gemma3:4b", 920)):
            q.conn.execute(
                "INSERT INTO playground_runs(created_at,group_id,source_call_id,label,model,options,system_prompt,user_prompt,schema,"
                "raw_response,parsed,valid_json,prompt_tokens,completion_tokens,load_ms,prompt_eval_ms,eval_ms,total_ms) "
                "VALUES(datetime('now'),?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
                (gid, call["id"], "seed sample (not a real replay)", model, json.dumps({"temperature": 0.9}), call["system_prompt"],
                 call["user_prompt"], call["schema"], call["raw_response"], call["parsed"], call["prompt_tokens"], call["completion_tokens"],
                 0, ms // 4, ms // 2, ms))
    n_tick = engine.last_tick_id or 0
    _service_lines(cfg, "web", [("INFO", "serving the journal", None), ("WARNING", "slow request: /api/diary?before=40 took 410 ms", None),
                                ("INFO", "GET /api/state 200", None)])
    _service_lines(cfg, "devdash", [("INFO", "devdash started (loopback only)", None), ("WARNING", "Ollama unreachable (seed: no Ollama here)", None)])
    summary = {}
    conn = engine.conn
    for label, sql in (("ticks", "SELECT COUNT(*) FROM ticks"), ("diary entries", "SELECT COUNT(*) FROM ticks WHERE kind='tick'"),
                       ("dozing", "SELECT COUNT(*) FROM ticks WHERE kind='doze'"), ("asleep", "SELECT COUNT(*) FROM ticks WHERE kind='asleep'"),
                       ("nights", "SELECT COUNT(*) FROM sleep_log"), ("keepsakes", "SELECT COUNT(*) FROM memories WHERE kind='long'"),
                       ("preferences", "SELECT COUNT(*) FROM preferences"), ("LLM calls", "SELECT COUNT(*) FROM llm_calls"),
                       ("embed calls", "SELECT COUNT(*) FROM embed_calls"), ("recall logs", "SELECT COUNT(*) FROM recall_logs"),
                       ("rejected changes", "SELECT COUNT(*) FROM effect_logs WHERE accepted=0")):
        summary[label] = conn.execute(sql).fetchone()[0]
    summary["dev commands"] = len(q.recent(100))
    summary["playground runs"] = q.conn.execute("SELECT COUNT(*) FROM playground_runs").fetchone()[0]
    summary["log services"] = sorted(p.name.split(".jsonl")[0] for p in cfg.log_dir.glob("*.jsonl"))
    summary["paths"] = {"db": str(db_path), "devdash_db": str(cfg.dev_db_path), "logs": str(cfg.log_dir)}
    engine.mark_clean_stop()
    write_heartbeat(cfg.heartbeat_path)                               # so the Services panel has an engine heartbeat to show
    q.close()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/seed.db")
    ap.add_argument("--ticks", type=int, default=120)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--force", action="store_true", help="overwrite an existing database")
    a = ap.parse_args()
    try:
        s = seed(a.db, a.ticks, a.seed, a.force, quiet=True)
    except FileExistsError as e:
        print(e)
        return 1
    paths = s.pop("paths")
    print("seeded " + ", ".join(f"{v} {k}" if not isinstance(v, list) else f"{k}: {', '.join(v)}" for k, v in s.items()))
    print(f"  game db   {paths['db']}\n  devdash   {paths['devdash_db']}\n  logs      {paths['logs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

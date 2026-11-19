"""Balance check with no LLM: run the real engine with scripted brains and report how long the needs spend
in each band, so decay rates, move gains and sleep can be tuned from numbers instead of by feel.

  .venv/bin/python scripts/simulate_balance.py [--ticks 400] [--seeds 3] [--policy random|first|cycle|all]
                                               [--config-dir config]

Policies (what the stand-in brain picks from the menu; none looks at needs, like the real brain's menu):
  random  a uniformly random move       first  always the first move on the menu (the worst case: no variety)
  cycle   the menu in rotation          needy  an upper bound: sleeps when tired, else feeds the lowest need
                                         (a yardstick only; the real engine never ranks moves by need)
  all     each of the above
Reads like: "food: empty 3%, low 20%, okay 50%, full 27%". A need that sits in 'empty' for a long time under
`random` is probably decaying too fast, or nothing offered can restore it. Uses a temporary database."""
import argparse
import itertools
import json
import random
import re
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat import character  # noqa: E402
from cat import db  # noqa: E402
from cat import moves as MV  # noqa: E402
from cat.config import load_config  # noqa: E402
from cat.engine import Engine  # noqa: E402
from cat.llm import MockBackend  # noqa: E402

MENU = re.compile(r"^- \[(m\d+)\] (.+)$", re.M)
POLICIES = ("random", "first", "cycle", "needy")


def needy_pick(e, items):
    """An upper bound, NOT how the real brain or engine behaves (code never ranks moves by need): sleep
    when tired and allowed, else the move that feeds the lowest need most. If even this policy cannot keep
    the needs up, the moves/decay numbers are wrong, not the brain."""
    menu = {m.id: m for m in e._menu()}
    st = e.state
    needs = st["needs"]
    low = min(needs, key=lambda k: needs[k])
    gv = e.cfg.settings["gain_values"]
    best, best_score = None, 0.0
    goes = [mid for mid, _ in items if menu[mid].verb == "go"]
    can_feed = False
    for mid, _label in items:
        mv = menu[mid]
        if mv.verb == "sleep":
            if st["energy"] < 0:
                return mid
            continue
        pool = e.world.here(st["area"]) + e.world.inventory()
        tags = next((o["tags"] for o in pool if o["id"] == mv.target_id), []) if mv.target_id else []
        res = MV.resolve(e.cfg, mv, target_tags=tags, rng=random.Random(0), novel=False)
        pts = {n: gv.get(b, 0) for n, b in res["gains"].items()}
        if pts.get(low, 0) > 0:
            can_feed = True
        score = pts.get(low, 0) * 10 + sum(pts.values())       # the lowest need first, then anything useful
        if score > best_score:
            best, best_score = mid, score
    if not can_feed and goes:                      # nothing here feeds it: look elsewhere
        return random.Random(st["sim_time"]).choice(goes)
    return best or items[0][0]


def brain_for(policy: str, seed: int, holder: dict):
    rng = random.Random(seed)
    counter = itertools.count()

    def handler(messages, _seed):
        items = MENU.findall(messages[-1]["content"])
        if policy == "needy":
            mid = needy_pick(holder["engine"], items)
        elif policy == "first":
            mid = items[0][0]
        elif policy == "cycle":
            mid = items[next(counter) % len(items)][0]
        else:
            mid = rng.choice(items)[0]
        return {"reaction": "", "thought": "", "move": mid, "why": "", "feeling": "neutral"}
    return handler


def run(config_dir, policy: str, seed: int, ticks: int) -> dict:
    cfg = load_config(config_dir)
    with tempfile.TemporaryDirectory() as tmp:
        cfg.settings["db_path"] = str(Path(tmp) / "sim.db")
        cfg.settings["log_dir"] = str(Path(tmp) / "logs")
        cfg.settings["dev_db_path"] = str(Path(tmp) / "devdash.db")
        cfg.settings["heartbeat_file"] = str(Path(tmp) / "hb")
        b = MockBackend(seed=seed)
        b.need_ids = list(cfg.character["needs"])
        b.tendency_names = list(cfg.character["tendencies"])
        holder: dict = {}
        b.handlers["brain"] = brain_for(policy, seed, holder)
        e = Engine(cfg, b, rng=random.Random(seed))
        holder["engine"] = e
        e.run(ticks=ticks, interval=0)
        rows = e.conn.execute("SELECT kind, needs_after, energy_after FROM ticks ORDER BY id").fetchall()
        sleeps = e.conn.execute("SELECT COUNT(*) FROM sleep_log").fetchone()[0]
        decisions = sum(1 for r in rows if r["kind"] == "tick")
        e.conn.close()
    bands = {n: {k: 0 for k in character.BANDS} for n in cfg.character["needs"]}
    energy, any_empty, n = [], 0, 0
    for r in rows:
        if r["kind"] in ("doze", "catchup"):
            continue
        needs = json.loads(r["needs_after"])
        n += 1
        empty = False
        for nid in bands:
            band = character.need_band(needs.get(nid, 0), cfg)
            bands[nid][band] += 1
            empty = empty or band == "empty"
        any_empty += empty
        if r["energy_after"] is not None:
            energy.append(r["energy_after"])
    n = max(n, 1)
    return {"bands": {k: {b: v / n for b, v in d.items()} for k, d in bands.items()},
            "any_empty": any_empty / n, "energy": statistics.mean(energy) if energy else None,
            "sleeps": sleeps, "decisions": decisions, "ticks": n}


def merge(results: list[dict]) -> dict:
    out = {"bands": {}, "any_empty": statistics.mean(r["any_empty"] for r in results),
           "energy": statistics.mean(r["energy"] for r in results if r["energy"] is not None) if any(
               r["energy"] is not None for r in results) else None,
           "sleeps": statistics.mean(r["sleeps"] for r in results),
           "decisions": statistics.mean(r["decisions"] for r in results),
           "ticks": statistics.mean(r["ticks"] for r in results)}
    for nid in results[0]["bands"]:
        out["bands"][nid] = {b: statistics.mean(r["bands"][nid][b] for r in results) for b in character.BANDS}
    return out


def report(policy: str, m: dict) -> str:
    L = [f"policy: {policy}  ({m['ticks']:.0f} ticks, {m['decisions']:.0f} decisions, "
         f"{m['sleeps']:.1f} sleeps, mean energy {m['energy'] if m['energy'] is None else round(m['energy'], 1)})"]
    for nid, d in m["bands"].items():
        L.append(f"  {nid:<14}" + ", ".join(f"{b} {d[b] * 100:3.0f}%" for b in character.BANDS))
    L.append(f"  some need empty: {m['any_empty'] * 100:.0f}% of the time")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=400)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--policy", choices=POLICIES + ("all",), default="all")
    ap.add_argument("--config-dir", default=None)
    a = ap.parse_args()
    for policy in (POLICIES if a.policy == "all" else (a.policy,)):
        print(report(policy, merge([run(a.config_dir, policy, s, a.ticks) for s in range(1, a.seeds + 1)])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

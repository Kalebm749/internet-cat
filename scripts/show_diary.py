"""Print the diary from a database so you can read it: reaction, thought, action, outcome,
changes and rejected changes. Usage: scripts/show_diary.py [--db PATH] [-n 50]"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat import clock, db  # noqa: E402
from cat.config import load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("-n", type=int, default=50)
    a = ap.parse_args()
    cfg = load_config(overrides={"db_path": a.db} if a.db else None)
    conn = db.connect(cfg.db_path, readonly=True)
    first = clock.parse(db.get_meta(conn, "first_sim_time"))
    rows = conn.execute("SELECT * FROM ticks ORDER BY id DESC LIMIT ?", (a.n,)).fetchall()[::-1]
    for r in rows:
        dt = clock.parse(r["sim_time"])
        area = cfg.world["areas"].get(r["area"], {}).get("name", r["area"])
        print(f"\n#{r['id']} {clock.day_label(first, dt)} {clock.time_label(dt)} - {area}")
        if r["kind"] == "doze":
            print("  (dozing: brain offline)")
            continue
        print(f"  reaction: {r['reaction']}\n  thought:  {r['thought']}")
        print(f"  action:   {r['action']}  ({r['intent']})\n  outcome:  {r['outcome']}")
        print(f"  gains {json.loads(r['gains'])}  effort {r['effort']}  {r['duration']} min")
        for c in json.loads(r["changes"]):
            print("  + change:", {k: v for k, v in c.items() if k != "label"})
        for c in json.loads(r["rejected_changes"]):
            print("  ! rejected:", c["change"], "->", c["reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

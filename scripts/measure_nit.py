"""Read-only report on a real run (rewrite step 7): is the small model a good enough brain on this machine?

    .venv/bin/python scripts/measure_nit.py --db data/nit.db
    .venv/bin/python scripts/measure_nit.py --db data/nit.db --json       # the same numbers as JSON
    docker compose exec engine python scripts/measure_nit.py               # inside Docker (default database)

What it answers:
  speed        decision latency (median, 90th percentile, worst) against the 15 s decision target
  validity     answers that needed a retry, fallbacks, the most common problems
  prompt       prompt size against the token budget; context dropped to fit
  list bias    does the position of an option in the (shuffled) menu steer the pick?
  hunger       when hungry with food on the menu, does it eat?
  nights       does each night's sleep cycle finish, and how long do the 14B stages take?
  behaviour    variety, freshness, restarts, repeated thoughts, rejected decisions
  letters      imported, read, answered, and decisions that answered a letter
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat import db, tuning  # noqa: E402


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def measure(conn) -> dict:
    t = tuning.load()
    out: dict = {}
    decs = conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    out["run"] = {
        "decisions": len(decs),
        "sim_from": decs[0]["sim_time"] if decs else None, "sim_to": decs[-1]["sim_time"] if decs else None,
        "real_from": decs[0]["real_time"] if decs else None, "real_to": decs[-1]["real_time"] if decs else None,
        "kinds": dict(Counter(d["kind"] for d in decs)),
    }
    ms = [d["ms"] for d in decs if not d["fallback"]]
    target = t["decisions"]["interval_seconds"] * 1000
    out["speed"] = {"median_ms": pct(ms, 0.5), "p90_ms": pct(ms, 0.9), "max_ms": max(ms) if ms else None,
                    "over_target": sum(1 for x in ms if x > target), "target_ms": target}
    attempts = [json.loads(d["attempts"]) for d in decs]
    problems = Counter(p.split(";")[0][:80] for a in attempts for x in a for p in x["problems"])
    out["validity"] = {
        "first_try": sum(1 for a in attempts if len(a) == 1 and not a[0]["problems"]),
        "retried": sum(1 for a in attempts if len(a) > 1),
        "fallbacks": sum(d["fallback"] for d in decs),
        "fallback_rate": round(sum(d["fallback"] for d in decs) / len(decs), 3) if decs else None,
        "top_problems": problems.most_common(5),
    }
    toks = [sum(len(m["content"]) for m in json.loads(d["messages"])[:2]) // 4 for d in decs]
    out["prompt"] = {"median_tokens": pct(toks, 0.5), "max_tokens": max(toks) if toks else None,
                     "budget": t["decisions"]["prompt_tokens"],
                     "dropped_context": sum(1 for d in decs if json.loads(d["dropped"]))}
    # list bias: position of the chosen option among the options shown (only full-menu choices)
    thirds = Counter()
    first, expected_first, n = 0, 0.0, 0
    for d in decs:
        if d["kind"] != "full" or d["fallback"]:
            continue
        opts = [o["id"] for o in json.loads(d["mind"])["options"]]
        if d["action"] not in opts or len(opts) < 3:
            continue
        i = opts.index(d["action"])
        thirds[min(2, i * 3 // len(opts))] += 1
        first += i == 0
        expected_first += 1 / len(opts)
        n += 1
    out["list_bias"] = {"choices": n, "first_third": thirds[0], "middle_third": thirds[1], "last_third": thirds[2],
                        "picked_first": first, "expected_first_by_chance": round(expected_first, 1)}
    # hunger: hungry, food on the menu -> did it eat?
    eat_ids = {"eat_offering"}
    hungry, ate = 0, 0
    hunger_vals = []
    for d in decs:
        m = json.loads(d["mind"])
        hunger_vals.append(m["body"]["hunger"])
        if d["kind"] == "full" and m["body"]["hunger"] >= 0.5 and eat_ids & {o["id"] for o in m["options"]}:
            hungry += 1
            ate += d["action"] in eat_ids
    out["hunger"] = {"hungry_with_food_on_menu": hungry, "ate": ate,
                     "ate_rate": round(ate / hungry, 2) if hungry else None,
                     "decisions_at_hunger_0.65_plus": sum(1 for h in hunger_vals if h >= 0.65),
                     "max_hunger": max(hunger_vals) if hunger_vals else None}
    nights = []
    for r in conn.execute("SELECT * FROM sleep_runs ORDER BY id"):
        st = json.loads(r["stages"] or "{}")
        nights.append({"started": r["started_at"], "committed": r["finished_at"], "status": r["status"],
                       "repeated": r["repeated"],
                       "stages": {k: f"{v.get('status')} {v.get('seconds', '?')}s" for k, v in st.items()},
                       "real_seconds": round(sum(v.get("seconds", 0) for v in st.values()), 1)})
    sleeps = conn.execute("SELECT started_at, ended_at FROM activities WHERE action_id = 'sleep' AND status = 'finished' "
                          "ORDER BY id").fetchall()
    out["nights"] = {"count": len(nights), "runs": nights,
                     "slept": [f"{s['started_at']} -> {s['ended_at']}" for s in sleeps]}
    acts = conn.execute("SELECT action_id, status, freshness, restart_of, text FROM activities").fetchall()
    done = [a for a in acts if a["status"] in ("finished", "interrupted")]
    thoughts = conn.execute("SELECT repeated FROM thoughts").fetchall()
    out["behaviour"] = {
        "actions": dict(Counter(a["action_id"] for a in done).most_common()),
        "mean_freshness": round(statistics.mean(a["freshness"] for a in done), 2) if done else None,
        "restarts": sum(1 for a in done if a["restart_of"]),
        "repeated_thoughts": f"{sum(r[0] for r in thoughts)} of {len(thoughts)}",
        "rejected": dict(Counter(a["text"].split(": ", 1)[-1][:60] for a in acts if a["status"] == "rejected")),
        "knows": [r[0] for r in conn.execute("SELECT action_id FROM knowledge ORDER BY discovered_at")],
        "memories": dict(Counter(r[0] for r in conn.execute("SELECT kind FROM memories"))),
        "character_versions": conn.execute("SELECT COUNT(*) FROM character_versions").fetchone()[0],
        "proposals": f"{conn.execute('SELECT COALESCE(SUM(accepted), 0) FROM dev_proposals').fetchone()[0]} accepted of "
                     f"{conn.execute('SELECT COUNT(*) FROM dev_proposals').fetchone()[0]}",
    }
    out["letters"] = {
        "imported": conn.execute("SELECT COUNT(*) FROM mail_imports").fetchone()[0],
        "read": sum(1 for a in done if a["action_id"] == "read_messages"),
        "answered": sum(1 for a in done if a["action_id"] == "reply_message"),
        "decisions_answering_a_letter": sum(1 for d in decs if d["letter"]),
    }
    mb = [json.loads(d["mind"])["body"] for d in decs]
    out["body"] = {v: {"min": round(min(b[v] for b in mb), 2), "mean": round(statistics.mean(b[v] for b in mb), 2),
                       "max": round(max(b[v] for b in mb), 2)} for v in ("mood", "energy", "hunger")} if mb else {}
    return out


def show(r: dict) -> str:
    lines = []
    for section, body in r.items():
        lines.append(f"\n== {section} ==")
        if section == "nights":
            lines.append(f"  nights: {body['count']}")
            for n in body["runs"]:
                lines.append(f"  {n['started']} -> committed {n['committed']}  {n['status']}  "
                             f"{n['real_seconds']} s real{'  REPEATED' if n['repeated'] else ''}")
                lines.append("    " + ", ".join(f"{k}: {v}" for k, v in n["stages"].items()))
            for s in body["slept"]:
                lines.append(f"  slept {s}")
            continue
        for k, v in body.items():
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(tuning.path(tuning.load(), "db_path")))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not Path(a.db).exists():
        print(f"no database at {a.db}", file=sys.stderr)
        return 1
    r = measure(db.connect(a.db, readonly=True))
    print(json.dumps(r, indent=2, ensure_ascii=False, default=str) if a.json else show(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure a run: numbers to judge how the creature actually behaved, read straight from its database.

  .venv/bin/python scripts/measure_run.py [--db data/cat.db] [--json] [--config-dir config]

Read-only (the database is opened readonly, like the website). Nothing here calls an LLM. Sections:

  run        ticks by kind, sim days covered, decisions vs carried-on steps, dozes (brain offline)
  brain      calls, retries, errors (off-menu replies, bad JSON, timeouts), latency, prompt size against
             llm_num_ctx (a prompt near the limit is silently cut from the front by Ollama)
  position   where on the menu the picked move sat. Under no bias the mean is about 0.5 and the first
             tenth of the menu gets about 10% of picks; a big first-tenth share means list order steers it
  choices    most picked moves, how varied they are (entropy), same move twice in a row, impulses,
             places it spent its decisions in
  eating     brain decisions made while hungry (its hunger lines in the prompt), how often food was on the menu
             then, and how often it ate by choice; meals the feeding reflex took over
  needs      time in each band per need, how often some need is empty, anxiety and mood
  inner      what it has built up: memories, preferences, tendencies, self-image, habits, projects, nights

Use it on the first real run before judging the diary by feel (docs/analysis-2026-11.md, roadmap phase 0).
Needs trace_level: full for the menu-position and prompt-size sections (summary keeps no prompts)."""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat import character, clock, db  # noqa: E402
from cat.config import load_config  # noqa: E402

MENU = re.compile(r"^- \[(m\d+)\] (.+)$", re.M)
IMPULSE = "an impulse"


def _pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _q(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


def run_section(conn) -> dict:
    kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM ticks GROUP BY kind").fetchall())
    span = conn.execute("SELECT MIN(sim_time), MAX(sim_time) FROM ticks").fetchone()
    days = 0.0
    if span[0]:
        days = (clock.parse(span[1]) - clock.parse(span[0])).total_seconds() / 86400
    nights = conn.execute("SELECT COUNT(*) FROM sleep_log").fetchone()[0]
    return {"ticks": sum(kinds.values()), "by_kind": kinds, "sim_days": round(days, 2),
            "decisions": kinds.get("tick", 0), "carried": kinds.get("carry", 0), "dozes": kinds.get("doze", 0),
            "nights": nights}


def brain_section(conn, cfg) -> dict:
    rows = conn.execute("SELECT tick_id, attempt, error, valid_json, prompt_tokens, total_ms FROM llm_calls "
                        "WHERE kind='brain'").fetchall()
    errors = Counter()
    for r in rows:
        e = r["error"] or ""
        if not e:
            continue
        errors["off-menu move" if "not on the menu" in e else
               "invalid JSON" if "JSON" in e or "json" in e else
               "timeout" if "timed out" in e.lower() or "timeout" in e.lower() else "other"] += 1
    ok = [r for r in rows if not r["error"]]
    toks = [r["prompt_tokens"] for r in rows if r["prompt_tokens"]]
    ctx = cfg.settings["llm_num_ctx"]
    ms = [r["total_ms"] for r in ok if r["total_ms"] is not None]
    others = dict(conn.execute("SELECT kind, COUNT(*) FROM llm_calls WHERE kind!='brain' GROUP BY kind").fetchall())
    return {"attempts": len(rows), "ok": len(ok), "retries": sum(1 for r in rows if (r["attempt"] or 1) > 1),
            "errors": dict(errors), "ok_rate_pct": _pct(len(ok), len(rows)),
            "latency_ms_p50": _q(ms, 0.5), "latency_ms_p90": _q(ms, 0.9),
            "prompt_tokens_p50": _q(toks, 0.5), "prompt_tokens_max": max(toks) if toks else None,
            "num_ctx": ctx, "near_ctx_limit_pct": _pct(sum(1 for t in toks if t >= 0.9 * ctx), len(toks)),
            "other_calls": others}


def position_section(conn) -> dict:
    """Where the chosen move sat on the menu it was shown (0 = first, 1 = last)."""
    rel, first_tenth, sizes = [], 0, []
    for r in conn.execute("SELECT user_prompt, raw_response FROM llm_calls WHERE kind='brain' AND error IS NULL "
                          "AND user_prompt IS NOT NULL AND raw_response IS NOT NULL"):
        items = [mid for mid, _ in MENU.findall(r["user_prompt"])]
        try:
            move = str(json.loads(r["raw_response"]).get("move", "")).strip().lower()
        except (ValueError, AttributeError):
            continue
        ids = [i.lower() for i in items]
        if move not in ids or len(ids) < 2:
            continue
        i = ids.index(move)
        sizes.append(len(ids))
        rel.append(i / (len(ids) - 1))
        first_tenth += i < max(1, math.ceil(len(ids) / 10))
    return {"picks": len(rel), "menu_size_mean": round(statistics.mean(sizes), 1) if sizes else None,
            "mean_position": round(statistics.mean(rel), 3) if rel else None,
            "first_tenth_share_pct": _pct(first_tenth, len(rel)),
            "deciles_pct": [_pct(sum(1 for x in rel if min(9, int(x * 10)) == d), len(rel)) for d in range(10)]}


def choices_section(conn) -> dict:
    rows = conn.execute("SELECT move_key, action, intent, area FROM ticks WHERE kind='tick' ORDER BY id").fetchall()
    keys = [r["move_key"] or r["action"] for r in rows]
    verbs = Counter((k or "").split(":")[0] for k in keys)
    labels = Counter(r["action"] for r in rows)
    n = len(rows)
    ent = -sum(c / n * math.log(c / n) for c in verbs.values()) if n else 0.0
    ent_norm = ent / math.log(len(verbs)) if len(verbs) > 1 else 0.0
    repeats = sum(1 for a, b in zip(keys, keys[1:]) if a == b)
    impulses = sum(1 for r in rows if (r["intent"] or "").startswith(IMPULSE))
    areas = Counter(r["area"] for r in rows)
    return {"decisions": n, "distinct_moves": len(set(keys)), "distinct_verbs": len(verbs),
            "verb_entropy_norm": round(ent_norm, 3), "same_move_twice_pct": _pct(repeats, max(n - 1, 0)),
            "impulse_pct": _pct(impulses, n), "top_moves": labels.most_common(10),
            "areas_pct": {a: _pct(c, n) for a, c in areas.most_common()}}


def eating_section(conn, cfg) -> dict:
    """Did it eat when it could? A brain decision is "hungry" when its prompt carries the sustenance need's low or
    empty expression; food is "on the menu" when a consume verb's move was offered. Separates a world with no food
    from a brain that ignores it (the first real run: food offered 51 times, eaten 0)."""
    sus = cfg.settings["sustenance_need"]
    exprs = [e.strip() for b, e in (cfg.character["needs"].get(sus, {}).get("expressions") or {}).items()
             if b in ("low", "empty") and e]
    eat_verbs = {v for v, spec in cfg.moves["verbs"].items() if spec.get("does") == "consume"}
    starts = tuple(cfg.moves["verbs"][v]["label"].split("{target}")[0] for v in eat_verbs)
    out = {"hungry": {"decisions": 0, "food_offered": 0, "ate": 0},
           "not_hungry": {"decisions": 0, "food_offered": 0, "ate": 0}}
    for r in conn.execute("SELECT user_prompt, raw_response FROM llm_calls WHERE kind='brain' AND error IS NULL "
                          "AND user_prompt IS NOT NULL AND raw_response IS NOT NULL"):
        items = dict(MENU.findall(r["user_prompt"]))
        food = [mid for mid, lab in items.items() if starts and lab.startswith(starts)]
        d = out["hungry" if any(e in r["user_prompt"] for e in exprs) else "not_hungry"]
        d["decisions"] += 1
        if food:
            d["food_offered"] += 1
            try:
                d["ate"] += str(json.loads(r["raw_response"]).get("move", "")).strip() in food
            except (ValueError, AttributeError):
                pass
    meals = [r for r in conn.execute("SELECT move_key, intent FROM ticks WHERE kind='tick' AND move_key IS NOT NULL")
             if r["move_key"].split(":")[0] in eat_verbs]
    out["meals"] = len(meals)
    out["reflex_meals"] = sum(1 for r in meals if (r["intent"] or "") == "an impulse (feeding)")
    return out


def needs_section(conn, cfg) -> dict:
    """Time in each band, counted per tick of the creature's life (decisions, carried-on steps and sleep)."""
    rows = conn.execute("SELECT needs_after, mood_after FROM ticks WHERE kind IN ('tick','carry','asleep')").fetchall()
    bands = {nid: Counter() for nid in cfg.character["needs"]}
    any_empty, anx, mood = 0, [], []
    for r in rows:
        nd = json.loads(r["needs_after"] or "{}")
        empty = False
        for nid in bands:
            if nid in nd:
                b = character.need_band(float(nd[nid]), cfg)
                bands[nid][b] += 1
                empty = empty or b == "empty"
        any_empty += empty
        m = json.loads(r["mood_after"] or "{}")
        if "anxiety" in m:
            anx.append(float(m["anxiety"]))
        if "value" in m:
            mood.append(float(m["value"]))
    n = len(rows)
    thr = cfg.settings["anxiety"]["impulse_above"]
    return {"ticks": n,
            "bands_pct": {nid: {b: _pct(c[b], n) for b in character.BANDS} for nid, c in bands.items()},
            "some_need_empty_pct": _pct(any_empty, n),
            "anxiety_mean": round(statistics.mean(anx), 1) if anx else None,
            "anxiety_above_impulse_threshold_pct": _pct(sum(1 for a in anx if a > thr), len(anx)),
            "mood_mean": round(statistics.mean(mood), 1) if mood else None}


def inner_section(conn) -> dict:
    def one(sql: str, default=0):
        try:
            return conn.execute(sql).fetchone()[0]
        except Exception:
            return default
    st = db.get_state(conn) or {}
    return {"long_memories": one("SELECT COUNT(*) FROM memories WHERE kind='long'"),
            "short_memories": one("SELECT COUNT(*) FROM memories WHERE kind='short'"),
            "likes": one("SELECT COUNT(*) FROM preferences WHERE valence>0"),
            "dislikes": one("SELECT COUNT(*) FROM preferences WHERE valence<0"),
            "tendencies": {k: round(v, 2) for k, v in (st.get("tendencies") or {}).items()},
            "self_image_lines": one("SELECT COUNT(*) FROM self_image"),
            "habits": one("SELECT COUNT(*) FROM habits"),
            "projects": dict(conn.execute("SELECT status, COUNT(*) FROM projects GROUP BY status").fetchall())
            if one("SELECT COUNT(*) FROM projects", None) is not None else {},
            "artifacts": one("SELECT COUNT(*) FROM artifacts"),
            "drawings": dict(conn.execute("SELECT status, COUNT(*) FROM drawings GROUP BY status").fetchall())
            if one("SELECT COUNT(*) FROM drawings", None) is not None else {},
            "nights": one("SELECT COUNT(*) FROM sleep_log")}


def measure(conn, cfg) -> dict:
    return {"run": run_section(conn), "brain": brain_section(conn, cfg), "position": position_section(conn),
            "choices": choices_section(conn), "eating": eating_section(conn, cfg), "needs": needs_section(conn, cfg),
            "inner": inner_section(conn)}


def report(m: dict) -> str:
    r, b, p, c, n, i, ea = m["run"], m["brain"], m["position"], m["choices"], m["needs"], m["inner"], m["eating"]
    L = ["== run",
         f"  {r['ticks']} ticks over {r['sim_days']} sim days: {r['decisions']} decisions, {r['carried']} carried-on "
         f"steps, {r['dozes']} dozes (brain offline), {r['nights']} nights",
         "== brain",
         f"  {b['attempts']} attempts, {b['ok']} ok ({b['ok_rate_pct']}%), {b['retries']} retries; errors: "
         f"{b['errors'] or 'none'}",
         f"  latency p50 {b['latency_ms_p50']} ms, p90 {b['latency_ms_p90']} ms; prompt tokens p50 "
         f"{b['prompt_tokens_p50']}, max {b['prompt_tokens_max']} of num_ctx {b['num_ctx']} "
         f"({b['near_ctx_limit_pct']}% of calls within 10% of the limit)",
         f"  other calls: {b['other_calls'] or 'none'}",
         "== menu position of the picked move (no bias: mean about 0.5, first tenth about 10%)",
         f"  {p['picks']} picks, menu size {p['menu_size_mean']}, mean position {p['mean_position']}, "
         f"first tenth {p['first_tenth_share_pct']}%",
         "  by tenth of the menu: " + " ".join(f"{x:.0f}%" for x in p["deciles_pct"]),
         "== choices",
         f"  {c['distinct_moves']} distinct moves, {c['distinct_verbs']} verbs, verb entropy {c['verb_entropy_norm']} "
         f"(0 = always the same, 1 = perfectly even); same move twice in a row {c['same_move_twice_pct']}%; "
         f"impulses {c['impulse_pct']}%",
         "  most picked: " + "; ".join(f"{lab} x{k}" for lab, k in c["top_moves"]),
         "  places: " + ", ".join(f"{a} {v}%" for a, v in c["areas_pct"].items()),
         "== eating (did it eat when it could?)",
         f"  hungry: {ea['hungry']['decisions']} brain decisions, food on the menu in {ea['hungry']['food_offered']}, "
         f"ate by choice {ea['hungry']['ate']}; not hungry: {ea['not_hungry']['decisions']}, food on the menu in "
         f"{ea['not_hungry']['food_offered']}, ate {ea['not_hungry']['ate']}",
         f"  meals in all: {ea['meals']}, of which the feeding reflex took over {ea['reflex_meals']}",
         "== needs (share of ticks in each band)"]
    for nid, d in n["bands_pct"].items():
        L.append(f"  {nid:<14}" + ", ".join(f"{bb} {v:3.0f}%" for bb, v in d.items()))
    L += [f"  some need empty {n['some_need_empty_pct']}%; anxiety mean {n['anxiety_mean']}, above the impulse "
          f"threshold {n['anxiety_above_impulse_threshold_pct']}%; mood mean {n['mood_mean']}",
          "== inner life",
          f"  memories: {i['long_memories']} long, {i['short_memories']} short; likes {i['likes']}, dislikes "
          f"{i['dislikes']}; self-image lines {i['self_image_lines']}; habits {i['habits']}; nights {i['nights']}",
          f"  tendencies: {i['tendencies'] or 'none yet'}",
          f"  projects: {i['projects'] or 'none'}; pieces made {i['artifacts']}",
          f"  drawings: {i['drawings'] or 'none yet'}"]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="database to read (default: settings db_path)")
    ap.add_argument("--config-dir", default=None)
    ap.add_argument("--json", action="store_true", help="print the numbers as JSON")
    a = ap.parse_args()
    cfg = load_config(a.config_dir, {"db_path": a.db} if a.db else None)
    if not Path(cfg.db_path).exists():
        print(f"no database at {cfg.db_path}", file=sys.stderr)
        return 1
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        m = measure(conn, cfg)
    finally:
        conn.close()
    print(json.dumps(m, indent=2) if a.json else report(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Beliefs (docs/individuality-plan.md §5): what it has come to think about what things do to it.

Experiences are one thing; conclusions are another. Each night code counts, for every thing it did, every place it was
in and every kind of weather, how it left the creature (lifted, wore it down) and keeps a belief row per
(kind, subject, outcome) with the evidence FOR and AGAINST, the distinct days that bore it out, and when it was last
borne out. A row is only a *candidate* until enough distinct days bore it out; contradicting evidence weakens it, a strong
belief gives way slowly, one nothing bears out for long enough is dropped. The counting is deliberately naive: it looks
at what went with what, not at causes, so place and weather beliefs can be wrong (and hard to shake), as a creature's can.

Failures (an action that could not happen) are classified by code (circumstance, misunderstanding, ability, assumption,
unexpected) and, after repeating, become a belief about *why*: the creature blames itself or the world according to its
self-criticism, whatever the real cause was (kept in the row's note for the dashboard).
"""
from __future__ import annotations

import json
import random
import sqlite3
from typing import Any

from cat import clock

LIFT, WEAR = "lift", "wear"
KINDS = ("effect", "place", "weather", "failure", "habit")
CURSOR, FAIL_CURSOR = "beliefs.cursor", "beliefs.fail_cursor"
# the reason an action could not happen -> what kind of failure that was (circumstance, misunderstanding, ...)
FAILURE_KINDS = (("moment has passed", "circumstance"), ("not possible here", "circumstance"),
                 ("does not know how", "misunderstanding"), ("no such thing", "misunderstanding"),
                 ("against its nature", "ability"), ("does not work out", "ability"), ("already", "assumption"))


def outcome_of(mood_base: float, cfg: dict) -> str | None:
    if mood_base > cfg["lift_mood"]:
        return LIFT
    if mood_base < -cfg["lift_mood"]:
        return WEAR
    return None


def failure_kind(reason: str) -> str:
    low = reason.lower()
    return next((k for needle, k in FAILURE_KINDS if needle in low), "unexpected")


def _meta(conn: sqlite3.Connection, key: str) -> int:
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return int(r[0]) if r else 0


def plan_updates(conn: sqlite3.Connection, content, texts: dict, cfg: dict, temperament: dict | None, now: str) -> dict:
    """Read the experiences and failures since the last night and work out what they add to each belief (a read
    only step; `apply` writes it)."""
    cursor, fail_cursor = _meta(conn, CURSOR), _meta(conn, FAIL_CURSOR)
    deltas: dict[tuple[str, str, str], dict] = {}
    successes: dict[str, int] = {}
    last = cursor

    def row(kind, subject, outcome):
        return deltas.setdefault((kind, subject, outcome), {"for": 0, "against": 0, "days": set(), "ids": [], "last": ""})

    for r in conn.execute("SELECT id, sim_time, action_id, location, data FROM experiences WHERE kind = 'activity' "
                          "AND id > ? ORDER BY id", (cursor,)):
        last = r["id"]
        data = json.loads(r["data"] or "{}")
        if data.get("status") != "finished" or not r["action_id"]:
            continue
        a = content.actions.get(r["action_id"])
        if a is not None and a.builtin == "sleep":
            continue
        successes[r["action_id"]] = successes.get(r["action_id"], 0) + 1
        if a is not None and a.builtin not in ("move", "sleep") and r["location"]:           # a regular time and place
            band = clock.part_of_day(clock.parse(r["sim_time"]))
            hd = row("habit", f"{r['action_id']}|{r['location']}|{band}", "tends")
            hd["for"] += 1
            hd["days"].add(r["sim_time"][:10])
            hd["ids"].append(r["id"])
            hd["last"] = max(hd["last"], r["sim_time"])
        o = outcome_of(float(data.get("mood_base") or 0.0), cfg)
        subjects = [("effect", r["action_id"]), ("place", r["location"])]
        if data.get("weather"):
            subjects.append(("weather", str(data["weather"])))
        for kind, subject in subjects:
            if not subject:
                continue
            for outcome in (LIFT, WEAR):
                if o == outcome:
                    d = row(kind, subject, outcome)
                    d["for"] += 1
                    d["days"].add(r["sim_time"][:10])
                    d["ids"].append(r["id"])
                    d["last"] = max(d["last"], r["sim_time"])
                elif o is not None:
                    row(kind, subject, outcome)["against"] += 1
    # failures: what could not happen, and how it was classified
    fails: dict[str, dict] = {}
    last_fail = fail_cursor
    for r in conn.execute("SELECT id, started_at AS sim_time, action_id, text FROM activities WHERE status = 'rejected' AND id > ? "
                          "ORDER BY id", (fail_cursor,)):
        last_fail = r["id"]
        reason = (r["text"] or "").split("could not:", 1)[-1].strip().rstrip(".")
        f = fails.setdefault(r["action_id"], {"n": 0, "days": set(), "kinds": {}, "last": ""})
        f["n"] += 1
        f["days"].add(r["sim_time"][:10])
        k = failure_kind(reason)
        f["kinds"][k] = f["kinds"].get(k, 0) + 1
        f["last"] = r["sim_time"]
    for aid, n in successes.items():                              # it worked: that challenges a belief about failing
        for outcome in ("self", "world"):
            b = conn.execute("SELECT statement FROM beliefs WHERE kind = 'failure' AND subject = ? AND outcome = ? "
                             "AND status != 'dropped'", (aid, outcome)).fetchone()
            if b:
                deltas.setdefault(("failure", aid, outcome), {"for": 0, "against": 0, "days": set(), "ids": [], "last": "",
                                                              "statement": b[0]})["against"] += n
    names = {"effect": lambda s: content.actions[s].name if s in content.actions else s,
             "place": lambda s: content.locations[s].name if s in content.locations else s,
             "weather": lambda s: s, "habit": lambda s: s, "failure": lambda s: content.actions[s].name if s in content.actions else s}
    self_crit = float((temperament or {}).get("self_criticism", 0.5))
    failure_rows = []
    for aid, f in fails.items():
        truth = max(f["kinds"], key=f["kinds"].get)
        # it may blame the wrong thing: by temperament, not by what really happened
        blames_self = random.Random(f"{aid}|{f['n']}|{now[:10]}").random() < 0.2 + 0.6 * self_crit
        failure_rows.append({"subject": aid, "outcome": "self" if blames_self else "world", "n": f["n"],
                             "days": sorted(f["days"]), "last": f["last"], "truth": truth,
                             "statement": texts["belief_failure_self" if blames_self else "belief_failure_world"].format(
                                 subject=names["failure"](aid))})
    out = []
    for (kind, subject, outcome), d in deltas.items():
        if d["for"] or d["against"]:
            out.append({"kind": kind, "subject": subject, "outcome": outcome, "for": d["for"], "against": d["against"],
                        "days": sorted(d["days"]), "ids": d["ids"][-6:], "last": d["last"],
                        "statement": d.get("statement") or (
                            _habit_words(content, texts, subject) if kind == "habit"
                            else texts[f"belief_{kind}_{outcome}"].format(subject=names[kind](subject)))})
    return {"rows": out, "failures": failure_rows, "cursor": last, "fail_cursor": last_fail}


def _habit_words(content, texts: dict, subject: str) -> str:
    aid, loc, band = subject.split("|")
    return texts["habit_line"].format(what=content.actions[aid].name, place=content.locations[loc].name, band=band)


def apply(conn: sqlite3.Connection, upd: dict, cfg: dict, now: str, texts: dict | None = None) -> list[str]:
    """Write the night's evidence and settle every belief (inside the caller's transaction). Returns what changed."""
    changes: list[str] = []
    for u in upd["rows"]:
        _bump(conn, u["kind"], u["subject"], u["outcome"], u["for"], u["against"], u["days"], u["ids"], u["statement"],
              u["last"] or now, None)
    for f in upd["failures"]:
        _bump(conn, "failure", f["subject"], f["outcome"], f["n"], 0, f["days"], [], f["statement"], f["last"] or now,
              {"truth": f["truth"]})
    changes += settle(conn, cfg, now)
    if texts is not None:
        changes += sync_identity(conn, cfg, now, texts)
    for key, val in ((CURSOR, upd["cursor"]), (FAIL_CURSOR, upd["fail_cursor"])):
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, str(val)))
    return changes


def _bump(conn, kind, subject, outcome, n_for, n_against, days, ids, statement, last, note) -> None:
    r = conn.execute("SELECT * FROM beliefs WHERE kind = ? AND subject = ? AND outcome = ?",
                     (kind, subject, outcome)).fetchone()
    if r is None:
        conn.execute("INSERT INTO beliefs(kind, subject, outcome, statement, for_count, against_count, days, strength, "
                     "first_seen, last_reinforced, status, sources, note) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, "
                     "'candidate', ?, ?)",
                     (kind, subject, outcome, statement, n_for, n_against, len(days), last, last, json.dumps(ids[-12:]),
                      json.dumps({**(note or {}), "last_day": days[-1] if days else None})))
        return
    old_note = json.loads(r["note"] or "{}")
    new_days = [d for d in days if d != old_note.get("last_day")]
    src = (json.loads(r["sources"]) + ids)[-12:]
    reinforced = last if n_for else r["last_reinforced"]
    status = r["status"]
    if status == "dropped" and n_for:                          # something bears it out again: it is back in play
        status = "candidate"
    conn.execute("UPDATE beliefs SET for_count = for_count + ?, against_count = against_count + ?, days = days + ?, "
                 "last_reinforced = ?, sources = ?, statement = ?, status = ?, note = ? WHERE id = ?",
                 (n_for, n_against, len(new_days), reinforced, json.dumps(src), statement, status,
                  json.dumps({**old_note, **(note or {}), "last_day": days[-1] if days else old_note.get("last_day")}),
                  r["id"]))


def strength_of(r: sqlite3.Row, cfg: dict, now: str) -> float:
    base = r["for_count"] / (r["for_count"] + r["against_count"] + cfg["prior"])
    age = max(0.0, (clock.parse(now) - clock.parse(r["last_reinforced"])).total_seconds() / 86400)
    return base * 0.5 ** (age / cfg["half_life_days"])


def settle(conn: sqlite3.Connection, cfg: dict, now: str) -> list[str]:
    """Candidates become beliefs when enough distinct days bore them out; contradiction challenges a belief (a strong one
    gives way slowly); one nothing has borne out for long enough is dropped."""
    changes = []
    for r in conn.execute("SELECT * FROM beliefs WHERE status != 'dropped'").fetchall():
        total = r["for_count"] + r["against_count"]
        ratio = r["for_count"] / total if total else 0.0
        loose = r["kind"] == "weather"                           # weather is the easiest thing to read too much into
        need_days = cfg["min_days_loose"] if loose else cfg["min_days"]
        need_for = cfg["min_for"]
        expire = cfg["expire_days"]
        if r["kind"] == "failure":
            need_days = need_for = cfg["fail_min"]
        if r["kind"] == "habit":                                  # done on enough different days in the same part of the day
            need_days = need_for = cfg["habit_days"]
            expire = cfg["habit_expire_days"]
        age = (clock.parse(now) - clock.parse(r["last_reinforced"])).total_seconds() / 86400
        status = r["status"]
        if status == "candidate" and r["days"] >= need_days and r["for_count"] >= need_for \
                and (ratio >= cfg["ratio"] or r["kind"] == "habit"):
            status = "active"
            changes.append(f"came to believe: {r['statement']}")
        elif status == "active":
            floor = cfg["drop_ratio"] * (0.75 if r["for_count"] >= 10 else 1.0)
            if ratio < floor:
                status = "dropped"
                changes.append(f"gave up: {r['statement']}")
            elif age > expire:
                status = "dropped"
                changes.append(f"let go (nothing bore it out for {round(age)} days): {r['statement']}")
        elif status == "candidate" and age > expire * 2:
            status = "dropped"
        s = strength_of(r, cfg, now) if status == "active" else 0.0
        if status != r["status"] or abs(s - r["strength"]) > 1e-6:
            conn.execute("UPDATE beliefs SET status = ?, strength = ? WHERE id = ?", (status, s, r["id"]))
    return changes


def sync_identity(conn: sqlite3.Connection, cfg: dict, now: str, texts: dict) -> list[str]:
    """What it has come to do, fear and enjoy becomes part of its sheet as *acquired* identity, one dated version at a
    time with the experiences behind it, and leaves it again when nothing bears it out any more. The written sheet
    (core, temperament, traits) is never touched."""
    from cat import identity
    sheet = identity.current(conn).sheet
    have = {(cat, i["key"]) for cat in identity.ACQUIRED for i in (sheet.get("acquired") or {}).get(cat, [])}
    want: dict[tuple[str, str], tuple[str, list]] = {}
    for r in active(conn):
        key = f"{r['kind']}|{r['subject']}|{r['outcome']}"
        if r["kind"] == "habit":
            want[("habits", key)] = (r["statement"], json.loads(r["sources"]))
        elif r["kind"] in ("effect", "place") and r["outcome"] == WEAR and r["for_count"] >= cfg["fear_min"]:
            want[("fears", key)] = (texts["acquired_fear"].format(statement=r["statement"]), json.loads(r["sources"]))
        elif r["kind"] == "effect" and r["outcome"] == LIFT and r["for_count"] >= cfg["interest_min"]:
            want[("interests", key)] = (texts["acquired_interest"].format(statement=r["statement"]),
                                        json.loads(r["sources"]))
    changes = []
    for (cat, key), (text, ev) in want.items():
        if (cat, key) not in have:
            identity.commit_change(conn, {"kind": "add_acquired", "target": cat, "value": {"key": key, "text": text},
                                          "since": now[:10]}, f"acquired {cat[:-1]}: {text}", ev, now)
            changes.append(f"acquired {cat[:-1]}: {text}")
    for cat, key in sorted(have - set(want)):
        text = next(i["text"] for i in identity.current(conn).sheet["acquired"][cat] if i["key"] == key)
        identity.commit_change(conn, {"kind": "drop_acquired", "target": cat, "value": {"key": key, "text": text}},
                               f"let go of {cat[:-1]}: {text}", [], now)
        changes.append(f"let go of {cat[:-1]}: {text}")
    return changes


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM beliefs WHERE status = 'active' ORDER BY strength DESC").fetchall()


def notes_by_action(conn: sqlite3.Connection, cfg: dict) -> dict[str, list[str]]:
    """Statements about what a thing does to it (and why it fails), by action, for the notes on options."""
    out: dict[str, list[str]] = {}
    for r in active(conn):
        if r["kind"] in ("effect", "failure") and r["strength"] >= cfg["note_min"]:
            out.setdefault(r["subject"], []).append(r["statement"])
    return out


def lines_for_now(conn: sqlite3.Connection, cfg: dict, location: str, weather: str | None) -> list[str]:
    """Beliefs about where it is and what the weather is, the strongest few."""
    out = []
    for r in active(conn):
        if (r["kind"] == "place" and r["subject"] == location) or (r["kind"] == "weather" and r["subject"] == weather):
            out.append(r["statement"])
    return out[: cfg["max_shown"]]

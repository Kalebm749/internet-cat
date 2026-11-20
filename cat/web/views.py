"""DB rows -> the JSON shapes in docs/web-gui.md §5. All words, labels and phases are decided
here (never in JS). Works with an empty or missing database: it falls back to a "first boot"
view built from the config."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from cat import character, clock, db, projects
from cat.config import Config

ARROWS = {"up": "▲", "down": "▼", "flat": "—"}


# ---------- small helpers ----------
def _safe(conn: sqlite3.Connection | None, fn, default):
    """Run a read; a missing DB/table (engine hasn't started yet) yields `default`."""
    if conn is None:
        return default
    try:
        return fn(conn)
    except sqlite3.OperationalError:
        return default


def _ui(cfg: Config) -> dict:
    return cfg.settings["web_ui"]


def first_dt(conn, cfg: Config) -> datetime:
    v = _safe(conn, lambda c: db.get_meta(c, "first_sim_time"), None)
    return clock.parse(v or cfg.settings["sim_start"])


def _state_row(conn, cfg: Config) -> dict:
    st = _safe(conn, db.get_state, None)
    if st:
        return st
    needs = character.initial_needs(cfg)   # first boot: the creature hasn't woken up yet
    return {"sim_time": cfg.settings["sim_start"], "area": cfg.character["start_area"],
            "needs": needs, "mood": character.initial_mood(needs, cfg),
            "energy": float(cfg.settings["energy_start"]), "weather": "clear",
            "tendencies": dict(cfg.character["tendencies"]), "is_asleep": 0, "brain_online": 1}


def _area_name(cfg: Config, area_id: str) -> str:
    return cfg.world["areas"].get(area_id, {}).get("name", area_id)


def _real_label(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    t = clock.time_label(d)
    return t if d.date() == datetime.now().date() else f"{d.strftime('%b')} {d.day}, {t}"


def _need_band_cls(value: float, cfg: Config) -> tuple[bool, bool]:
    """(low, critical) from the need's band: low or empty is low; empty is critical."""
    band = character.need_band(value, cfg)
    return band in ("low", "empty"), band == "empty"


# ---------- panels ----------
def needs_view(conn, cfg: Config, st: dict) -> list[dict]:
    rows = _safe(conn, lambda c: c.execute(
        "SELECT needs_after FROM ticks ORDER BY id DESC LIMIT 2").fetchall(), [])
    cur = json.loads(rows[0]["needs_after"]) if rows else st["needs"]
    prev = json.loads(rows[1]["needs_after"]) if len(rows) > 1 else cur
    out = []
    for nid in cfg.character["needs"]:
        v = float(st["needs"][nid])
        delta = round(float(cur.get(nid, v)) - float(prev.get(nid, v)), 1)
        band = character.need_band(v, cfg)
        low, critical = _need_band_cls(v, cfg)
        trend = "flat" if abs(delta) <= _ui(cfg)["trend_min_delta"] else ("up" if delta > 0 else "down")
        out.append({
            "id": nid, "label": character.need_label(nid, cfg), "value": round(v),
            "band": band, "word": band, "meaning": cfg.character["needs"][nid]["meaning"],
            "delta": delta, "low": low,
            "critical": critical, "trend": trend, "trend_symbol": ARROWS[trend],
            "cls": "critical" if critical else "low" if low else "",
            "title": cfg.character["needs"][nid]["meaning"],
            "aria": f"{character.need_label(nid, cfg)}: {band}. "
                    f"{cfg.character['needs'][nid]['meaning']}"})
    return out


def tendencies_view(conn, cfg: Config, st: dict) -> list[dict]:
    last = _safe(conn, lambda c: c.execute(
        "SELECT tendency_changes FROM sleep_log ORDER BY id DESC LIMIT 1").fetchone(), None)
    changes = json.loads(last["tendency_changes"]) if last and last["tendency_changes"] else {}
    labels = cfg.character.get("tendency_labels", {})
    out = []
    for tid, value in st["tendencies"].items():
        lo, hi = labels.get(tid, ["low", "high"])
        prev = changes.get(tid, {}).get("from", value)
        direction = "up" if value > prev + 1e-9 else "down" if value < prev - 1e-9 else "flat"
        out.append({"id": tid, "label": tid, "low_label": lo, "high_label": hi,
                    "value": round(value, 3), "previous": round(prev, 3),
                    "pct": round(value * 100, 1), "prev_pct": round(prev * 100, 1),
                    "dir": direction, "symbol": "" if direction == "flat" else ARROWS[direction],
                    "aria": f"{tid}: {value:.2f} between {lo} and {hi}"
                            + ("" if direction == "flat" else f", {direction} from {prev:.2f} last night")})
    return out


def preferences_view(conn, cfg: Config) -> dict:
    n = _ui(cfg)["preferences_shown"]

    def q(c, op, order):
        return [dict(r) for r in c.execute(
            f"SELECT subject, valence, reason FROM preferences WHERE valence {op} 0 "
            f"ORDER BY {order} LIMIT ?", (n,))]
    return {"loves": _safe(conn, lambda c: q(c, ">", "valence DESC"), []),
            "dislikes": _safe(conn, lambda c: q(c, "<", "valence ASC"), [])}


def self_image_view(conn, cfg: Config, first) -> dict:
    """What it thinks it is: the optional config hint, then the lines learned in sleep."""
    lines = []
    seed = (cfg.character.get("self_image") or {}).get("seed", "")
    if isinstance(seed, str) and seed.strip():
        lines.append({"text": seed.strip(), "title": "a hint it started with"})
    rows = _safe(conn, lambda c: [dict(r) for r in c.execute(
        "SELECT text, confidence, formed_day FROM self_image ORDER BY formed_day, id")], [])
    for r in rows:
        try:
            day = clock.day_number(first, clock.parse(f"{r['formed_day']}T00:00"))
            when = f"formed on day {day}"
        except Exception:
            when = "formed some time ago"
        lines.append({"text": r["text"], "title": f"{when}, {round(r['confidence'] * 100)}% sure"})
    return {"lines": lines, "empty": not lines, "empty_text": "still figuring that out"}


def mood_value(mood: dict) -> float:
    """The one mood number, -50..50 (an older saved state still has `valence`)."""
    return float(mood.get("value", mood.get("valence", 0.0)))


def presence_view(cfg: Config, mood: dict, state: str, name: str, energy: float = 0.0) -> dict:
    """The abstract presence: colour follows mood, motion follows energy, it dims when asleep and
    desaturates when dozing. All numbers come from settings.yaml web_ui.presence."""
    p = _ui(cfg)["presence"]
    v = max(-1.0, min(1.0, mood_value(mood) / character.MOOD_MAX))            # mood is -50..50
    e = max(0.0, min(1.0, (float(energy) - character.ENERGY_MIN) / (character.ENERGY_MAX - character.ENERGY_MIN)))
    hue = round(p["hue_low_valence"] + (p["hue_high_valence"] - p["hue_low_valence"]) * (v + 1) / 2)
    sat = p["saturation_dozing"] if state == "dozing" else p["saturation_awake"]
    light = {"awake": p["lightness_awake"], "asleep": p["lightness_asleep"],
             "dozing": p["lightness_dozing"]}[state]
    period = round(p["pulse_seconds_tired"] + (p["pulse_seconds_energetic"] - p["pulse_seconds_tired"]) * e, 2)
    return {"hue": hue, "saturation": sat, "lightness": light, "pulse_seconds": period,
            "style": f"--pv-h:{hue};--pv-s:{sat}%;--pv-l:{light}%;--pv-t:{period}s",
            "aria": f"A soft glow for {name}: its colour shows its mood, its motion how much energy it has"}


STATUS_LABEL = {"active": "working on it", "paused": "set aside", "done": "finished"}


def projects_view(conn, cfg: Config) -> dict:
    """What it is working on and the pieces it has made. Stage words only, never the progress number."""
    first = first_dt(conn, cfg)
    pc = cfg.settings["projects"]
    rows = _safe(conn, lambda c: [dict(r) for r in c.execute(
        "SELECT * FROM projects WHERE status IN ('active','paused','done') "
        "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, id DESC LIMIT 8")], [])
    items = []
    for r in rows:
        stage = "finished" if r["status"] == "done" else projects.stage_word(r["progress"], cfg)
        items.append({"id": r["id"], "title": r["title"], "why": r["why"] or "", "note": r["note"] or "",
                      "stage": stage, "status": r["status"], "status_label": STATUS_LABEL[r["status"]],
                      "cls": r["status"], "title_attr": r["why"] or r["title"]})
    arts = _safe(conn, lambda c: [dict(r) for r in c.execute(
        "SELECT a.id, a.kind, a.title, a.text, a.created_sim_time, p.title AS project FROM artifacts a "
        "LEFT JOIN projects p ON p.id = a.project_id ORDER BY a.id DESC LIMIT ?", (pc["artifacts_shown"],))], [])
    artifacts = [{"id": a["id"], "kind": a["kind"] or "piece", "title": a["title"], "text": a["text"],
                  "project": a["project"] or "",
                  "day_text": f"Day {clock.day_number(first, clock.parse(a['created_sim_time']))}"} for a in arts]
    return {"rows": items, "empty": not items, "artifacts": artifacts, "artifacts_empty": not artifacts,
            "empty_text": "no projects yet.", "artifacts_empty_text": "nothing made yet."}


def habits_view(conn, cfg: Config) -> dict:
    """The patterns it keeps repeating, in the words it was given in sleep."""
    rows = _safe(conn, lambda c: [dict(r) for r in c.execute(
        "SELECT text, part FROM habits ORDER BY share*count DESC, id")], [])
    return {"rows": [{"text": r["text"], "title": f"most often in the {r['part']}"} for r in rows],
            "empty": not rows, "empty_text": "none noticed yet."}


def keepsakes_view(conn, cfg: Config, offset: int = 0, limit: int | None = None) -> tuple[list[dict], bool]:
    limit = limit if limit is not None else cfg.settings["keepsakes_first_page"]
    first = first_dt(conn, cfg)
    lo, hi = _ui(cfg)["keepsake_dots"]
    rows = _safe(conn, lambda c: c.execute(
        "SELECT id, text, importance, created_sim_time FROM memories WHERE kind='long' "
        "ORDER BY id DESC LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall(), [])
    out = []
    for r in rows[:limit]:
        imp = float(r["importance"])
        out.append({"id": r["id"], "text": r["text"], "importance": imp,
                    "dots": 1 if imp < lo else 2 if imp < hi else 3,
                    "dot1": "on", "dot2": "on" if imp >= lo else "", "dot3": "on" if imp >= hi else "",
                    "day_number": clock.day_number(first, clock.parse(r["created_sim_time"]))})
        out[-1]["day_text"] = f"Day {out[-1]['day_number']}"
        out[-1]["dots_aria"] = f"importance {out[-1]['dots']} of 3"
    return out, len(rows) > limit


# ---------- diary ----------
def _world_change_text(cfg: Config, c: dict) -> str | None:
    label = c.get("label") or c.get("name") or c.get("object_id") or "something"
    op = c.get("op")
    if op == "create":
        return f"+ {label} ({'appeared' if c.get('source') in ('event', 'director') else 'created'})"
    if op == "consume":
        return f"− {label} (eaten)"
    if op == "take":
        return f"picked up {label}"
    if op == "drop":
        return f"put down {label}"
    if op == "move_object":
        return f"moved {label} to {_area_name(cfg, c.get('area', ''))}"
    return None   # move_self / set_state: not worth a tag


def _tick_entry(conn, cfg: Config, first: datetime, r: sqlite3.Row) -> dict:
    dt = clock.parse(r["sim_time"])
    prev = conn.execute("SELECT needs_after FROM ticks WHERE id<? ORDER BY id DESC LIMIT 1",
                        (r["id"],)).fetchone()
    now_n = json.loads(r["needs_after"])
    # the very first tick has no predecessor: it starts from the configured initial needs
    prev_n = json.loads(prev["needs_after"]) if prev else character.initial_needs(cfg)
    chips = []
    for nid in cfg.character["needs"]:
        d = now_n.get(nid, 0) - prev_n.get(nid, 0)
        if abs(d) >= _ui(cfg)["chip_min_delta"]:
            direction = "up" if d > 0 else "down"
            chips.append({"id": nid, "label": character.need_label(nid, cfg), "dir": direction,
                          "symbol": ARROWS[direction]})
    wc = []
    for c in json.loads(r["changes"] or "[]"):
        text = _world_change_text(cfg, c)
        if text:
            wc.append({"op": c["op"], "label": c.get("label") or c.get("name") or "", "text": text})
    for a in _safe(conn, lambda c: c.execute("SELECT title FROM artifacts WHERE tick_id=? ORDER BY id",
                                             (r["id"],)).fetchall(), []):
        wc.append({"op": "make", "label": a["title"], "text": f"made “{a['title']}”"})
    events = [e["text"] for e in conn.execute("SELECT text FROM events WHERE tick_id=? ORDER BY id",
                                              (r["id"],))]
    am = r["activity_minutes"] if "activity_minutes" in r.keys() else None
    span = f"about {am} min" if am and am > (r["duration"] or 0) else ""          # a longer move, carried on
    nxt = conn.execute("SELECT MIN(id) FROM ticks WHERE id>? AND kind!='carry'", (r["id"],)).fetchone()[0]
    carried = conn.execute("SELECT COUNT(*) FROM ticks WHERE kind='carry' AND id>? AND id<?",
                           (r["id"], nxt if nxt is not None else 2 ** 62)).fetchone()[0]
    why = (r["intent"] or "").strip()
    return {"type": "tick", "key": f"t{r['id']}", "id": r["id"], "span": span,
            "why": why, "why_text": f"because: {why}" if why else "",
            "carried": carried,
            "carried_text": (f"carried on for {carried} more step{'s' if carried != 1 else ''}, "
                             "no new decision") if carried else "",
            "day_number": clock.day_number(first, dt), "day_label": clock.day_label(first, dt),
            "time_label": clock.time_label(dt), "area": _area_name(cfg, r["area"]),
            "action": r["action"] or "", "outcome": r["outcome"] or "", "reaction": r["reaction"] or "",
            "reaction_quoted": f"“{r['reaction']}”" if r["reaction"] else "",
            "events": events, "need_changes": chips, "world_changes": wc}


def _doze_entry(cfg: Config, first: datetime, group: list[sqlite3.Row]) -> dict:
    oldest, newest = group[-1], group[0]          # group is newest-first
    start = clock.parse(oldest["sim_time"])
    end = clock.parse(clock.advance(newest["sim_time"], newest["duration"] or 0))
    return {"type": "doze", "key": f"d{oldest['id']}", "id": newest["id"],
            "day_number": clock.day_number(first, start), "day_label": clock.day_label(first, start),
            "from_label": clock.time_label(start), "to_label": clock.time_label(end),
            "text": f"dozed from {clock.time_label(start)} to {clock.time_label(end)} (brain offline)"}


def _catchup_entry(cfg: Config, first: datetime, r: sqlite3.Row) -> dict:
    """The engine/host was off and `downtime_policy: catch_up` applied the gap. Shown as a doze line."""
    start = clock.parse(r["sim_time"])
    end = clock.parse(clock.advance(r["sim_time"], r["duration"] or 0))
    return {"type": "doze", "key": f"c{r['id']}", "id": r["id"],
            "day_number": clock.day_number(first, start), "day_label": clock.day_label(first, start),
            "from_label": clock.time_label(start), "to_label": clock.time_label(end),
            "text": f"{r['outcome']}, from {clock.time_label(start)} to {clock.time_label(end)}"}


def _sleep_entry(cfg: Config, first: datetime, r: sqlite3.Row) -> dict:
    labels = cfg.character.get("tendency_labels", {})
    d = clock.parse(r["sim_date"] + "T00:00")
    tc = []
    for tid, ch in json.loads(r["tendency_changes"] or "{}").items():
        hi = labels[tid][1] if tid in labels else tid
        up = ch["to"] > ch["from"]
        text = (f"noticed it is {tid}" if ch.get("new") else f"stopped being {tid}" if ch.get("dropped")
                else f"a little more {hi}" if up else f"a little less {hi}")
        tc.append({"id": tid, "dir": "up" if up else "down", "symbol": ARROWS["up" if up else "down"], "text": text})
    prefs = []
    for p in json.loads(r["new_preferences"] or "[]"):
        prefs.append({**p, "text": ("likes " if p["valence"] > 0 else "dislikes ") + p["subject"]})
    return {"type": "sleep", "key": f"s{r['id']}", "id": r["tick_id"] + 0.5 if r["tick_id"] else 0,
            "day_number": clock.day_number(first, d), "day_label": clock.day_label(first, d),
            "summary": r["summary"] or "", "tendency_changes": tc, "new_preferences": prefs}


def diary_view(conn, cfg: Config, before: int | None = None,
               limit: int | None = None) -> tuple[list[dict], int | None]:
    """Newest-first entries. Consecutive dozes collapse into one; asleep ticks are hidden (the
    sleep card stands for them). Returns (entries, next_before cursor or None)."""
    if conn is None:
        return [], None
    limit = min(limit or cfg.settings["diary_page_size"], _ui(cfg)["max_page_size"])
    first = first_dt(conn, cfg)
    try:
        cards: dict[int, list[sqlite3.Row]] = {}
        for s in conn.execute("SELECT * FROM sleep_log WHERE tick_id>0"):
            cards.setdefault(s["tick_id"], []).append(s)
        entries: list[dict] = []
        group: list[sqlite3.Row] = []
        cursor = before if before is not None else 2 ** 62
        while True:
            rows = conn.execute("SELECT * FROM ticks WHERE id<? ORDER BY id DESC LIMIT 200",
                                (cursor,)).fetchall()
            if not rows:
                if group:
                    entries.append(_doze_entry(cfg, first, group))
                return entries, None
            for r in rows:
                cursor = r["id"]
                if r["id"] in cards:                       # the "last night" card sits above its tick
                    if group:
                        entries.append(_doze_entry(cfg, first, group))
                        group = []
                    entries += [_sleep_entry(cfg, first, s) for s in cards[r["id"]]]
                if r["kind"] == "doze":
                    group.append(r)
                    continue
                if group:
                    entries.append(_doze_entry(cfg, first, group))
                    group = []
                if r["kind"] == "tick":
                    entries.append(_tick_entry(conn, cfg, first, r))
                elif r["kind"] == "catchup":
                    entries.append(_catchup_entry(cfg, first, r))
                if len(entries) >= limit:
                    more = conn.execute("SELECT 1 FROM ticks WHERE id<? AND kind IN ('tick','doze','catchup') "
                                        "LIMIT 1", (r["id"],)).fetchone()
                    return entries, (r["id"] if more else None)
    except sqlite3.OperationalError:
        return [], None


# ---------- the whole state ----------
def build_state(conn: sqlite3.Connection | None, cfg: Config) -> dict:
    st = _state_row(conn, cfg)
    ch = cfg.character
    first = first_dt(conn, cfg)
    dt = clock.parse(st["sim_time"])
    asleep = bool(st["is_asleep"])
    brain_online = bool(st["brain_online"])
    state = "asleep" if asleep else ("awake" if brain_online else "dozing")
    last_seen_iso = _safe(conn, lambda c: db.get_meta(c, "brain_last_seen"), None)
    last_seen = _real_label(last_seen_iso)
    model = _safe(conn, lambda c: db.get_meta(c, "brain_model"), None) or cfg.settings["brain_model"]
    area_name = _area_name(cfg, st["area"])
    energy = float(st["energy"])
    latest = _safe(conn, lambda c: c.execute("SELECT COALESCE(MAX(id),0) FROM ticks").fetchone()[0], 0)

    diary, next_before = diary_view(conn, cfg)
    keepsakes, more = keepsakes_view(conn, cfg)
    mood = st["mood"]
    mv = mood_value(mood)
    slider_pct = (mv - character.MOOD_MIN) / (character.MOOD_MAX - character.MOOD_MIN) * 100
    thr = cfg.settings["mood"]["word_threshold"]
    return {
        "creature": {"name": ch["name"], "core_traits": ch["core_traits"],
                     "pronoun": ch.get("pronoun", "they")},
        "time": {"sim_time": st["sim_time"], "day_number": clock.day_number(first, dt),
                 "label": f"{clock.day_label(first, dt)} · {clock.time_label(dt)}",
                 "day_label": clock.day_label(first, dt), "time_label": clock.time_label(dt),
                 "part": clock.part_of_day(dt), "phase": clock.phase(dt, cfg.settings["phases"])},
        "location": {"id": st["area"], "name": area_name},
        "status": {"state": state, "brain_online": brain_online, "brain_last_seen": last_seen_iso,
                   "model": model, "model_title": f"model: {model}",
                   "pill": {"awake": "awake", "asleep": "asleep · dreaming", "dozing": "dozing"}[state],
                   "brain_text": "brain: connected" if brain_online else
                                 f"brain: last seen {last_seen}" if last_seen else "brain: not seen yet"},
        "presence": {**presence_view(cfg, mood, state, ch["name"], energy),
                     "caption": f"{ch['name']}, in {area_name}"},
        "mood": {"word": mood["word"], "description": mood["description"],
                 "value": mv, "slider_pct": round(slider_pct, 1),
                 "aria": f"Mood slider: {mood['word']}, "
                         f"{'positive' if mv > thr else 'negative' if mv < -thr else 'in the middle'}, "
                         f"from negative to positive"},
        "energy": {"value": round(energy), "pct": round(energy - character.ENERGY_MIN),
                   "word": character.energy_word(energy, cfg),
                   "aria": f"energy: {character.energy_word(energy, cfg)}, {energy:.0f} on a scale from -50 to 50"},
        "needs": needs_view(conn, cfg, st),
        "tendencies": tendencies_view(conn, cfg, st),
        "preferences": preferences_view(conn, cfg),
        "projects": projects_view(conn, cfg),
        "habits": habits_view(conn, cfg),
        "self_image": self_image_view(conn, cfg, first),
        "latest_tick_id": latest,
        "diary": diary, "diary_next_before": next_before,
        "keepsakes": keepsakes, "keepsakes_has_more": more,
        "poll_seconds": _ui(cfg)["poll_seconds"],
    }

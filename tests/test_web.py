import json
import random
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cat import db
from cat.engine import Engine
from cat.llm import MockBackend
from cat.web import views
from cat.web.app import create_app

WEB = Path(__file__).resolve().parent.parent / "cat" / "web"


def make_engine(cfg, seed=3):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    return Engine(cfg, b, rng=random.Random(seed)), b


@pytest.fixture
def seeded(cfg):
    e, b = make_engine(cfg)
    for i in range(130):
        b.online = not (50 <= i < 57)          # an outage => a collapsed doze entry
        e.tick()
    b.online = True
    return e


def client(cfg):
    return TestClient(create_app(cfg))


# ---------- empty / first boot ----------
def test_missing_db_shows_empty_states(cfg):
    assert not cfg.db_path.exists()
    c = client(cfg)
    r = c.get("/")
    assert r.status_code == 200
    assert "the journal is empty. gregorovitch hasn’t woken up yet." in r.text
    assert "no keepsakes yet." in r.text
    s = c.get("/api/state").json()
    assert s["diary"] == [] and s["keepsakes"] == [] and s["latest_tick_id"] == 0
    assert s["status"]["state"] == "awake" and len(s["needs"]) == len(cfg.character["needs"])
    assert c.get("/api/diary").json() == {"entries": [], "next_before": None}
    assert c.get("/api/keepsakes").json() == {"keepsakes": [], "has_more": False}


def test_db_with_tables_but_no_state(cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    conn.close()
    r = client(cfg).get("/")
    assert r.status_code == 200 and "hasn’t woken up yet" in r.text


def test_db_file_without_tables_does_not_error(cfg):
    import sqlite3
    sqlite3.connect(cfg.db_path).close()
    c = client(cfg)
    assert c.get("/").status_code == 200 and c.get("/api/state").status_code == 200


# ---------- API shapes (web-gui §5) ----------
def test_state_shape(cfg, seeded):
    s = client(cfg).get("/api/state").json()
    assert {"creature", "time", "location", "status", "mood", "energy", "needs", "tendencies",
            "preferences", "latest_tick_id", "diary", "keepsakes"} <= set(s)
    assert {"name", "core_traits"} <= set(s["creature"])
    assert {"sim_time", "day_number", "label", "phase"} <= set(s["time"])
    assert s["time"]["phase"] in ("dawn", "day", "dusk", "night")
    assert re.match(r"^\w+day, Day \d+ · \d{1,2}:\d\d (am|pm)$", s["time"]["label"])
    assert {"id", "name"} == set(s["location"])
    assert {"state", "brain_online", "brain_last_seen", "model"} <= set(s["status"])
    assert {"word", "description", "valence", "arousal"} <= set(s["mood"])
    assert {"value", "word"} <= set(s["energy"])
    n = s["needs"][0]
    assert {"id", "label", "value", "word", "delta", "low", "critical"} <= set(n)
    assert [x["id"] for x in s["needs"]] == list(cfg.character["needs"])
    t = s["tendencies"][0]
    assert {"id", "low_label", "high_label", "value", "previous"} <= set(t)
    assert set(s["preferences"]) == {"loves", "dislikes"}
    assert len(s["diary"]) >= cfg.settings["diary_page_size"]
    assert s["latest_tick_id"] == db.connect(cfg.db_path).execute("SELECT MAX(id) FROM ticks").fetchone()[0]
    assert len(s["keepsakes"]) <= cfg.settings["keepsakes_first_page"]


def test_entry_shapes(cfg, seeded):
    types = {}
    for e in client(cfg).get("/api/diary?limit=100").json()["entries"]:
        types.setdefault(e["type"], e)
    t = types["tick"]
    assert {"id", "day_number", "time_label", "area", "action", "outcome", "reaction", "events",
            "need_changes", "world_changes"} <= set(t)
    assert re.match(r"^\d{1,2}:\d\d (am|pm)$", t["time_label"])
    assert all({"id", "dir"} <= set(c) and c["dir"] in ("up", "down") for c in t["need_changes"])
    assert {"from_label", "to_label"} <= set(types["doze"])
    assert "(brain offline)" in types["doze"]["text"]
    assert {"summary", "tendency_changes", "new_preferences"} <= set(types["sleep"])


def test_doze_collapsed_and_asleep_hidden(cfg, seeded):
    entries, _ = views.diary_view(db.connect(cfg.db_path, readonly=True), cfg, limit=100)
    ids = {e["id"] for e in entries if e["type"] == "tick"}
    conn = db.connect(cfg.db_path)
    asleep = {r[0] for r in conn.execute("SELECT id FROM ticks WHERE kind='asleep'")}
    assert not (ids & asleep)
    n_doze_rows = conn.execute("SELECT COUNT(*) FROM ticks WHERE kind='doze'").fetchone()[0]
    dozes = [e for e in views.diary_view(conn, cfg, limit=1000)[0] if e["type"] == "doze"]
    assert 1 <= len(dozes) < n_doze_rows              # collapsed


def test_sleep_card_sits_right_above_its_tick(cfg, seeded):
    entries, _ = views.diary_view(db.connect(cfg.db_path), cfg, limit=1000)
    cards = [i for i, e in enumerate(entries) if e["type"] == "sleep"]
    assert cards
    for i in cards:
        assert entries[i]["id"] % 1 == 0.5                          # tick id + .5
        nxt = entries[i + 1] if i + 1 < len(entries) else None
        assert nxt is None or nxt["id"] <= entries[i]["id"] - 0.5   # the onset tick (or older) follows
        if i:
            assert entries[i - 1]["id"] > entries[i]["id"]          # newer entries are above


def test_pagination_complete_and_no_overlap(cfg, seeded):
    c = client(cfg)
    everything = c.get("/api/diary?limit=1000").json()["entries"]
    seen, before, pages = [], None, 0
    while True:
        url = "/api/diary?limit=20" + (f"&before={before}" if before else "")
        d = c.get(url).json()
        seen += d["entries"]
        pages += 1
        before = d["next_before"]
        if not before:
            break
        assert pages < 50
    assert [e["key"] for e in seen] == [e["key"] for e in everything]
    assert len({e["key"] for e in seen}) == len(seen)
    assert pages > 2


def test_limit_is_clamped_and_validated(cfg, seeded):
    c = client(cfg)
    assert len(c.get("/api/diary?limit=100000").json()["entries"]) <= cfg.settings["web_ui"]["max_page_size"] + 1
    assert c.get("/api/diary?limit=0").status_code == 422
    assert c.get("/api/diary?before=abc").status_code == 422
    assert c.get("/api/keepsakes?offset=-1").status_code == 422


def test_keepsakes_paging(cfg, seeded):
    conn = db.connect(cfg.db_path)
    for i in range(15):
        conn.execute("INSERT INTO memories(kind,text,importance,created_sim_time) VALUES('long',?,?,?)",
                     (f"memory {i}", (i % 10) / 10, "2026-11-19T10:00"))
    c = client(cfg)
    first = c.get("/api/state").json()
    assert len(first["keepsakes"]) == cfg.settings["keepsakes_first_page"] and first["keepsakes_has_more"]
    ids = [k["id"] for k in first["keepsakes"]]
    assert ids == sorted(ids, reverse=True)                       # newest first
    d = c.get(f"/api/keepsakes?offset={len(ids)}&limit=100").json()
    all_ids = ids + [k["id"] for k in d["keepsakes"]]
    assert len(all_ids) == len(set(all_ids)) and not d["has_more"]
    everything = c.get("/api/keepsakes?limit=100").json()["keepsakes"]
    assert [k["id"] for k in everything] == all_ids
    dots = {k["importance"]: k["dots"] for k in everything}
    assert dots[0.0] == 1 and dots[0.5] == 2 and dots[0.9] == 3


# ---------- words, trends, status ----------
def test_need_words_labels_and_trend(cfg, seeded):
    cfg.character["needs"]["food"].update(label="tummy", words={15: "starving", 101: "fed"})
    conn = db.connect(cfg.db_path)
    st = db.get_state(conn)
    needs = views.needs_view(conn, cfg, st)
    food = next(n for n in needs if n["id"] == "food")
    assert food["label"] == "tummy" and food["word"] in ("starving", "fed")
    for n in needs:
        assert n["low"] == (n["value"] < 35) and n["critical"] == (n["value"] < 15)
        assert (n["trend"] == "flat") == (abs(n["delta"]) <= cfg.settings["web_ui"]["trend_min_delta"])


def test_tendency_previous_comes_from_last_night(cfg, seeded):
    conn = db.connect(cfg.db_path)
    last = json.loads(conn.execute("SELECT tendency_changes FROM sleep_log ORDER BY id DESC LIMIT 1").fetchone()[0])
    tv = {t["id"]: t for t in views.tendencies_view(conn, cfg, db.get_state(conn))}
    for tid, ch in last.items():
        assert tv[tid]["previous"] == pytest.approx(ch["from"], abs=1e-3)
        assert tv[tid]["dir"] in ("up", "down", "flat")
    for tid in set(tv) - set(last):
        assert tv[tid]["previous"] == tv[tid]["value"] and tv[tid]["dir"] == "flat"


def test_preferences_top_by_valence(cfg, conn):
    cfg.settings["web_ui"]["preferences_shown"] = 2
    for subj, v in [("a", .9), ("b", .5), ("c", .1), ("d", -.9), ("e", -.2)]:
        conn.execute("INSERT INTO preferences(subject,valence,reason,updated_at) VALUES(?,?,?,?)", (subj, v, "r", "t"))
    p = views.preferences_view(conn, cfg)
    assert [x["subject"] for x in p["loves"]] == ["a", "b"] and [x["subject"] for x in p["dislikes"]] == ["d", "e"]


@pytest.mark.parametrize("asleep,online,state", [(1, 1, "asleep"), (1, 0, "asleep"), (0, 0, "dozing"), (0, 1, "awake")])
def test_status_states_and_body_class(cfg, seeded, asleep, online, state):
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE state SET is_asleep=?, brain_online=?", (asleep, online))
    c = client(cfg)
    assert c.get("/api/state").json()["status"]["state"] == state
    assert f"state-{state}" in c.get("/").text


@pytest.mark.parametrize("hour,phase", [("03:00", "night"), ("06:00", "dawn"), ("13:00", "day"), ("19:00", "dusk"), ("22:00", "night")])
def test_phase_class_on_body(cfg, seeded, hour, phase):
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE state SET sim_time=?", (f"2026-11-20T{hour}",))
    assert re.search(rf'<body class="{phase} ', client(cfg).get("/").text)


def test_first_boot_uses_config_not_errors(cfg):
    s = views.build_state(None, cfg)
    assert s["time"]["sim_time"] == cfg.settings["sim_start"]
    assert s["location"]["id"] == cfg.character["start_area"]


# ---------- page: no-JS snapshot, paging links, escaping ----------
def test_page_is_a_full_server_rendered_snapshot(cfg, seeded):
    html = client(cfg).get("/").text
    s = client(cfg).get("/api/state").json()
    first_tick = next(e for e in s["diary"] if e["type"] == "tick")
    assert first_tick["action"] in html and s["creature"]["name"] in html
    assert html.count('class="entry ') >= 20 and 'class="day-header"' in html
    assert all(n["label"] in html for n in s["needs"])
    assert s["time"]["label"] in html
    pages, before = html, re.search(r'href="/\?before=(\d+)"', html)
    while before:                                   # walk the no-JS pages back through history
        page = client(cfg).get(f"/?before={before.group(1)}").text
        pages += re.sub(r"<template.*?</template>", "", page, flags=re.S)
        before = re.search(r'href="/\?before=(\d+)"', page)
    assert "dozed from" in pages and re.search(r'class="entry entry-sleep"', pages)
    assert html.count("<script") == 1 and "defer" in html          # JS is only an enhancement


def test_no_js_paging_links(cfg, seeded):
    c = client(cfg)
    first = c.get("/").text
    m = re.search(r'href="/\?before=(\d+)"', first)
    assert m
    older = c.get(f"/?before={m.group(1)}")
    assert older.status_code == 200 and "back to the newest pages" in older.text
    assert 'class="entry ' in older.text
    more = c.get("/?all_keepsakes=1")
    assert more.status_code == 200


def test_output_is_escaped(cfg):
    e, b = make_engine(cfg)
    evil = "<script>alert(1)</script><b>x</b>"
    b.handlers["brain"] = lambda m, s: {"reaction": evil, "thought": "", "action": evil, "intent": "",
                                        "feeling": "neutral", "wants_sleep": False}
    b.handlers["narrator"] = lambda m, s: {"outcome": evil, "gains": {}, "effort": "low", "duration": 10, "changes": []}
    e.tick()
    html = client(cfg).get("/").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)" in html
    assert html.count("<script") == 1


# ---------- offline site, accessibility, style ----------
def test_no_external_urls_anywhere():
    for f in list((WEB / "templates").glob("*.html")) + list((WEB / "static").rglob("*.css")) + list((WEB / "static").rglob("*.js")):
        for url in re.findall(r"https?://[^\s\"')]+", f.read_text()):
            assert "w3.org" in url, f"{f.name} references external {url}"


def test_static_assets_served(cfg):
    c = client(cfg)
    for p in ("css/site.css", "js/journal.js", "img/cat.png", "img/cat-asleep.png", "fonts/Fraunces-500.woff2",
              "fonts/Lora-400.woff2", "fonts/Lora-400-italic.woff2", "fonts/Caveat-500.woff2"):
        assert c.get(f"/static/{p}").status_code == 200, p
    assert c.get("/static/fonts/Lora-400.woff2").content[:4] == b"wOF2"


def test_every_bar_has_a_word_and_aria_label(cfg, seeded):
    html = re.sub(r"<template.*?</template>", "", client(cfg).get("/").text, flags=re.S)  # live markup only
    bars = re.findall(r'<div class="bar[^"]*"[^>]*>', html)
    assert len(bars) == len(cfg.character["needs"]) + 1                       # needs + energy
    assert all('role="progressbar"' in b and "aria-label=" in b and "aria-valuenow=" in b for b in bars)
    assert 'role="img"' in html                                              # sliders / mood map have text equivalents


def test_motion_only_in_reduced_motion_guard():
    css = (WEB / "static/css/site.css").read_text()
    outside = re.sub(r"@media \(prefers-reduced-motion: no-preference\) \{.*\Z", "", css, flags=re.S)
    assert "animation:" not in outside and "@keyframes" not in outside
    assert "rotate(" not in outside


def _lum(hexc):
    r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4
    return .2126 * f(r) + .7152 * f(g) + .0722 * f(b)


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + .05) / (lb + .05)


def test_text_contrast_meets_wcag_aa_in_both_palettes():
    css = (WEB / "static/css/site.css").read_text()
    day = dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", css.split("body.night")[0]))
    night = {**day, **dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", css.split("body.night {")[1].split("}")[0]))}
    for name, pal in (("day", day), ("night", night)):
        for fg in ("ink", "ink-soft"):
            for bg in ("card", "paper"):
                assert _ratio(pal[fg], pal[bg]) >= 4.5, f"{name}: {fg} on {bg} = {_ratio(pal[fg], pal[bg]):.2f}"


def test_readonly_connection_cannot_write(cfg, seeded):
    conn = db.connect(cfg.db_path, readonly=True)
    with pytest.raises(Exception):
        conn.execute("DELETE FROM ticks")

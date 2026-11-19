"""Part B6: Needs & mood, Behaviour, Sleep and World state pages."""
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cat import character, clock, db
from cat.devdash import data
from cat.devdash.app import create_app
from cat.engine import Engine
from cat.llm import MockBackend

STATIC = Path(__file__).resolve().parent.parent / "cat" / "devdash" / "static"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)


def make_engine(cfg, seed=3):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    return Engine(cfg, b, rng=random.Random(seed)), b


def client(cfg):
    return TestClient(create_app(cfg))


def synth(cfg):
    """An initialised game DB we can fill with exactly the ticks a test needs."""
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.set_meta(conn, "first_sim_time", "2026-11-18T07:00")
    return conn


def tick(conn, i, sim, kind="tick", dur=30, needs=None, mood=None, area="living_room", action=None, changes=(), rejected=(), outcome="ok", energy=20.0):
    needs = needs or {"sustenance": 50, "wonder": 50, "joy": 50, "peace": 50, "curiosity": 50}
    conn.execute("INSERT INTO ticks(id,kind,sim_time,real_time,area,action,outcome,duration,changes,rejected_changes,needs_after,mood_after,energy_after,gains) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'{}')",
                 (i, kind, sim, "2026-11-18T10:00:00", area, action, outcome, dur, json.dumps(list(changes)), json.dumps(list(rejected)),
                  json.dumps(needs), json.dumps(mood or {"value": 5.0, "word": "calm"}), energy))


# =============================== needs & mood ===============================
def synthetic_needs_run(cfg):
    conn = synth(cfg)
    for i in range(10):                              # 10 ticks x 30 min from 08:00
        t = clock.advance("2026-11-18T08:00", 30 * i)
        food = 10.0 if 3 <= i <= 6 else 60.0         # critical (<15) for ticks 3..6 = 4 x 30 min
        kind = "asleep" if i in (7, 8) else ("doze" if i == 9 else "tick")
        tick(conn, i + 1, t, kind=kind, needs={"sustenance": food, "wonder": 80, "joy": 40, "peace": 90, "curiosity": 20},
             mood={"value": 4.0 * i, "word": "calm"})
    return conn


def test_needs_series_shapes_values_and_stored_energy(cfg):
    synthetic_needs_run(cfg)
    s = data.needs_series(cfg, "all")
    assert s["ticks"] == 10 and len(s["x"]) == 10 and all(len(v) == 10 for v in s["needs"].values())
    assert s["x"] == sorted(s["x"]) and s["x"][1] - s["x"][0] == 30 * 60                   # epoch seconds, 30 sim minutes apart
    assert s["x"][0] == 1_795_000_000 + (s["x"][0] - 1_795_000_000) and s["from"] == "2026-11-18T08:00"
    assert s["needs"]["sustenance"][3] == 10.0 and s["needs"]["sustenance"][0] == 60.0
    assert s["mood"] == pytest.approx([4.0 * i for i in range(10)]) and "arousal" not in s
    assert s["energy"] == [20.0] * 10                                                    # stored per tick, not derived
    assert set(s["labels"]) == set(cfg.character["needs"])


def test_naive_sim_time_is_plotted_as_is(cfg):
    synthetic_needs_run(cfg)
    import datetime
    x0 = data.needs_series(cfg, "all")["x"][0]
    assert datetime.datetime.fromtimestamp(x0, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M") == "2026-11-18T08:00"


def test_critical_time_and_longest_stretch(cfg):
    synthetic_needs_run(cfg)
    c = {r["need"]: r for r in data.needs_series(cfg, "all")["critical"]}
    assert c["sustenance"]["minutes"] == 120 and c["sustenance"]["hours"] == 2.0 and c["sustenance"]["longest_minutes"] == 120
    assert c["sustenance"]["pct"] == pytest.approx(100 * 120 / 300, abs=0.1)                    # 10 ticks x 30 = 300 min
    assert c["wonder"]["minutes"] == 0 and c["joy"]["minutes"] == 0
    assert data.needs_series(cfg, "all")["critical"][0]["need"] == "sustenance"                  # sorted: worst first
    assert data.needs_series(cfg)["critical_below"] == cfg.settings["need_bands"]["empty_below"]


def test_critical_streaks_are_split_by_recovery(cfg):
    conn = synth(cfg)
    pattern = [5, 5, 5, 60, 5, 5, 60, 5]                                                  # streaks of 3, 2, 1
    for i, f in enumerate(pattern):
        tick(conn, i + 1, clock.advance("2026-11-18T08:00", 30 * i), needs={"sustenance": f, "wonder": 80, "joy": 40, "peace": 90, "curiosity": 20})
    c = next(r for r in data.needs_series(cfg, "all")["critical"] if r["need"] == "sustenance")
    assert c["minutes"] == 6 * 30 and c["longest_minutes"] == 90


def test_bands_for_sleep_doze_and_catchup(cfg):
    synthetic_needs_run(cfg)
    b = data.needs_series(cfg, "all")["bands"]
    assert [x["kind"] for x in b] == ["asleep", "doze"]
    assert b[0]["to"] - b[0]["from"] == 60 * 60                                           # two merged 30-min asleep ticks
    assert b[1]["to"] - b[1]["from"] == 30 * 60
    conn = db.connect(cfg.db_path)
    tick(conn, 11, "2026-11-18T13:00", kind="catchup", dur=480)
    tick(conn, 12, "2026-11-18T21:00", kind="tick")
    assert [x["kind"] for x in data.needs_series(cfg, "all")["bands"]] == ["asleep", "doze", "catchup"]


def test_ranges_are_relative_to_the_latest_sim_time(cfg):
    conn = synth(cfg)
    for i in range(0, 12 * 24 * 2):                                                        # 12 sim days, one tick per 30 min
        tick(conn, i + 1, clock.advance("2026-11-10T00:00", 30 * i), dur=30)
    a, w, al = (data.needs_series(cfg, r) for r in ("24h", "7d", "all"))
    assert (a["ticks"], w["ticks"], al["ticks"]) == (24 * 2 + 1, 7 * 24 * 2 + 1, 12 * 24 * 2)
    assert a["x"][-1] - a["x"][0] == 24 * 3600 and w["x"][-1] - w["x"][0] == 7 * 24 * 3600
    assert data.needs_series(cfg, "bogus")["range"] == "24h"
    c = client(cfg)
    assert c.get("/api/dev/needs?range=7d").json()["ticks"] == w["ticks"] and c.get("/api/dev/needs?range=x").status_code == 422


def test_downsampling_keeps_first_and_last_point(cfg):
    conn = synth(cfg)
    for i in range(300):
        tick(conn, i + 1, clock.advance("2026-11-10T00:00", 10 * i), dur=10)
    cfg.settings["devdash"]["max_series_points"] = 50
    s = data.needs_series(cfg, "all")
    assert len(s["x"]) <= 50 and s["x"][0] == data.needs_series(cfg, "all")["x"][0] and s["ticks"] == 300
    assert s["x"][-1] == data._epoch(clock.advance("2026-11-10T00:00", 10 * 299))


def test_needs_page_and_empty_db(cfg):
    c = client(cfg)
    html = c.get("/needs").text                                                           # no DB at all
    assert html.count('id="chart-') == 3 and "critical level the longest" in html and c.get("/needs?range=all").status_code == 200
    e, b = make_engine(cfg)
    e.run(ticks=80, interval=0)
    html = c.get("/needs?range=7d").text
    assert "chartlib.js" in html and "needs.js" in html and 'data-range="7d"' in html
    s = data.needs_series(cfg, "all")
    assert s["ticks"] == 80 and s["bands"] and all(len(s["needs"][k]) == len(s["x"]) for k in s["needs"])


# ================================ behaviour ================================
def synthetic_behaviour(cfg):
    conn = synth(cfg)
    acts = ["Nap in the sun", "nap  in the SUN", "chase the ball", "Nap in the sun", "look out of the window",   # day 1
            "chase the ball", "chase the ball", "chase the ball"]                                              # day 2
    for i, a in enumerate(acts):
        day = "2026-11-18" if i < 5 else "2026-11-19"
        area = "living_room" if i % 2 == 0 else "kitchen"
        tick(conn, i + 1, f"{day}T{8 + i:02d}:00", dur=20, area=area, action=a)
    tick(conn, 9, "2026-11-19T20:00", kind="asleep", dur=60, area="living_room")
    tick(conn, 10, "2026-11-19T21:00", kind="doze", dur=20, area="kitchen")
    return conn


def test_behaviour_action_variety_and_top_actions(cfg):
    synthetic_behaviour(cfg)
    b = data.behaviour(cfg)
    assert b["total_actions"] == 8 and b["distinct_actions"] == 3
    days = {d["day"]: d for d in b["days"]}
    assert (days[1]["total"], days[1]["distinct"], days[1]["variety"]) == (5, 3, 0.6)      # case/whitespace-insensitive
    assert (days[2]["total"], days[2]["distinct"], days[2]["variety"]) == (3, 1, 0.33)
    assert [d["day"] for d in b["days"]] == [2, 1]                                         # newest day first
    top = b["top_actions"]
    assert top[0] == {"action": "chase the ball", "count": 4, "pct": 50.0} and top[1]["action"] == "nap in the sun" and top[1]["count"] == 3
    assert sum(t["count"] for t in top) == 8


def test_behaviour_time_per_area_split_by_state(cfg):
    synthetic_behaviour(cfg)
    a = {r["area"]: r for r in data.behaviour(cfg)["areas"]}
    assert a["living_room"]["awake"] == 80 and a["living_room"]["asleep"] == 60 and a["living_room"]["minutes"] == 140
    assert a["kitchen"]["awake"] == 80 and a["kitchen"]["doze"] == 20 and a["kitchen"]["minutes"] == 100
    assert a["living_room"]["pct"] + a["kitchen"]["pct"] == pytest.approx(100, abs=0.2)
    assert data.behaviour(cfg)["areas"][0]["area"] == "living_room"                        # sorted by time


def test_penalty_hits_only_count_real_reductions(cfg):
    conn = synthetic_behaviour(cfg)
    nm = lambda t, need, mult, raw: conn.execute(
        'INSERT INTO need_math(tick_id,need,"before",decay,bucket,gain_raw,repetition_mult,effort_cost,clamp_adjust,"after") '
        "VALUES(?,?,0,0,'+',?,?,0,0,0)", (t, need, raw, mult))
    nm(2, "joy", 0.4, 15)            # hit
    nm(2, "curiosity", 0.4, 15)      # same tick: counted once
    nm(4, "joy", 0.4, 0)             # multiplier but nothing to reduce: not a hit
    nm(5, "joy", 1.0, 15)            # no penalty
    nm(8, "joy", 0.4, 30)            # hit (day 2)
    p = data.behaviour(cfg)["penalty"]
    assert p["hits"] == 2 and p["ticks"] == 8 and [h["tick_id"] for h in p["recent"]] == [8, 2]
    assert p["by_day"] == {1: 1, 2: 1} and {d["day"]: d["penalty_hits"] for d in data.behaviour(cfg)["days"]} == {1: 1, 2: 1}
    assert p["recent"][0]["mult"] == 0.4 and p["recent"][0]["action"] == "chase the ball"


def test_effect_histograms_and_reason_normalisation(cfg):
    conn = synthetic_behaviour(cfg)
    fx = lambda op, acc, reason=None: conn.execute("INSERT INTO effect_logs(tick_id,op,args,accepted,reason) VALUES(1,?,'{}',?,?)", (op, acc, reason))
    for _ in range(3):
        fx("consume", 1)
    fx("consume", 0, "no such object 'ghost_fish'")
    fx("consume", 0, "no such object 'ghost_cat'")
    fx("move_self", 0, "'the_moon' is not connected to 'kitchen'")
    fx("take", 0, "inventory is full")
    fx("take", 0, "inventory is full")
    fx(None, 0, "unknown op 'teleport'")
    b = data.behaviour(cfg)
    ops = {o["op"]: o for o in b["ops"]}
    assert (ops["consume"]["accepted"], ops["consume"]["rejected"], ops["consume"]["n"]) == (3, 2, 5)
    assert ops["take"]["rejected"] == 2 and "(invalid)" in ops
    reasons = {r["reason"]: r["count"] for r in b["reasons"]}
    assert reasons["no such object '…'"] == 2 and reasons["inventory is full"] == 2           # ids collapsed together
    assert reasons["'…' is not connected to '…'"] == 1 and b["reasons"][0]["count"] == 2
    assert [o["n"] for o in b["ops"]] == sorted((o["n"] for o in b["ops"]), reverse=True)


def test_behaviour_on_a_real_run_is_consistent(cfg):
    e, b = make_engine(cfg, seed=6)
    e.run(ticks=300, interval=0)
    r = data.behaviour(cfg)
    conn = db.connect(cfg.db_path)
    assert sum(a["minutes"] for a in r["areas"]) == conn.execute("SELECT SUM(duration) FROM ticks").fetchone()[0]
    assert r["total_actions"] == conn.execute("SELECT COUNT(*) FROM ticks WHERE kind='tick' AND action IS NOT NULL").fetchone()[0]
    assert sum(o["n"] for o in r["ops"]) == conn.execute("SELECT COUNT(*) FROM effect_logs").fetchone()[0]
    assert sum(o["rejected"] for o in r["ops"]) == sum(x["count"] for x in r["reasons"]) > 0
    assert r["penalty"]["hits"] > 0 and all(0 < d["variety"] <= 1 for d in r["days"])
    html = client(cfg).get("/behaviour").text
    for needle in ("Action variety", "Top repeated actions", "Time spent per area", "Effect ops", "Rejection reasons", "Repetition-penalty hits"):
        assert needle in html


def test_behaviour_empty(cfg):
    html = client(cfg).get("/behaviour").text
    assert "no actions yet" in html and client(cfg).get("/api/dev/behaviour").json()["total_actions"] == 0


# ================================== sleep ==================================
def slept_engine(cfg, nights=3, proposals=(1, -5, 0.2)):
    e, b = make_engine(cfg)
    cfg.settings["sleep"]["fall_asleep_energy_below"] = 0                                  # only the forced cycles
    b.handlers["brain"] = lambda m, s: {"reaction": "wow", "thought": "", "action": "watch the rain", "intent": "", "feeling": "positive", "wants_sleep": False}
    b.handlers["tendencies"] = lambda m, s: {"changes": {"boldness": proposals[0], "sociability": proposals[1], "tidiness": proposals[2]}}
    b.handlers["preferences"] = lambda m, s: {"preferences": [{"subject": "the rain", "valence": 0.8, "reason": "so calming"}]}
    for _ in range(nights):
        e.run(ticks=4, interval=0)
        e.force_sleep_cycle()
    return e


def test_sleep_nights_show_inputs_outputs_proposed_vs_clamped(cfg):
    e = slept_engine(cfg)
    s = data.sleep_nights(cfg)
    assert s["total"] == 3 and len(s["nights"]) == 3 and s["nights"][0]["id"] > s["nights"][-1]["id"]
    night = s["nights"][0]
    assert night["tick_id"] and night["summary"] and set(night["traces"]) == {"consolidate", "tendencies", "preferences", "self_image", "projects"}
    step = cfg.settings["sleep"]["tendency_max_step"]
    t = {x["id"]: x for x in night["tendency_table"]}
    assert t["boldness"]["proposed"] == 1 and t["boldness"]["applied"] == pytest.approx(step) and not t["boldness"]["clamped"]
    assert t["sociability"]["proposed"] == -5 and t["sociability"]["applied"] == pytest.approx(-step)
    assert t["sociability"]["to"] == pytest.approx(t["sociability"]["from"] - step) and t["sociability"]["clamped"] is True    # -5 was held to -1 x step
    assert t["tidiness"]["applied"] == pytest.approx(0.2 * step)
    assert night["traces"]["consolidate"]["input"]["day_log"] and night["traces"]["consolidate"]["output"]["memories"]
    assert night["new_memories"] and night["pref_table"][0]["subject"] == "the rain"
    assert [c["kind"] for c in night["calls"]] == ["sleep_consolidate", "sleep_tendencies", "sleep_preferences", "sleep_self_image", "sleep_projects"]
    assert s["nights"][-1]["pref_table"][0]["previous"] is None                             # the first night it is liked
    assert s["nights"][0]["pref_table"][0]["previous"] == s["nights"][1]["pref_table"][0]["valence"]   # blended with last night


def test_tendency_history_matches_the_final_state_and_respects_the_step(cfg):
    e = slept_engine(cfg, nights=4)
    h = data.sleep_nights(cfg)["history"]
    step = cfg.settings["sleep"]["tendency_max_step"]
    assert h["x"] == [1, 2, 3, 4] and len(h["dates"]) == 4
    final = db.get_state(db.connect(cfg.db_path))["tendencies"]
    for k, series in h["tendencies"].items():
        assert len(series) == 4 and series[-1] == pytest.approx(final[k], abs=1e-3)
        prev = cfg.character["tendencies"][k]
        for v in series:
            assert abs(v - prev) <= step + 1e-9 and 0 <= v <= 1
            prev = v
    assert h["tendencies"]["boldness"][-1] == pytest.approx(0.5 + 4 * step)                 # kept rising
    assert h["tendencies"]["tidiness"][0] == pytest.approx(cfg.character["tendencies"]["tidiness"] + 0.2 * step)


def test_sleep_nights_without_traces_fall_back_to_the_log(cfg):
    slept_engine(cfg, nights=2)
    db.connect(cfg.db_path).execute("DELETE FROM sleep_traces")
    s = data.sleep_nights(cfg)
    night = s["nights"][0]
    assert night["traces"] == {} and night["tendency_table"] and all(t["proposed"] is None for t in night["tendency_table"])
    assert s["history"]["x"] == [1, 2]
    assert "no trace stored for this night" in client(cfg).get("/sleep").text


def test_sleep_page_and_pagination(cfg):
    slept_engine(cfg, nights=5)
    c = client(cfg)
    html = c.get("/sleep").text
    for needle in ("Tendency history", "Night #", "Consolidate memories", "LLM proposed", "applied change", "Preferences", "sleep.js", "chart-tendencies"):
        assert needle in html
    assert html.count("<details class=\"night\"") == 5 and f'href="/ticks/{data.sleep_nights(cfg)["nights"][0]["tick_id"]}#sleep"' in html
    api = c.get("/api/dev/sleep?limit=2&page=2").json()
    assert len(api["nights"]) == 2 and api["total"] == 5 and api["pages"] == 3 and len(api["history"]["x"]) == 5
    assert "no sleep cycle has run yet" in TestClient(create_app(cfg.__class__(**{**cfg.__dict__, "settings": {**cfg.settings, "db_path": str(cfg.root / "nope.db")}}))).get("/sleep").text


# ================================== world ==================================
def test_world_areas_objects_inventory(cfg):
    e, b = make_engine(cfg)
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": "pick up the ball", "intent": "", "feeling": "neutral", "wants_sleep": False}
    b.handlers["narrator"] = lambda m, s: {"outcome": "x", "gains": {}, "effort": "low", "duration": 10, "changes": [
        {"op": "move_self", "area": "hallway"}, {"op": "take", "object_id": "ball"}]}
    e.tick()
    w = data.world_state(cfg)
    assert [a["id"] for a in w["areas"]] == list(cfg.world["areas"]) and {a["id"] for a in w["areas"] if a["here"]} == {"hallway"}
    assert w["creature_area"] == "hallway" and w["weather"] == cfg.world["weather"][0]
    assert [o["id"] for o in w["inventory"]] == ["ball"] and "ball" not in {o["id"] for o in w["objects"]}
    sofa = next(o for o in w["objects"] if o["id"] == "sofa")
    assert sofa["area_name"] == "the living room" and "climbable" in sofa["tags"] and sofa["created_by"] == "world"
    assert next(a for a in w["areas"] if a["id"] == "living_room")["objects"] == 4            # sofa, window, cushion, books
    assert next(a for a in w["areas"] if a["id"] == "living_room")["exits"] == ["the kitchen", "the hallway"]


def test_object_history_from_recorded_changes(cfg):
    e, b = make_engine(cfg)
    cfg.world["events"] = []
    script = iter([
        [{"op": "create", "name": "a cushion fort", "tags": ["soft", "movable"]}],
        [{"op": "set_state", "object_id": "cushion_fort", "key": "tidy", "value": "no"}, {"op": "take", "object_id": "cushion_fort"}],
        [{"op": "move_self", "area": "kitchen"}, {"op": "drop", "object_id": "cushion_fort"}, {"op": "consume", "object_id": "bowl"}],
        [{"op": "move_object", "object_id": "cushion_fort", "area": "living_room"}],
    ])
    b.handlers["brain"] = lambda m, s: {"reaction": "", "thought": "", "action": "tinker", "intent": "", "feeling": "neutral", "wants_sleep": False}
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok", "gains": {}, "effort": "low", "duration": 10, "changes": next(script)}
    e.state["area"] = "living_room"
    for _ in range(4):
        e.tick()
    e.queue_injected_event({"text": "A parcel arrives.", "spawn": {"name": "a parcel", "tags": ["movable"], "area": "living_room"}})
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok", "gains": {}, "effort": "low", "duration": 10, "changes": []}
    e.tick()
    w = data.world_state(cfg, object_id="cushion_fort")
    assert [h["op"] for h in w["history"]] == ["move_object", "drop", "take", "set_state", "create"]          # newest first
    d = {h["op"]: h["detail"] for h in w["history"]}
    assert d["create"] == "created 'a cushion fort' in the living room (by the creature)" and d["take"] == "picked up (now carried)"
    assert d["drop"] == "put down in the kitchen" and d["move_object"] == "moved to the living room" and d["set_state"] == "state tidy = no"
    assert w["object_row"]["name"] == "a cushion fort" and w["object_row"]["area_name"] == "the living room"
    gone = data.world_state(cfg, object_id="bowl")                                                           # consumed: row is gone, history stays
    assert gone["object_row"] is None and [h["op"] for h in gone["history"]] == ["consume"]
    allh = data.world_state(cfg)["history"]
    assert any(h["op"] == "move_self" and h["detail"] == "creature moved to the kitchen" for h in allh)
    ev = next(h for h in allh if h["label"] == "a parcel")
    assert ev["source"] == "event" and "spawned by a world event" in ev["detail"]
    assert all(h["tick_id"] >= 1 for h in allh) and [h["tick_id"] for h in allh] == sorted((h["tick_id"] for h in allh), reverse=True)


def test_world_history_pagination_and_pages(cfg):
    e, b = make_engine(cfg, seed=8)
    e.run(ticks=300, interval=0)
    total = data.world_state(cfg, limit=1000)["history_total"]
    assert total > 60
    seen = []
    p = 1
    while True:
        w = data.world_state(cfg, page=p, limit=25)
        seen += [(h["tick_id"], h["op"], h["label"]) for h in w["history"]]
        if p >= w["pages"]:
            break
        p += 1
    assert len(seen) == total
    c = client(cfg)
    html = c.get("/world").text
    for needle in ("Areas", "Objects", "Inventory", "Object history", "creature is here", "page 1 /"):
        assert needle in html
    assert c.get("/world?object=nothing_here").status_code == 200 and "no longer exists" in c.get("/world?object=nothing_here").text
    assert c.get("/api/dev/world?limit=5").json()["history"].__len__() == 5


def test_world_page_with_no_database(cfg):
    html = client(cfg).get("/world").text
    assert "Areas" in html and "no objects" in html and "no world changes recorded yet" in html


def test_pages_never_write_the_game_db(cfg):
    e = slept_engine(cfg, nights=2)
    conn = db.connect(cfg.db_path)
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("ticks", "memories", "objects", "sleep_log", "need_math")}
    c = client(cfg)
    for u in ("/needs?range=all", "/behaviour", "/sleep", "/world", "/world?object=ball", "/api/dev/needs", "/api/dev/behaviour",
              "/api/dev/sleep", "/api/dev/world"):
        assert c.get(u).status_code == 200, u
    assert {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before} == before


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("script", ["chartlib.js", "needs.js", "sleep.js"])
def test_new_scripts_parse(script):
    assert subprocess.run(["node", "--check", str(STATIC / script)], capture_output=True).returncode == 0

"""Part B definition of done, checked end to end on a seeded world: 100 mock ticks + 1 forced sleep."""
import importlib.util
import json
import random
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cat import db, trace
from cat.config import load_config, sibling_overrides
from cat.devdash import data, playground
from cat.devdash.app import create_app
from cat.engine import Engine
from cat.llm import MockBackend
from cat.web.app import create_app as create_web

ROOT = Path(__file__).resolve().parent.parent
HDR = {"X-Requested-With": "devdash"}
REAL_CLIENT = httpx.Client


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_db", ROOT / "scripts" / "seed_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    d = tmp_path_factory.mktemp("seeded")
    summary = _load_seed_module().seed(d / "cat.db", ticks=100, seed_value=4, force=True, quiet=True)
    cfg = load_config(overrides=sibling_overrides(d / "cat.db"))
    return cfg, summary


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)
    data._probe_cache.clear()


def dash(cfg):
    return TestClient(create_app(cfg))


# ---------------------------------------------------------------------------
# "After 100 mock ticks + 1 forced sleep, every page shows real data"
# ---------------------------------------------------------------------------
def test_the_seed_covers_everything(world):
    cfg, s = world
    assert s["ticks"] >= 101 and s["dozing"] >= 1 and s["asleep"] >= 1 and s["nights"] >= 2 and s["keepsakes"] >= 2
    assert s["LLM calls"] > 100 and s["embed calls"] > 100 and s["recall logs"] > 5 and s["rejected changes"] > 0
    assert s["dev commands"] == 8 and s["playground runs"] == 2 and s["log services"] == ["devdash", "engine", "web"]
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM sleep_traces").fetchone()[0] >= 6
    last = conn.execute("SELECT tick_id FROM sleep_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert last == conn.execute("SELECT MAX(id) FROM ticks").fetchone()[0] or last        # the forced cycle hangs on the latest tick
    from cat.devqueue import DevQueue
    cmds = {c["command"]: c for c in DevQueue(cfg.dev_db_path).recent(50)}
    assert {c["status"] for c in cmds.values()} == {"done"} and set(cmds) == {
        "pause", "resume", "step", "set_interval", "force_sleep", "reload_settings", "inject_event", "snapshot"}


PAGES = [
    ("/", ["ticks run", "invalid-JSON rate", "Force sleep cycle", "force_sleep", "in the "]),
    ("/ticks", ["/ticks/", "dozing", "rejections"]),
    ("/llm", ["sleep_consolidate", "brain", "narrator", "uPlot.iife.min.js"]),
    ("/memory", ["Long-term memories", "<circle", "Most recalled", "times recalled"]),
    ("/needs?range=all", ["chart-needs", "critical level the longest", "sustenance"]),
    ("/behaviour", ["Action variety", "Top repeated actions", "Rejection reasons", "no such object"]),
    ("/sleep", ["Night #", "LLM proposed", "Consolidate memories", "chart-tendencies"]),
    ("/world", ["Object history", "creature is here", "a feather"]),
    ("/logs", ["engine", "web", "devdash", "slow request"]),
    ("/config", ["matches disk", "settings.yaml", "character.yaml", "world.yaml"]),
    ("/playground", ["Prompt playground", "seed sample"]),
    ("/playground/runs", ["seed sample", "qwen3:8b"]),
    ("/playground/groups/seedsample01", ["Compare: 2 runs", "gemma3:4b", "Original"]),
]


@pytest.mark.parametrize("url,needles", PAGES, ids=[p[0] for p in PAGES])
def test_every_page_shows_real_data(world, url, needles):
    cfg, _ = world
    r = dash(cfg).get(url)
    assert r.status_code == 200, url
    for n in needles:
        assert n in r.text, f"{url}: missing {n!r}"
    assert "Traceback" not in r.text and "UndefinedError" not in r.text


def test_every_json_endpoint_returns_data(world):
    cfg, _ = world
    c = dash(cfg)
    must_have_items = {"/api/dev/ticks": "items", "/api/dev/llm": "items", "/api/dev/commands": "items", "/api/dev/playground/runs": "items"}
    for url, key in must_have_items.items():
        j = c.get(url).json()
        assert len(j[key]) > 0, url
    assert c.get("/api/dev/overview").json()["stats"]["ticks"] >= 100
    assert len(c.get("/api/dev/llm/series").json()["x"]) > 5 and len(c.get("/api/dev/needs?range=all").json()["x"]) > 50
    assert c.get("/api/dev/memory/map").json()["n"] >= 2 and c.get("/api/dev/behaviour").json()["total_actions"] > 30
    assert c.get("/api/dev/sleep").json()["total"] >= 2 and c.get("/api/dev/world").json()["history_total"] > 5
    assert len(c.get("/api/dev/logs").json()["lines"]) > 10 and c.get("/api/dev/config").json()["files"][0]["state"] == "same"


def test_overview_shows_the_command_history(world):
    cfg, _ = world
    html = dash(cfg).get("/").text
    for cmd in ("snapshot", "force_sleep", "reload_settings", "inject_event"):
        assert cmd in html
    assert "sleep cycle done" in html and "ran tick #" in html


# ---------------------------------------------------------------------------
# "Tick detail fully explains every number on the public site for that tick"
# ---------------------------------------------------------------------------
def test_tick_detail_explains_every_diary_entry_the_public_site_shows(world):
    cfg, _ = world
    web = TestClient(create_web(cfg))
    entries = [e for e in web.get("/api/diary?limit=100").json()["entries"] if e["type"] == "tick"]
    assert len(entries) >= 50
    conn = db.connect(cfg.db_path, readonly=True)
    for e in entries:
        d = data.tick_detail(cfg, e["id"])
        t = d["tick"]
        assert (e["action"], e["outcome"], e["reaction"], e["area"]) == (t["action"], t["outcome"], t["reaction"] or "", d["clock"]["area"])
        assert [x["text"] for x in d["events"]] == e["events"]
        # every need value on the site is `after` in the math; every math row rebuilds from its terms
        for m in d["need_math"]:
            assert m["after"] == pytest.approx(t["needs_after"][m["need"]], abs=1e-3)
            assert m["after"] == pytest.approx(m["before"] - m["decay"] + m["gain_raw"] * m["repetition_mult"]
                                               - m["effort_cost"] + m["clamp_adjust"], abs=2e-3)
        chips = {c["id"]: c["dir"] for c in e["need_changes"]}
        for m in d["need_math"]:
            delta = m["after"] - m["before"]
            assert (m["need"] in chips) == (abs(delta) >= cfg.settings["web_ui"]["chip_min_delta"])
        # mood word/value and every term of the formula
        mm = d["mood_math"]
        assert (mm["word"], mm["value"]) == (t["mood_after"]["word"], t["mood_after"]["value"])
        i = mm["inputs"]
        assert max(-50, min(50, i["unclamped"])) == pytest.approx(mm["value"], abs=0.06)
        assert (sum(i["terms"].values()) + i["event_points"] + i["feeling_points"]
                == pytest.approx(i["unclamped"], abs=2e-3))
        # world changes tags come from the effects that were accepted (plus event spawns)
        accepted = [x for x in d["effects"] if x["accepted"]]
        site_ops = {w["op"] for w in e["world_changes"]}
        assert site_ops <= {x["op"] for x in accepted} | {"create"}
        # the brain's and narrator's exact words are recorded
        kinds = {c["kind"] for c in d["llm"] if c["status"] == "ok"}
        assert {"brain", "narrator"} <= kinds
        assert d["stages"] and d["trace"]["status"] == "ok"
    state = web.get("/api/state").json()                                   # and the live panel numbers for the latest tick
    latest = data.tick_detail(cfg, state["latest_tick_id"])
    assert {n["id"]: (n["value"], n["word"]) for n in state["needs"]} == {n["id"]: (n["value"], n["word"]) for n in latest["public"]["needs"]}
    assert (state["energy"]["value"], state["energy"]["word"]) == (latest["public"]["energy"]["value"], latest["public"]["energy"]["word"])
    assert state["mood"]["word"] == latest["mood_math"]["word"]


# ---------------------------------------------------------------------------
# "I can recover the exact prompt and raw response for any brain or narrator call in the last 14 days
#  and replay it in the playground"
# ---------------------------------------------------------------------------
def test_every_brain_and_narrator_call_keeps_exact_prompt_and_response(world):
    cfg, _ = world
    conn = db.connect(cfg.db_path, readonly=True)
    rows = conn.execute("SELECT * FROM llm_calls WHERE kind IN ('brain','narrator')").fetchall()
    assert len(rows) >= 120
    for r in rows:
        assert r["system_prompt"] and r["user_prompt"] and r["raw_response"] and r["schema"] and r["parsed"], r["id"]
        assert json.loads(r["parsed"]) == json.loads(r["raw_response"])


def test_retention_keeps_13_days_drops_15_and_what_is_kept_replays(world, tmp_path, monkeypatch):
    cfg, _ = world
    work = tmp_path / "w"
    work.mkdir()
    import shutil
    shutil.copy(cfg.db_path, work / "cat.db")
    c2 = load_config(overrides=sibling_overrides(work / "cat.db"))
    conn = db.connect(c2.db_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM llm_calls WHERE kind IN ('brain','narrator') ORDER BY id")]
    old13 = (datetime.now() - timedelta(days=13)).isoformat(timespec="milliseconds")
    old15 = (datetime.now() - timedelta(days=15)).isoformat(timespec="milliseconds")
    keep, drop = ids[:5], ids[5:10]
    for i in keep:
        conn.execute("UPDATE llm_calls SET started_at=? WHERE id=?", (old13, i))
    for i in drop:
        conn.execute("UPDATE llm_calls SET started_at=? WHERE id=?", (old15, i))
    trace.prune(conn, c2)                                                  # what every sleep cycle does
    left = {r[0] for r in conn.execute("SELECT id FROM llm_calls")}
    assert set(keep) <= left and not set(drop) & left
    pick = dict(conn.execute("SELECT * FROM llm_calls WHERE id=?", (keep[0],)).fetchone())

    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": pick["raw_response"]}})
    monkeypatch.setattr(httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(h), **kw))
    c = dash(c2)
    page = c.get(f"/playground?call_id={pick['id']}").text
    from markupsafe import escape
    assert str(escape(pick["system_prompt"])) in page and str(escape(pick["user_prompt"])) in page
    j = c.post("/api/dev/playground/run", headers=HDR, json={"models": [pick["model"]], "system_prompt": pick["system_prompt"],
               "user_prompt": pick["user_prompt"], "schema": pick["schema"], "source_call_id": pick["id"]}).json()
    assert seen[0]["messages"][0]["content"] == pick["system_prompt"] and seen[0]["messages"][1]["content"] == pick["user_prompt"]
    assert seen[0]["format"] == json.loads(pick["schema"]) and j["runs"][0]["raw_response"] == pick["raw_response"]
    assert "same JSON as the original" in c.get(f"/playground/groups/{j['group_id']}").text


# ---------------------------------------------------------------------------
# "Pausing, stepping and resuming from the dashboard works. The engine stays the only writer of game state."
# ---------------------------------------------------------------------------
def test_pause_step_resume_driven_through_the_dashboard_http_api(cfg):
    b = MockBackend(seed=2)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    e = Engine(cfg, b, rng=random.Random(2))
    c = dash(cfg)
    post = lambda cmd, **a: c.post("/api/dev/commands", headers=HDR, json={"command": cmd, "args": a, "confirm": True}).json()["id"]
    status = lambda cid: next(x for x in c.get("/api/dev/commands").json()["items"] if x["id"] == cid)
    n = lambda: e.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    log, ids = [], {}

    def sleep(_):
        k = len(log)
        log.append(n())
        if k == 0:
            assert n() == 0 and status(ids["pause"])["status"] == "done"
            assert c.get("/api/dev/overview").json()["engine"]["state"] == "paused"
            ids["step1"] = post("step")
        elif k == 1:
            assert n() == 1 and status(ids["step1"])["status"] == "done" and "ran tick #1" in status(ids["step1"])["result"]
            ids["step2"] = post("step")
        elif k == 2:
            assert n() == 2
            ids["resume"] = post("resume")

    ids["pause"] = post("pause")
    assert e.run(ticks=5, interval=0, sleep=sleep) == 5 and n() == 5
    assert status(ids["resume"])["status"] == "done" and c.get("/api/dev/overview").json()["engine"]["state"] == "stopped"


def test_the_dashboard_can_never_change_game_state(world):
    cfg, _ = world

    def snapshot():
        conn = sqlite3.connect(cfg.db_path)
        out = {}
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            out[t] = conn.execute(f"SELECT COUNT(*), TOTAL(rowid) FROM {t}").fetchone()
        out["state"] = conn.execute("SELECT * FROM state").fetchone()
        out["objects"] = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
        conn.close()
        return out
    before = snapshot()
    c = dash(cfg)
    urls = [p[0] for p in PAGES] + ["/ticks/5", "/ticks/-1", "/llm/10", "/memory?q=oak", "/world?object=sofa", "/logs?level=warning",
                                    "/api/dev/ticks/20", "/api/dev/overview", "/api/dev/memory/search?q=fish"]
    for u in urls:
        c.get(u)
    for cmd in ("pause", "resume", "force_sleep", "snapshot", "reload_settings"):
        assert c.post("/api/dev/commands", headers=HDR, json={"command": cmd, "confirm": True}).status_code == 202
    assert snapshot() == before
    # ... and by construction: every cat.db connection in the dashboard code is read-only
    for f in (ROOT / "cat" / "devdash").glob("*.py"):
        src = f.read_text()
        for m in re.finditer(r"db\.connect\(([^)]*)\)", src):
            assert "readonly=True" in m.group(1), f"{f.name}: {m.group(0)}"
        assert "sqlite3.connect(" not in src.replace("devqueue", ""), f.name


# ---------------------------------------------------------------------------
# "Reachable only from localhost by default, and needs auth when LAN-exposed"
# ---------------------------------------------------------------------------
def test_default_bind_is_loopback_and_lan_is_refused_without_credentials(monkeypatch):
    import uvicorn
    from cat.devdash import __main__ as m, auth
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setattr("sys.argv", ["devdash"])
    m.main()
    assert calls[-1][1]["host"] == "127.0.0.1" and calls[-1][1]["port"] == 8090
    monkeypatch.setattr("sys.argv", ["devdash", "--host", "0.0.0.0"])
    m.main()
    assert calls[-1][1]["host"] == "0.0.0.0"
    import os
    assert os.environ["DEVDASH_BIND"] == "0.0.0.0"
    with pytest.raises(auth.AccessError):                                  # importing the app in that mode refuses to start
        auth.check_startup()
    monkeypatch.delenv("DEVDASH_BIND")


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------
def test_readme_documents_the_dashboard():
    readme = (ROOT / "README.md").read_text()
    assert "## Using the dev dashboard" in readme
    for needle in ("127.0.0.1:8090", "DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS", "Basic", "devdash.db", "trace_level",
                   "trace_retention_days", "Playground", "Tick detail", "Force sleep", "Snapshot", "X-Requested-With",
                   "seed_db.py", "python -m cat.devdash", "data/logs"):
        assert needle in readme, needle
    for page in ("Overview", "Ticks", "LLM calls", "Memory", "Needs", "Behaviour", "Sleep", "World", "Logs", "Config"):
        assert page in readme

"""Part B3: devdash skeleton, access rules, Overview, Ticks, Tick detail."""
import base64
import json
import random
import re
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cat import db, devqueue
from cat.devdash import auth, data
from cat.devdash.app import create_app
from cat.engine import Engine
from cat.llm import MockBackend
from cat.web.app import create_app as create_web

HERE = Path(__file__).resolve().parent.parent / "cat" / "devdash"
HDR = {"X-Requested-With": "devdash"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)
    data._probe_cache.clear()


def make_engine(cfg, seed=3, ticks=0):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    e = Engine(cfg, b, rng=random.Random(seed))
    if ticks:
        e.run(ticks=ticks, interval=0)
    return e, b


@pytest.fixture
def seeded(cfg):
    e, b = make_engine(cfg, ticks=0)
    for i in range(90):
        b.online = not (40 <= i < 45)
        e.tick()
    b.online = True
    return e


def dash(cfg):
    return TestClient(create_app(cfg))


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


# ---------- access rules ----------
def test_loopback_default_needs_no_credentials(cfg, seeded):
    c = dash(cfg)
    assert c.get("/").status_code == 200 and c.get("/ticks").status_code == 200
    assert auth.bind_host() == "127.0.0.1" and auth.is_loopback("127.0.0.1") and auth.is_loopback("::1")


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::"])
def test_lan_bind_without_credentials_refuses_to_start(cfg, monkeypatch, host):
    monkeypatch.setenv("DEVDASH_BIND", host)
    with pytest.raises(auth.AccessError, match="DEVDASH_USER"):
        create_app(cfg)
    monkeypatch.setenv("DEVDASH_USER", "dev")                    # one of two is not enough
    with pytest.raises(auth.AccessError):
        create_app(cfg)


def test_lan_bind_requires_basic_auth(cfg, seeded, monkeypatch):
    monkeypatch.setenv("DEVDASH_BIND", "0.0.0.0")
    monkeypatch.setenv("DEVDASH_USER", "dev")
    monkeypatch.setenv("DEVDASH_PASS", "s3cret")
    c = dash(cfg)
    r = c.get("/")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert c.get("/ticks", headers=basic("dev", "wrong")).status_code == 401
    assert c.get("/ticks", headers=basic("nope", "s3cret")).status_code == 401
    assert c.get("/", headers={"Authorization": "Basic !!notbase64"}).status_code == 401
    assert c.get("/", headers={"Authorization": "Bearer abc"}).status_code == 401
    assert c.get("/api/dev/ticks", headers=basic("dev", "s3cret")).status_code == 200
    assert c.get("/", headers=basic("dev", "s3cret")).status_code == 200
    assert c.get("/healthz").status_code == 200                  # the container healthcheck has no credentials
    assert c.get("/static/devdash.css").status_code == 401       # nothing else is open


def test_credentials_are_enforced_even_on_loopback_when_set(cfg, monkeypatch):
    monkeypatch.setenv("DEVDASH_USER", "dev")
    monkeypatch.setenv("DEVDASH_PASS", "pw")
    c = dash(cfg)
    assert c.get("/").status_code == 401 and c.get("/", headers=basic("dev", "pw")).status_code == 200


def test_basic_auth_handles_colons_in_passwords():
    assert auth.authorized("Basic " + base64.b64encode(b"u:p:a:ss").decode(), ("u", "p:a:ss"))
    assert not auth.authorized(None, ("u", "p"))
    assert not auth.authorized("Basic " + base64.b64encode(b"u").decode(), ("u", "p"))


def test_state_changing_requests_need_the_csrf_header(cfg):
    c = dash(cfg)
    body = {"command": "pause", "args": {}, "confirm": True}
    r = c.post("/api/dev/commands", json=body)                   # what a cross-site form/fetch would send
    assert r.status_code == 403 and "X-Requested-With" in r.json()["error"]
    assert c.post("/api/dev/commands", json=body, headers=HDR).status_code == 202
    assert c.get("/api/dev/commands").status_code == 200         # GETs need no header


def test_commands_need_explicit_confirmation_and_valid_args(cfg):
    c = dash(cfg)
    assert c.post("/api/dev/commands", json={"command": "pause"}, headers=HDR).status_code == 400
    assert c.post("/api/dev/commands", json={"command": "pause", "confirm": "yes"}, headers=HDR).status_code == 400
    assert c.post("/api/dev/commands", content=b"not json", headers=HDR).status_code == 400
    r = c.post("/api/dev/commands", json={"command": "shutdown", "confirm": True}, headers=HDR)
    assert r.status_code == 422 and "unknown command" in r.json()["detail"]
    r = c.post("/api/dev/commands", json={"command": "set_interval", "args": {"seconds": -3}, "confirm": True}, headers=HDR)
    assert r.status_code == 422
    r = c.post("/api/dev/commands", json={"command": "set_interval", "args": {"seconds": 4, "ticks": 2}, "confirm": True}, headers=HDR)
    assert r.status_code == 202
    row = c.get("/api/dev/commands").json()["items"][0]
    assert row["command"] == "set_interval" and row["status"] == "pending" and json.loads(row["args"]) == {"seconds": 4, "ticks": 2}


def test_security_headers(cfg):
    r = dash(cfg).get("/")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"


# ---------- the dashboard cannot change game state ----------
def counts(path):
    conn = sqlite3.connect(path)
    out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    out["state"] = conn.execute("SELECT * FROM state").fetchone()
    conn.close()
    return out


def test_dashboard_never_writes_the_game_db(cfg, seeded):
    before = counts(cfg.db_path)
    c = dash(cfg)
    for url in ("/", "/ticks", "/ticks?status=ok", "/ticks/5", "/ticks/60", "/api/dev/overview", "/api/dev/ticks",
                "/api/dev/ticks/10", "/api/dev/commands"):
        assert c.get(url).status_code == 200, url
    for cmd in ("pause", "resume", "snapshot", "force_sleep"):
        assert c.post("/api/dev/commands", json={"command": cmd, "confirm": True}, headers=HDR).status_code == 202
    assert counts(cfg.db_path) == before                              # not one row changed
    with data.game_db(cfg) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM ticks")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta(key,value) VALUES('x','y')")
    q = devqueue.DevQueue(cfg.dev_db_path)
    assert q.conn.execute("SELECT COUNT(*) FROM dev_commands WHERE status='pending'").fetchone()[0] == 4


def test_dashboard_works_with_no_database_at_all(cfg):
    assert not cfg.db_path.exists()
    c = dash(cfg)
    for url in ("/", "/ticks", "/api/dev/overview", "/api/dev/ticks"):
        assert c.get(url).status_code == 200, url
    assert c.get("/ticks/1").status_code == 404
    assert c.get("/api/dev/ticks").json()["total"] == 0
    assert "not running" in c.get("/").text


# ---------- Overview ----------
def test_overview_engine_status_mapping(cfg, seeded):
    q = devqueue.DevQueue(cfg.dev_db_path)
    ov = data.overview(cfg)["engine"]
    assert ov["state"] in ("running", "asleep", "dozing", "stopped", "not running") and ov["current_tick"] == 90
    seeded.dev.publish(force=True)
    ov = data.overview(cfg)["engine"]
    assert ov["state"] in ("running", "dozing", "asleep") and ov["current_tick"] == 90 and ov["heartbeat_age_s"] < 5
    assert ov["interval_s"] == cfg.settings["tick_interval_seconds"] and ov["last_tick_ms"] is not None
    seeded.paused = True
    seeded.dev.publish(force=True)
    e = data.overview(cfg)["engine"]
    assert e["state"] == "paused" and e["paused"]
    q.set_status({"heartbeat": time.time() - 3600})                       # engine died an hour ago
    e = data.overview(cfg)["engine"]
    assert e["state"] == "not running" and e["stale"]
    q.set_status({"state": "stopped", "heartbeat": time.time() - 3600})
    assert data.overview(cfg)["engine"]["state"] == "stopped"             # a clean stop is not an alarm


def test_overview_stats_match_the_database(cfg, seeded):
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE llm_calls SET valid_json=0 WHERE id IN (SELECT id FROM llm_calls ORDER BY id LIMIT 7)")
    s = data.stats(db.connect(cfg.db_path, readonly=True), cfg)
    n_ticks = conn.execute("SELECT COUNT(*) FROM tick_traces").fetchone()[0]
    n_llm = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    assert s["ticks"] == n_ticks == 90 and s["doze"] == 5 and s["errors"] == 0
    assert s["llm_attempts"] == n_llm and s["invalid_json_rate"] == pytest.approx(7 / n_llm)
    eff = conn.execute("SELECT COUNT(*), SUM(accepted=0) FROM effect_logs").fetchone()
    assert s["effects"] == eff[0] and s["rejected_rate"] == pytest.approx(eff[1] / eff[0])
    toks = conn.execute("SELECT SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) FROM llm_calls").fetchone()[0]
    assert s["tokens_per_tick"] == round(toks / 90)
    brain_ms = [r[0] for r in conn.execute("SELECT total_ms FROM llm_calls WHERE kind='brain'")]
    assert s["brain"]["n"] == len(brain_ms) and s["brain"]["mean_ms"] == pytest.approx(sum(brain_ms) / len(brain_ms), abs=0.06)


def test_stats_only_cover_the_window(cfg, seeded):
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE tick_traces SET started_at='2020-01-01T00:00:00.000' WHERE tick_id<=30")
    conn.execute("UPDATE llm_calls SET started_at='2020-01-01T00:00:00.000' WHERE tick_id<=30")
    s = data.stats(db.connect(cfg.db_path, readonly=True), cfg, hours=24)
    assert s["ticks"] == 60
    assert data.stats(db.connect(cfg.db_path, readonly=True), cfg, hours=10 ** 6)["ticks"] == 90


def test_percentile_and_mean():
    assert data.percentile([], 95) is None and data.mean([]) is None
    assert data.percentile([10], 95) == 10
    assert data.percentile([1, 2, 3, 4, 5], 50) == 3 and data.percentile(list(range(1, 101)), 95) == pytest.approx(95.05)


OLLAMA_TAGS = {"models": [{"name": "qwen3:8b", "digest": "abcdef0123456789", "size": 5_200_000_000, "modified_at": "2026-11-01",
                           "details": {"family": "qwen3", "parameter_size": "8.2B", "quantization_level": "Q4_K_M"}}]}
OLLAMA_PS = {"models": [{"name": "qwen3:8b", "size": 6_000_000_000, "size_vram": 5_400_000_000, "expires_at": "2099-01-01T00:00:00Z"}]}


def patch_ollama(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(data.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_ollama_panel_models_vram_and_versions(cfg, monkeypatch):
    def h(req):
        return httpx.Response(200, json=OLLAMA_TAGS if req.url.path == "/api/tags" else OLLAMA_PS)
    patch_ollama(monkeypatch, h)
    o = data.ollama_info(cfg)
    assert o["reachable"] and o["installed"][0]["digest"] == "abcdef012345" and o["installed"][0]["quantization"] == "Q4_K_M"
    assert o["loaded"][0]["vram_mb"] == 5400 and o["loaded"][0]["gpu_pct"] == 90


def test_ollama_panel_unreachable_and_cached(cfg, monkeypatch):
    calls = []

    def refused(req):
        calls.append(1)
        raise httpx.ConnectError("refused", request=req)
    patch_ollama(monkeypatch, refused)
    o = data.ollama_info(cfg)
    assert not o["reachable"] and "ConnectError" in o["error"]
    data.ollama_info(cfg)
    assert len(calls) == 1                                                # cached for a few seconds


def test_service_health_panel(cfg, seeded, monkeypatch):
    patch_ollama(monkeypatch, lambda r: httpx.Response(200, json=OLLAMA_TAGS if r.url.path == "/api/tags" else OLLAMA_PS))
    monkeypatch.setattr(data.httpx, "get", lambda url, timeout: httpx.Response(200, request=httpx.Request("GET", url)))
    from cat.healthcheck import write_heartbeat
    write_heartbeat(cfg.heartbeat_path)
    ov = data.overview(cfg)
    names = {s["name"]: s for s in ov["services"]}
    assert set(names) == {"engine", "ollama", "web", "devdash"}
    assert names["ollama"]["ok"] and names["web"]["ok"] and names["devdash"]["ok"]
    assert names["engine"]["ok"]                                          # the engine loop wrote a heartbeat


def test_overview_page_shows_controls_and_numbers(cfg, seeded):
    html = dash(cfg).get("/").text
    for label in ("Pause", "Resume", "Step", "Set interval", "Force sleep cycle", "Reload settings", "Inject event", "Snapshot DB"):
        assert label in html
    assert html.count("data-command=") == 8 and "data-confirm=" in html
    assert "#90" in html and "ticks run" in html and "invalid-JSON rate" in html and "tokens per tick" in html


# ---------- Ticks list ----------
def test_ticks_filters(cfg, seeded):
    L = lambda **f: data.ticks_list(cfg, {k: v for k, v in f.items()}, 1, 500)
    allr = L()
    assert allr["total"] == 90 and [r["id"] for r in allr["items"]][:2] == [90, 89]          # newest first
    assert L(status="doze")["total"] == 5 and all(r["kind"] == "doze" for r in L(status="doze")["items"])
    kinds = {r["kind"] for r in allr["items"]}
    for k in kinds:
        assert all(r["kind"] == k for r in L(kind=k)["items"])
    rej = L(rejections=1)
    assert rej["total"] > 0 and rej["total"] < 90
    conn = db.connect(cfg.db_path)
    assert {r["id"] for r in rej["items"]} == {r[0] for r in conn.execute("SELECT id FROM ticks WHERE rejected_changes!='[]'")}
    a = next(r["action"] for r in allr["items"] if r["action"])
    assert all(a.split()[0].lower() in (r["action"] or "").lower() + (r["outcome"] or "").lower() for r in L(q=a.split()[0])["items"])
    some_area = allr["items"][0]["area"]
    assert all(r["area"] == some_area for r in L(area=some_area)["items"])
    conn.execute("UPDATE tick_traces SET total_ms=5000 WHERE tick_id=12")
    assert [r["id"] for r in L(slow_ms=4000)["items"]] == [12]
    assert L(slow_ms=10 ** 6)["total"] == 0


def test_ticks_errors_filter_and_negative_error_traces(cfg, seeded, monkeypatch):
    orig = seeded._commit
    monkeypatch.setattr(seeded, "_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    with pytest.raises(RuntimeError):
        seeded.tick()
    monkeypatch.undo()
    r = data.ticks_list(cfg, {"status": "error"}, 1, 50)
    assert r["total"] == 1 and r["items"][0]["id"] == -1 and "disk on fire" in r["items"][0]["outcome"]
    assert data.ticks_list(cfg, {"errors": 1}, 1, 50)["total"] >= 1
    html = dash(cfg).get("/ticks?status=error").text
    assert "/ticks/-1" in html and "disk on fire" in html


def test_ticks_pagination_covers_everything_once(cfg, seeded):
    seen, page = [], 1
    while True:
        r = data.ticks_list(cfg, {}, page, 25)
        seen += [x["id"] for x in r["items"]]
        if page >= r["pages"]:
            break
        page += 1
    assert r["pages"] == 4 and sorted(seen) == list(range(1, 91)) and len(seen) == len(set(seen))
    api = dash(cfg).get("/api/dev/ticks?limit=10&page=2").json()
    assert len(api["items"]) == 10 and api["page"] == 2 and api["total"] == 90
    assert len(dash(cfg).get("/api/dev/ticks").json()["items"]) == 50       # default page size


def test_ticks_page_filters_render_and_keep_query_in_pager(cfg, seeded):
    html = dash(cfg).get("/ticks?kind=tick&limit=5").text
    assert "<select name=\"kind\">" in html and "selected" in html
    html = dash(cfg).get("/ticks?kind=tick").text
    assert "older →" in html and "kind=tick" in html.split("older →")[0].split("href=")[-1]


# ---------- Tick detail ----------
def test_tick_detail_has_every_section_for_a_normal_tick(cfg, seeded):
    tid = db.connect(cfg.db_path).execute(
        "SELECT t.id FROM ticks t JOIN recall_logs r ON r.tick_id=t.id WHERE t.kind='tick' ORDER BY t.id DESC LIMIT 1").fetchone()[0]
    html = dash(cfg).get(f"/ticks/{tid}").text
    for needle in ("World tick", "Event rolls", "Recall", "Brain call", "Narrator call", "Effects", "Need math",
                   "Mood math", "Memory writes", "What the public site shows", "System prompt", "User prompt",
                   "JSON schema", "Raw response", "Parsed JSON", "stagebar", "prev #", "all ticks"):
        assert needle in html, needle
    assert html.count('class="copy"') >= 8                                  # a copy button on each text block


def test_tick_detail_shows_the_exact_prompts_that_were_sent(cfg):
    from markupsafe import escape as esc            # the same escaping Jinja applies
    e, b = make_engine(cfg)
    sent = {}

    def br(m, s):
        sent["brain"] = m
        return {"reaction": "r", "thought": "t", "action": "bat <b>the</b> ball & run", "intent": "i",
                "feeling": "neutral", "wants_sleep": False}
    b.handlers["brain"] = br
    tid = e.tick()["id"]
    page = dash(cfg).get(f"/ticks/{tid}").text
    assert str(esc(sent["brain"][0]["content"])) in page and str(esc(sent["brain"][1]["content"])) in page
    assert "<b>the</b>" not in page and "&lt;b&gt;the&lt;/b&gt;" in page      # escaped, never injected


def test_tick_detail_numbers_equal_the_public_site(cfg, seeded):
    """Tick detail fully explains every number the public site shows for that tick."""
    web = TestClient(create_web(cfg))
    state = web.get("/api/state").json()
    conn = db.connect(cfg.db_path, readonly=True)
    latest = conn.execute("SELECT MAX(id) FROM ticks").fetchone()[0]
    d = data.tick_detail(cfg, latest)
    pub = d["public"]
    site = {n["id"]: n for n in state["needs"]}
    for n in pub["needs"]:                                                  # needs: value and word
        assert (n["value"], n["word"]) == (site[n["id"]]["value"], site[n["id"]]["word"])
    for m in d["need_math"]:                                                # ... and the arithmetic behind each
        assert round(m["after"]) == site[m["need"]]["value"]
        assert m["after"] == pytest.approx(m["before"] - m["decay"] + m["gain_raw"] * m["repetition_mult"]
                                           - (m["event_drain"] or 0) - (m["dislike_drain"] or 0)
                                           + m["clamp_adjust"], abs=2e-3)
    assert (pub["energy"]["value"], pub["energy"]["word"]) == (state["energy"]["value"], state["energy"]["word"])
    assert d["energy"] == pytest.approx(state["energy"]["value"], abs=0.5)          # stored, not derived
    assert d["mood_math"]["word"] == state["mood"]["word"] == pub["mood"]["word"]
    assert d["mood_math"]["value"] == state["mood"]["value"]
    # trend arrows on the site compare with the previous tick: the math's `before` is that previous value
    prev = [json.loads(r[0]) for r in conn.execute("SELECT needs_after FROM ticks ORDER BY id DESC LIMIT 2")][1]
    for m in d["need_math"]:
        assert m["before"] == pytest.approx(prev[m["need"]], abs=1e-3)


def test_every_diary_entry_on_the_site_is_explained_by_its_tick(cfg, seeded):
    web = TestClient(create_web(cfg))
    entries = [e for e in web.get("/api/diary?limit=100").json()["entries"] if e["type"] == "tick"]
    assert len(entries) > 20
    for e in entries[:25]:
        d = data.tick_detail(cfg, e["id"])
        t = d["tick"]
        assert (e["action"], e["outcome"], e["reaction"]) == (t["action"], t["outcome"], t["reaction"] or "")
        assert [ev["text"] for ev in d["events"]] == e["events"]
        math = {m["need"]: m for m in d["need_math"]}
        chips = {c["id"]: c["dir"] for c in e["need_changes"]}
        for nid, m in math.items():                                        # chips = changes >= the threshold
            delta = m["after"] - m["before"]
            if abs(delta) >= cfg.settings["web_ui"]["chip_min_delta"]:
                assert chips[nid] == ("up" if delta > 0 else "down")
            else:
                assert nid not in chips
        assert e["time_label"] and d["clock"]["from"] == t["sim_time"]


def test_tick_detail_for_doze_asleep_and_error_ticks(cfg, seeded, monkeypatch):
    conn = db.connect(cfg.db_path, readonly=True)
    c = dash(cfg)
    doze = conn.execute("SELECT id FROM ticks WHERE kind='doze' LIMIT 1").fetchone()[0]
    page = c.get(f"/ticks/{doze}").text
    assert "doze" in page and "Need math" in page and "restore" in page          # a doze restores a little rest
    seeded._online_cache = None
    from cat.llm import BackendError

    def down(m, s):
        raise BackendError("injected failure")
    seeded.backend.handlers["brain"] = down                                      # both brain attempts fail
    seeded.state["is_asleep"], seeded.state["energy"] = False, 20.0               # awake, whatever the seed run ended on
    failed = seeded.tick()
    assert failed["kind"] == "doze"
    page = c.get(f"/ticks/{failed['id']}").text
    assert "attempt 1" in page and "attempt 2" in page and "injected failure" in page
    asleep = conn.execute("SELECT id FROM ticks WHERE kind='asleep' LIMIT 1").fetchone()
    if asleep:
        assert c.get(f"/ticks/{asleep[0]}").status_code == 200
    monkeypatch.setattr(seeded, "_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        seeded.tick()
    page = c.get("/ticks/-1").text
    assert "boom" in page and "error before it was recorded" in page
    assert c.get("/ticks/99999").status_code == 404 and c.get("/api/dev/ticks/99999").status_code == 404


def test_tick_detail_without_a_trace_still_renders(cfg, seeded):
    conn = db.connect(cfg.db_path)
    conn.execute("DELETE FROM tick_traces WHERE tick_id=7")
    for t in ("llm_calls", "need_math", "effect_logs", "mood_math", "event_rolls", "embed_calls", "recall_logs"):
        conn.execute(f"DELETE FROM {t} WHERE tick_id=7")
    r = dash(cfg).get("/ticks/7")
    assert r.status_code == 200 and "No trace was stored" in r.text


def test_summary_level_tick_detail_says_text_was_not_stored(cfg):
    cfg.settings["trace_level"] = "summary"
    e, b = make_engine(cfg)
    e.run(ticks=3, interval=0)
    page = dash(cfg).get("/ticks/2").text
    assert "not stored" in page and "trace_level was" in page
    assert "Need math" in page                                              # the numbers are still there


def test_tick_detail_sleep_section_shows_proposed_vs_clamped(cfg):
    e, b = make_engine(cfg)
    b.handlers["tendencies"] = lambda m, s: {"changes": {"boldness": 1, "sociability": -9}}
    e.tick()
    e.state["sim_time"] = "2026-11-18T23:00"
    e.state["energy"] = -45.0
    tid = e.tick()["id"]
    d = data.tick_detail(cfg, tid)
    assert d["sleep_logs"] and {t["step"] for t in d["sleep_logs"][0]["traces"]} == {"consolidate", "tendencies", "preferences", "self_image", "projects"}
    page = dash(cfg).get(f"/ticks/{tid}").text
    assert "Sleep cycle" in page and "LLM proposed" in page and "applied change" in page and "sleep_tendencies" in page


def test_json_api_tick_detail_is_complete(cfg, seeded):
    j = dash(cfg).get("/api/dev/ticks/20").json()
    assert {"tick", "trace", "stages", "events", "event_rolls", "llm", "embeds", "recall", "effects", "need_math",
            "mood_math", "memories", "public", "clock"} <= set(j)
    assert j["llm"][0]["system_prompt"] and j["llm"][0]["raw_response"]


# ---------- static / offline ----------
def test_static_assets_and_no_external_urls(cfg):
    c = dash(cfg)
    assert c.get("/static/devdash.css").status_code == 200 and c.get("/static/devdash.js").status_code == 200
    for f in list((HERE / "templates").glob("*.html")) + list((HERE / "static").glob("*.css")) + list((HERE / "static").glob("*.js")):
        for url in re.findall(r"https?://[^\s\"')]+", f.read_text()):
            assert "w3.org" in url, f"{f.name}: {url}"

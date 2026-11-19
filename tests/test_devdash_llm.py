"""Part B4: LLM calls page, charts data, prompt playground and model compare."""
import json
import random
import shutil
import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cat import db
from cat.devdash import data, playground
from cat.devdash.app import create_app
from cat.engine import Engine
from cat.llm import BackendError, MockBackend

HDR = {"X-Requested-With": "devdash"}
REAL_CLIENT = httpx.Client            # the real class, captured before any test patches it
STATIC = Path(__file__).resolve().parent.parent / "cat" / "devdash" / "static"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)
    data._probe_cache.clear()
    assert not playground.LOCK.locked()


def make_engine(cfg, seed=3):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    return Engine(cfg, b, rng=random.Random(seed)), b


@pytest.fixture
def eng(cfg):
    e, b = make_engine(cfg)
    e.run(ticks=40, interval=0)
    return e


def client(cfg):
    return TestClient(create_app(cfg))


def ollama_handler(seen=None, content='{"action": "nap"}', fail=None):
    def h(req):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "llama3.1:8b"}, {"name": "gemma3:4b"}]})
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        body = json.loads(req.content)
        if seen is not None:
            seen.append(body)
        if fail:
            return fail(req, body)
        return httpx.Response(200, json={"message": {"content": content}, "prompt_eval_count": 100, "eval_count": 20,
                                         "load_duration": 3_000_000, "prompt_eval_duration": 40_000_000,
                                         "eval_duration": 80_000_000, "total_duration": 125_000_000})
    return h


def patch_ollama(monkeypatch, handler):
    monkeypatch.setattr(httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def counts(path):
    conn = sqlite3.connect(path)
    out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for (t,) in
           conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    out["state"] = conn.execute("SELECT * FROM state").fetchone()
    out["objects"] = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
    conn.close()
    return out


# ---------- the list ----------
def test_llm_list_filters_and_statuses(cfg):
    e, b = make_engine(cfg)
    outs = iter([{"reaction": "no action"}, None])           # tick 1: brain unusable once, then fine

    def flaky(m, s):
        o = next(outs, None)
        return o or {"reaction": "", "thought": "", "action": "sit", "intent": "", "feeling": "neutral", "wants_sleep": False}
    b.handlers["brain"] = flaky
    e.run(ticks=6, interval=0)
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE llm_calls SET total_ms=9000 WHERE id=3")
    L = lambda **f: data.llm_list(cfg, f, 1, 500)
    allc = L()
    assert allc["total"] == conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    assert set(allc["kinds"]) >= {"brain", "narrator"} and allc["models"] == ["qwen3:8b"]
    assert [c["id"] for c in allc["items"]] == sorted((c["id"] for c in allc["items"]), reverse=True)
    bad = L(valid="invalid")["items"]
    assert [(c["kind"], c["attempt"], c["status"]) for c in bad] == [("brain", 1, "error")]
    assert "no action" in bad[0]["error"]
    assert all(c["status"] == "ok" for c in L(valid="valid")["items"])
    assert L(errors=1)["total"] == 1
    assert all(c["kind"] == "narrator" for c in L(kind="narrator")["items"]) and L(kind="nope")["total"] == 0
    assert L(model="qwen3:8b")["total"] == allc["total"] and L(model="other")["total"] == 0
    assert [c["id"] for c in L(slow_ms=8000)["items"]] == [3]
    assert all(c["tick_id"] == 2 for c in L(tick_id=2)["items"]) and L(tick_id=2)["total"] >= 2


def test_llm_list_pagination_and_page_html(cfg, eng):
    seen, page = [], 1
    while True:
        r = data.llm_list(cfg, {}, page, 30)
        seen += [c["id"] for c in r["items"]]
        if page >= r["pages"]:
            break
        page += 1
    assert len(seen) == len(set(seen)) == r["total"]
    c = client(cfg)
    html = c.get("/llm?kind=brain").text
    for needle in ("LLM calls", "Latency, p50 / p95", "Tokens per call", "Invalid-JSON", "uPlot.iife.min.js", "charts.js", "name=\"kind\""):
        assert needle in html
    assert len(c.get("/api/dev/llm?limit=10").json()["items"]) == 10


def test_call_detail_shows_exact_text_attempts_and_playground_link(cfg):
    e, b = make_engine(cfg)
    b.fail_next = 1                                           # brain attempt 1 fails, attempt 2 works
    e.tick()
    c = client(cfg)
    first = data.llm_list(cfg, {"kind": "brain"}, 1, 10)["items"][-1]
    html = c.get(f"/llm/{first['id']}").text
    assert "attempt 1" in html and "injected failure" in html and f"/playground?call_id={first['id']}" in html
    sib = data.llm_detail(cfg, first["id"])["siblings"]
    assert [s["attempt"] for s in sib] == [1, 2]
    assert f"/llm/{sib[1]['id']}" in html                     # link to the other attempt
    assert c.get("/llm/99999").status_code == 404 and c.get("/api/dev/llm/99999").status_code == 404
    api = c.get(f"/api/dev/llm/{first['id']}").json()
    assert api["system_prompt"] and api["user_prompt"] and api["schema_obj"]["type"] == "object"


def test_summary_trace_level_disables_the_playground_for_that_call(cfg):
    cfg.settings["trace_level"] = "summary"
    e, b = make_engine(cfg)
    e.tick()
    cid = data.llm_list(cfg, {}, 1, 5)["items"][0]["id"]
    html = client(cfg).get(f"/llm/{cid}").text
    assert "no playground" in html and "/playground?call_id" not in html and "not stored" in html


# ---------- chart data ----------
def seed_calls(cfg, rows):
    """rows: (tick_id, kind, model, total_ms, tin, tout, valid, error, started_at)"""
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    for r in rows:
        conn.execute("INSERT INTO llm_calls(tick_id,kind,model,total_ms,prompt_tokens,completion_tokens,valid_json,error,"
                     "attempt,started_at) VALUES(?,?,?,?,?,?,?,?,1,?)", r)


def test_series_buckets_percentiles_tokens_and_invalid_rate(cfg):
    rows = []
    for t in range(1, 41):                                    # 40 ticks, 1 brain call each
        rows.append((t, "brain", "m", 100.0 + t, 1000 + t, 50, 1, None, f"2026-11-18T10:{t:02d}:00.000"))
    rows[3] = (4, "brain", "m", 5000.0, 1004, 50, 0, "bad json", "2026-11-18T10:04:00.000")
    rows.append((5, "narrator", "m2", 7.0, 10, 5, 1, None, "2026-11-18T10:05:30.000"))
    seed_calls(cfg, rows)
    s = data.llm_series(cfg, kind="brain", x="tick", buckets=4)
    assert s["axis"] == "tick" and len(s["x"]) == 4 and sum(s["n"]) == 40
    assert s["x"][0] == 1.0 and s["x"][1] == pytest.approx(1 + 39 / 4)
    assert s["p95"][0] > s["p50"][0] and s["p95"][0] > 1000                       # the 5000 ms outlier shows in p95
    assert s["invalid"][0] > 0 and s["invalid"][1:] == [0.0, 0.0, 0.0]
    assert s["tokens_out"] == [50.0] * 4
    assert data.llm_series(cfg, kind="narrator", x="tick")["n"] == [1]
    assert data.llm_series(cfg, model="m2")["n"] == [1] and data.llm_series(cfg, kind="x")["x"] == []
    t = data.llm_series(cfg, kind="brain", x="time", buckets=5)
    assert t["axis"] == "time" and t["x"][0] > 1.7e9 and len(t["x"]) <= 6          # unix seconds
    assert data.llm_series(cfg, kind="brain", buckets=1000)["n"][0] >= 1


def test_series_empty_and_api(cfg):
    c = client(cfg)
    assert c.get("/api/dev/llm/series").json()["x"] == []                        # no DB at all
    assert c.get("/api/dev/llm/series?x=bogus").status_code == 422
    seed_calls(cfg, [(1, "brain", "m", 10.0, 1, 1, 1, None, "2026-11-18T10:00:00.000")])
    j = c.get("/api/dev/llm/series?x=tick").json()
    assert j["x"] == [1.0] and j["n"] == [1]


# ---------- playground: validation ----------
@pytest.mark.parametrize("body,msg", [
    ("nope", "JSON object"), ({}, "at least one model"), ({"models": []}, "at least one model"),
    ({"models": ["a", "b", "c", "d"], "user_prompt": "x"}, "at most 3"), ({"models": ["a"], "user_prompt": "  "}, "empty"),
    ({"models": ["a"], "user_prompt": "x", "schema": "{not json"}, "not valid JSON"),
    ({"models": ["a"], "user_prompt": "x", "schema": [1]}, "must be an object"),
    ({"models": ["a"], "user_prompt": "x", "options": {"temperature": 3}}, "temperature"),
    ({"models": ["a"], "user_prompt": "x", "options": {"temperature": True}}, "temperature"),
    ({"models": ["a"], "user_prompt": "x", "options": {"num_ctx": 10}}, "num_ctx"),
    ({"models": ["a"], "user_prompt": "x", "think": "yes"}, "think"),
    ({"models": ["a"], "user_prompt": "x" * 300_000}, "too large"),
    ({"models": ["a" * 200], "user_prompt": "x"}, "too long"),
])
def test_validate_rejects_bad_requests(body, msg):
    with pytest.raises(playground.PlaygroundError, match=msg):
        playground.validate(body)


def test_validate_cleans_a_good_request():
    r = playground.validate({"models": [" a ", "a", "b"], "system_prompt": "s", "user_prompt": "u", "schema": '{"type":"object"}',
                             "options": {"temperature": 0.2, "num_ctx": 2048}, "think": False, "source_call_id": 7})
    assert r["models"] == ["a", "b"] and r["schema"] == {"type": "object"} and r["options"] == {"temperature": 0.2, "num_ctx": 2048}
    assert r["think"] is False and r["source_call_id"] == 7
    assert playground.validate({"models": ["a"], "user_prompt": "u", "schema": ""})["schema"] is None


# ---------- playground: the Ollama call ----------
def test_call_ollama_sends_exactly_what_it_is_given(cfg, monkeypatch):
    seen = []
    patch_ollama(monkeypatch, ollama_handler(seen))
    r = playground.call_ollama(cfg, "qwen3:8b", "SYS", "USER", {"type": "object"}, {"temperature": 0.1, "num_ctx": 2048}, False)
    body = seen[0]
    assert body == {"model": "qwen3:8b", "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}],
                    "stream": False, "format": {"type": "object"}, "options": {"temperature": 0.1, "num_ctx": 2048}, "think": False}
    assert r["parsed"] == {"action": "nap"} and r["valid_json"] == 1 and r["error"] is None
    assert (r["prompt_tokens"], r["completion_tokens"], r["load_ms"], r["prompt_eval_ms"], r["eval_ms"], r["total_ms"]) == (100, 20, 3, 40, 80, 125)


def test_call_ollama_omits_empty_system_schema_options(cfg, monkeypatch):
    seen = []
    patch_ollama(monkeypatch, ollama_handler(seen, content="just some text"))
    r = playground.call_ollama(cfg, "m", "  ", "hello", None, {}, None)
    assert seen[0] == {"model": "m", "messages": [{"role": "user", "content": "hello"}], "stream": False}
    assert r["raw_response"] == "just some text" and r["parsed"] is None and r["valid_json"] == 0 and r["error"] is None


@pytest.mark.parametrize("kind", ["timeout", "http", "connect", "badjson"])
def test_call_ollama_failures_are_results_not_crashes(cfg, monkeypatch, kind):
    def fail(req, body):
        if kind == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        if kind == "http":
            return httpx.Response(404, text="model 'x' not found")
        if kind == "connect":
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json={"message": {"content": "not { json"}})
    patch_ollama(monkeypatch, ollama_handler(fail=fail))
    r = playground.call_ollama(cfg, "x", "", "u", {"type": "object"}, {}, None)
    assert r["error"] and r["total_ms"] is not None
    if kind == "badjson":
        assert r["raw_response"] == "not { json" and r["valid_json"] == 0 and "invalid JSON" in r["error"]
    if kind == "http":
        assert "404" in r["error"] and "not found" in r["error"]


# ---------- playground: runs are saved and NEVER applied to the world ----------
def test_run_saves_results_and_never_touches_the_game_db(cfg, eng, monkeypatch):
    seen = []
    patch_ollama(monkeypatch, ollama_handler(seen))
    before = counts(cfg.db_path)
    req = playground.validate({"models": ["qwen3:8b", "llama3.1:8b"], "system_prompt": "S", "user_prompt": "U", "schema": {"type": "object"},
                               "options": {"temperature": 0.3}, "source_call_id": 5, "label": "try"})
    out = playground.run(cfg, req)
    assert [r["model"] for r in out["runs"]] == ["qwen3:8b", "llama3.1:8b"] and [b["model"] for b in seen] == ["qwen3:8b", "llama3.1:8b"]
    assert counts(cfg.db_path) == before                                      # not one game row, object or state value changed
    saved = playground.group_runs(cfg, out["group_id"])
    assert len(saved) == 2 and {r["group_id"] for r in saved} == {out["group_id"]}
    r = saved[0]
    assert (r["system_prompt"], r["user_prompt"], r["source_call_id"], r["label"]) == ("S", "U", 5, "try")
    assert r["schema_obj"] == {"type": "object"} and r["options"] == {"temperature": 0.3} and r["status"] == "ok" and r["parsed_obj"] == {"action": "nap"}
    assert playground.get_run(cfg, r["id"])["raw_response"] == '{"action": "nap"}' and playground.get_run(cfg, 9999) is None
    assert [x["id"] for x in playground.list_runs(cfg, 10)] == [saved[1]["id"], saved[0]["id"]]       # newest first


def test_a_failing_model_does_not_stop_the_others(cfg, monkeypatch):
    def fail(req, body):
        if body["model"] == "bad":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"message": {"content": "{}"}})
    patch_ollama(monkeypatch, ollama_handler(fail=fail))
    out = playground.run(cfg, playground.validate({"models": ["bad", "good"], "user_prompt": "u"}))
    assert [r["status"] for r in out["runs"]] == ["error", "ok"] and "500" in out["runs"][0]["error"]


def test_diff_keys():
    assert playground.diff_keys({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == ["b", "c"]
    assert playground.diff_keys({"a": 1}, {"a": 1}) == [] and playground.diff_keys("x", "y") == ["(whole response)"]


# ---------- the API and pages ----------
def test_run_endpoint_rules(cfg, eng, monkeypatch):
    patch_ollama(monkeypatch, ollama_handler())
    c = client(cfg)
    ok = {"models": ["qwen3:8b"], "user_prompt": "hello", "system_prompt": "hi"}
    assert c.post("/api/dev/playground/run", json=ok).status_code == 403                    # no CSRF header
    assert c.post("/api/dev/playground/run", json={**ok, "models": []}, headers=HDR).status_code == 422
    r = c.post("/api/dev/playground/run", json=ok, headers=HDR)
    assert r.status_code == 200 and r.json()["runs"][0]["status"] == "ok" and r.json()["group_id"]
    assert playground.LOCK.acquire(blocking=False)                                          # someone else is running
    try:
        assert c.post("/api/dev/playground/run", json=ok, headers=HDR).status_code == 409
    finally:
        playground.LOCK.release()
    assert c.get("/api/dev/playground/runs").json()["items"][0]["model"] == "qwen3:8b"
    rid = c.get("/api/dev/playground/runs").json()["items"][0]["id"]
    assert c.get(f"/api/dev/playground/runs/{rid}").json()["user_prompt"] == "hello"
    assert c.get("/api/dev/playground/runs/9999").status_code == 404


def test_replay_a_stored_brain_call_exactly_and_compare_three_models(cfg, eng, monkeypatch):
    """The definition of done: recover the exact prompt + raw response of a brain call and replay it."""
    patch_ollama(monkeypatch, ollama_handler())
    conn = db.connect(cfg.db_path, readonly=True)
    call = dict(conn.execute("SELECT * FROM llm_calls WHERE kind='brain' ORDER BY id DESC LIMIT 1").fetchone())
    c = client(cfg)
    page = c.get(f"/playground?call_id={call['id']}").text
    from markupsafe import escape
    assert str(escape(call["system_prompt"])) in page and str(escape(call["user_prompt"])) in page
    assert str(escape(call["raw_response"])) in page                                       # the original answer, side by side
    assert "qwen3:8b" in page and "llama3.1:8b" in page                                    # model dropdown from /api/tags
    seen = []
    patch_ollama(monkeypatch, ollama_handler(seen, content=call["raw_response"]))
    body = {"models": ["qwen3:8b", "llama3.1:8b", "gemma3:4b"], "system_prompt": call["system_prompt"],
            "user_prompt": call["user_prompt"], "schema": call["schema"], "source_call_id": call["id"],
            "options": {"temperature": json.loads(call["options"])["temperature"] if "temperature" in json.loads(call["options"]) else 0.9}}
    before = counts(cfg.db_path)
    j = c.post("/api/dev/playground/run", json=body, headers=HDR).json()
    assert len(j["runs"]) == 3
    for sent in seen:                                                                       # byte-for-byte what was stored
        assert sent["messages"][0]["content"] == call["system_prompt"] and sent["messages"][1]["content"] == call["user_prompt"]
        assert sent["format"] == json.loads(call["schema"])
    assert counts(cfg.db_path) == before
    g = c.get(f"/playground/groups/{j['group_id']}").text
    assert g.count('class="rcard') == 4 and "Original" in g and "same JSON as the original" in g
    assert "llama3.1:8b" in g and "gemma3:4b" in g


def test_group_page_marks_differences(cfg, eng, monkeypatch):
    call = dict(db.connect(cfg.db_path, readonly=True).execute("SELECT * FROM llm_calls WHERE kind='brain' LIMIT 1").fetchone())
    patch_ollama(monkeypatch, ollama_handler(content='{"action": "something else entirely"}'))
    j = client(cfg).post("/api/dev/playground/run", headers=HDR, json={"models": ["m"], "user_prompt": call["user_prompt"],
                         "schema": call["schema"], "source_call_id": call["id"]}).json()
    assert "differs from the original in: <b>action" in client(cfg).get(f"/playground/groups/{j['group_id']}").text.replace("\n", "")


def test_playground_pages_render(cfg, eng):
    c = client(cfg)
    assert c.get("/playground").status_code == 200 and c.get("/playground/runs").status_code == 200
    assert c.get("/playground?call_id=999999").status_code == 404 and c.get("/playground/groups/nope").status_code == 404
    assert "Ollama is unreachable" in c.get("/playground").text                             # no Ollama in tests: still usable
    assert "Nothing here is ever applied to the world" in c.get("/playground").text


def test_models_endpoint(cfg, monkeypatch):
    patch_ollama(monkeypatch, ollama_handler())
    j = client(cfg).get("/api/dev/playground/models").json()
    assert j["reachable"] and j["models"] == ["qwen3:8b", "llama3.1:8b", "gemma3:4b"]


# ---------- static assets ----------
def test_vendored_chart_library_and_scripts_are_served_locally(cfg):
    c = client(cfg)
    for p in ("vendor/uPlot.iife.min.js", "vendor/uPlot.min.css", "vendor/uPlot.LICENSE", "charts.js", "playground.js"):
        assert c.get(f"/static/{p}").status_code == 200, p
    assert "MIT" in (STATIC / "vendor" / "uPlot.LICENSE").read_text()


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("script", ["devdash.js", "charts.js", "playground.js"])
def test_scripts_are_syntactically_valid(script):
    r = subprocess.run(["node", "--check", str(STATIC / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

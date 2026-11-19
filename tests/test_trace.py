"""Part B1: every tick is fully traceable, and tracing never breaks the creature."""
import json
import logging
import random
from datetime import datetime, timedelta

import httpx
import pytest

from cat import brain, character, db, logs, narrator, trace
from cat.engine import Engine
from cat.llm import BackendError, MockBackend, OllamaBackend, call_json, hash_embed
from tests.helpers import pick

T0 = "2026-11-18T08:00"


def make(cfg, seed=1):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    return Engine(cfg, b, rng=random.Random(seed)), b


def rows(e, sql, *a):
    return [dict(r) for r in e.conn.execute(sql, a)]


def scripted(b, label="go to the kitchen", feeling="positive"):
    """Handlers that also capture exactly what the engine sent."""
    seen = {"brain": [], "narrator": []}

    def br(m, s):
        seen["brain"].append((m, s))
        return pick(m, label, feeling=feeling)

    def na(m, s):
        seen["narrator"].append((m, s))
        return {"outcome": "It rains softly."}
    b.handlers["brain"], b.handlers["narrator"] = br, na
    return seen


# ---------- a mock tick writes a COMPLETE trace ----------
def test_mock_tick_writes_complete_trace(cfg):
    e, b = make(cfg)
    seen = scripted(b)
    long_mem = e.memory.prepare("the rain on the window was lovely", 0.8, "2026-11-17T10:00")
    with db.transaction(e.conn):
        e.memory.save(long_mem, "long")
    info = e.tick()
    tid = info["id"]

    tt = rows(e, "SELECT * FROM tick_traces WHERE tick_id=?", tid)[0]
    assert tt["status"] == "ok" and tt["total_ms"] > 0 and tt["finished_at"] >= tt["started_at"]
    stages = json.loads(tt["stage_timings"])
    assert {"world", "recall", "brain", "narrator", "apply", "record"} <= set(stages)
    assert all(v >= 0 for v in stages.values()) and tt["total_ms"] >= max(stages.values())

    llm = rows(e, "SELECT * FROM llm_calls WHERE tick_id=? ORDER BY id", tid)
    assert [c["kind"] for c in llm] == ["brain", "narrator"]
    for c, key in zip(llm, ("brain", "narrator")):
        msgs, schema = seen[key][0]                            # exactly what was sent, not a reconstruction
        assert c["system_prompt"] == msgs[0]["content"] and c["user_prompt"] == msgs[1]["content"]
        assert json.loads(c["schema"]) == schema
        assert json.loads(c["parsed"]) == json.loads(c["raw_response"])
        assert c["valid_json"] == 1 and c["attempt"] == 1 and c["error"] is None
        assert c["model"] == cfg.settings[f"{key}_model"] and c["total_ms"] is not None
        assert c["prompt_tokens"] > 0 and c["completion_tokens"] > 0
    assert "You remember: the rain on the window was lovely" in llm[0]["user_prompt"]

    purposes = [r["purpose"] for r in rows(e, "SELECT purpose FROM embed_calls WHERE tick_id=?", tid)]
    assert "recall_query" in set(purposes) and "repetition_check" not in set(purposes)   # repetition is by move key now
    for r in rows(e, "SELECT * FROM embed_calls WHERE tick_id=?", tid):
        assert r["dims"] == 64 and r["input_text"] and r["model"] == "nomic-embed-text"

    rl = rows(e, "SELECT * FROM recall_logs WHERE tick_id=?", tid)[0]
    cands = json.loads(rl["candidates"])
    assert rl["query_text"] and cands[0]["selected"] and cands[0]["text"].startswith("the rain")
    assert {"memory_id", "recency", "importance", "relevance", "score", "selected"} <= set(cands[0])

    fx = rows(e, "SELECT * FROM effect_logs WHERE tick_id=? ORDER BY id", tid)
    assert [(f["op"], f["accepted"]) for f in fx] == [("move_self", 1)] and fx[0]["reason"] is None

    tick_row = rows(e, "SELECT * FROM ticks WHERE id=?", tid)[0]
    needs_after = json.loads(tick_row["needs_after"])
    nm = rows(e, "SELECT * FROM need_math WHERE tick_id=?", tid)
    assert [r["need"] for r in nm] == list(cfg.character["needs"])
    for r in nm:                                              # the arithmetic reproduces the stored number
        assert r["after"] == pytest.approx(needs_after[r["need"]], abs=1e-3)
        rebuilt = r["before"] - r["decay"] + r["gain_raw"] * r["repetition_mult"] \
            - (r["event_drain"] or 0) - (r["dislike_drain"] or 0) + r["clamp_adjust"]
        assert r["effort_cost"] == 0                          # effort spends energy, not needs
        assert rebuilt == pytest.approx(r["after"], abs=2e-3)
    cur = next(r for r in nm if r["need"] == "curiosity")          # going somewhere new: novelty gains
    assert cur["bucket"] == "+" and cur["gain_raw"] == cfg.settings["gain_values"]["+"]
    prev = rows(e, "SELECT energy_after FROM ticks WHERE id<? ORDER BY id DESC LIMIT 1", tid)
    start = prev[0]["energy_after"] if prev else cfg.settings["energy_start"]
    assert tick_row["energy_after"] == pytest.approx(start - cfg.settings["effort_energy_cost"]["low"])

    mm = rows(e, "SELECT * FROM mood_math WHERE tick_id=?", tid)[0]
    mood_after = json.loads(tick_row["mood_after"])
    assert (mm["word"], mm["value"]) == (mood_after["word"], mood_after["value"])
    inp = json.loads(mm["inputs"])
    assert {"bands", "terms", "event_nudge", "feeling", "unclamped", "thresholds"} <= set(inp)
    assert inp["feeling"] == "positive"

    ev = rows(e, "SELECT * FROM event_rolls WHERE tick_id=?", tid)
    assert len(ev) == len(cfg.world["events"])
    for r in ev:
        assert (r["roll"] is not None) or r["filtered_reason"]

    mem = rows(e, "SELECT * FROM memories WHERE kind='short'")[0]
    assert mem["tick_id"] == tid                              # memory writes link back to the tick


def test_need_and_mood_math_hold_for_every_tick_of_a_long_run(cfg):
    e, b = make(cfg, seed=4)
    e.run(ticks=400, interval=0)
    n = 0
    for t in rows(e, "SELECT id, needs_after, mood_after, kind FROM ticks"):
        nm = rows(e, "SELECT * FROM need_math WHERE tick_id=?", t["id"])
        assert len(nm) == len(cfg.character["needs"]), (t["id"], t["kind"])
        after = json.loads(t["needs_after"])
        for r in nm:
            assert r["after"] == pytest.approx(after[r["need"]], abs=1e-3)
            assert (r["before"] - r["decay"] + r["gain_raw"] * r["repetition_mult"]
                    - (r["event_drain"] or 0) - (r["dislike_drain"] or 0) + r["clamp_adjust"]) == pytest.approx(r["after"], abs=2e-3)
            n += 1
        mm = rows(e, "SELECT word, value FROM mood_math WHERE tick_id=?", t["id"])[0]
        assert mm["word"] == json.loads(t["mood_after"])["word"]
    assert n > 1000
    assert e.conn.execute("SELECT COUNT(*) FROM tick_traces").fetchone()[0] == 400


def test_doze_tick_trace_and_failed_attempts_recorded(cfg):
    e, b = make(cfg)
    b.fail_next = 2                                           # both attempts at the brain call fail
    info = e.tick()
    assert info["kind"] == "doze"
    assert rows(e, "SELECT status FROM tick_traces WHERE tick_id=?", info["id"])[0]["status"] == "doze"
    calls = rows(e, "SELECT * FROM llm_calls WHERE tick_id=? ORDER BY id", info["id"])
    assert [(c["kind"], c["attempt"], c["valid_json"]) for c in calls] == [("brain", 1, 0), ("brain", 2, 0)]
    assert all("injected failure" in c["error"] and c["system_prompt"] for c in calls)
    nm = rows(e, "SELECT * FROM need_math WHERE tick_id=?", info["id"])
    assert [r["need"] for r in nm] == list(cfg.character["needs"]) and all(r["gain_raw"] == 0 for r in nm)
    prev = rows(e, "SELECT energy_after FROM ticks WHERE id<? ORDER BY id DESC LIMIT 1", info["id"])
    start = prev[0]["energy_after"] if prev else cfg.settings["energy_start"]
    assert rows(e, "SELECT energy_after FROM ticks WHERE id=?", info["id"])[0]["energy_after"] == pytest.approx(
        min(50, start + cfg.settings["doze"]["energy_restore"]))


def test_unparseable_reply_then_good_reply_records_both_attempts(cfg):
    e, b = make(cfg)
    outs = iter([{"reaction": "no action key"}, None])

    def flaky(m, s):
        o = next(outs)
        return o if o is not None else pick(m)
    b.handlers["brain"] = flaky
    info = e.tick()
    calls = rows(e, "SELECT attempt, error, valid_json FROM llm_calls WHERE kind='brain' AND tick_id=?", info["id"])
    assert [c["attempt"] for c in calls] == [1, 2]
    assert "not on the menu" in calls[0]["error"] and calls[0]["valid_json"] == 1   # valid JSON, unusable content
    assert calls[1]["error"] is None and info["kind"] == "tick"


def test_exception_tick_is_traced_with_a_negative_id_and_engine_carries_on(cfg, monkeypatch):
    e, b = make(cfg)
    e.tick()
    boom = {"on": True}
    orig = e._commit

    def bad(*a, **k):
        if boom["on"]:
            raise RuntimeError("disk on fire")
        return orig(*a, **k)
    monkeypatch.setattr(e, "_commit", bad)
    with pytest.raises(RuntimeError):
        e.tick()
    tt = rows(e, "SELECT * FROM tick_traces WHERE tick_id<0")[0]
    assert tt["tick_id"] == -1 and tt["status"] == "error" and "disk on fire" in tt["error"]
    assert rows(e, "SELECT COUNT(*) c FROM llm_calls WHERE tick_id=-1")[0]["c"] == 2     # its calls are kept
    boom["on"] = False
    assert e.tick()["kind"] == "tick"
    with pytest.raises(RuntimeError):
        boom["on"] = True
        e.tick()
    assert [r["tick_id"] for r in rows(e, "SELECT tick_id FROM tick_traces WHERE tick_id<0 ORDER BY tick_id DESC")] == [-1, -2]


def test_trace_write_failure_never_breaks_the_tick(cfg, monkeypatch, caplog):
    e, b = make(cfg)

    def broken(*a, **k):
        raise RuntimeError("trace disk full")
    monkeypatch.setattr(e.tracer, "_write", broken)
    with caplog.at_level("WARNING", logger="cat.trace"):
        info = e.tick()
    assert info["kind"] == "tick" and "could not write trace" in caplog.text
    assert rows(e, "SELECT COUNT(*) c FROM ticks")[0]["c"] == 1
    assert rows(e, "SELECT COUNT(*) c FROM tick_traces")[0]["c"] == 0
    monkeypatch.undo()
    e.tick()                                                  # and the next tick traces normally
    assert rows(e, "SELECT COUNT(*) c FROM tick_traces")[0]["c"] == 1


def test_a_failing_recorder_never_breaks_a_call():
    b = MockBackend()
    b.recorder = lambda *a: (_ for _ in ()).throw(RuntimeError("recorder bug"))
    assert call_json(b, "brain", "m", [], {}, lambda r: r, retries=0)


def test_summary_level_stores_no_prompt_or_response_text(cfg):
    cfg.settings["trace_level"] = "summary"
    e, b = make(cfg)
    scripted(b, feeling="positive")
    e.memory.save(e.memory.prepare("an old memory", 0.9, "2026-11-17T10:00"), "long")
    e.tick()
    c = rows(e, "SELECT * FROM llm_calls")[0]
    assert c["system_prompt"] is None and c["user_prompt"] is None and c["raw_response"] is None
    assert c["schema"] is None and c["parsed"] is None
    assert c["total_ms"] is not None and c["prompt_tokens"] and c["valid_json"] == 1      # numbers are kept
    assert all(r["input_text"] is None for r in rows(e, "SELECT input_text FROM embed_calls"))
    rl = rows(e, "SELECT * FROM recall_logs")[0]
    assert rl["query_text"] is None and "text" not in json.loads(rl["candidates"])[0]
    assert rows(e, "SELECT COUNT(*) c FROM need_math")[0]["c"] > 0        # small tables unaffected


def test_trace_can_be_disabled(cfg):
    cfg.settings["trace_enabled"] = False
    e, b = make(cfg)
    e.run(ticks=5, interval=0)
    for t in ("tick_traces", "llm_calls", "embed_calls", "need_math", "effect_logs", "mood_math", "event_rolls"):
        assert rows(e, f"SELECT COUNT(*) c FROM {t}")[0]["c"] == 0, t


# ---------- sleep ----------
def test_sleep_cycle_traced_with_proposed_vs_clamped(cfg):
    e, b = make(cfg)
    b.handlers["tendencies"] = lambda m, s: {"changes": {"boldness": 1, "sociability": -3}}
    e.tick()                                                  # a day to consolidate
    e.state["sim_time"] = "2026-11-18T23:00"
    e.state["energy"] = -45.0
    info = e.tick()
    assert info["kind"] == "asleep"
    kinds = [r["kind"] for r in rows(e, "SELECT kind FROM llm_calls WHERE tick_id=? ORDER BY id", info["id"])]
    assert kinds == ["sleep_consolidate", "sleep_tendencies", "sleep_preferences", "sleep_self_image", "sleep_projects"]
    assert "sleep" in json.loads(rows(e, "SELECT stage_timings s FROM tick_traces WHERE tick_id=?", info["id"])[0]["s"])
    log_id = rows(e, "SELECT id FROM sleep_log")[0]["id"]
    st = {r["step"]: r for r in rows(e, "SELECT * FROM sleep_traces WHERE sleep_log_id=?", log_id)}
    assert set(st) == {"consolidate", "tendencies", "preferences", "self_image", "projects"}
    prop, clamped = json.loads(st["tendencies"]["proposed"]), json.loads(st["tendencies"]["clamped"])
    step = cfg.settings["sleep"]["tendency_max_step"]
    assert prop["boldness"] == 1 and clamped["boldness"]["applied_delta"] == pytest.approx(step)
    assert prop["sociability"] == -3 and clamped["sociability"]["applied_delta"] == pytest.approx(-step)
    assert json.loads(st["consolidate"]["input"])["day_log"] is not None
    assert rows(e, "SELECT tick_id FROM memories WHERE kind='long'")[0]["tick_id"] == info["id"]


def test_forced_cycle_style_flush_merges_into_an_existing_tick_trace(cfg):
    e, b = make(cfg)
    info = e.tick()
    e.tracer.begin()
    with e.tracer.stage("sleep"):
        pass
    e.tracer.flush(e.conn, info["id"], "ok")
    stages = json.loads(rows(e, "SELECT stage_timings s FROM tick_traces WHERE tick_id=?", info["id"])[0]["s"])
    assert "sleep" in stages and "brain" in stages            # merged, not replaced
    assert rows(e, "SELECT COUNT(*) c FROM tick_traces")[0]["c"] == 1


# ---------- retention ----------
def test_retention_prunes_big_tables_and_keeps_small_ones(cfg):
    cfg.settings["trace_retention_days"] = 14
    e, b = make(cfg)
    e.run(ticks=6, interval=0)
    old = (datetime.now() - timedelta(days=20)).isoformat(timespec="milliseconds")
    first = rows(e, "SELECT MIN(tick_id) t FROM tick_traces")[0]["t"]
    e.conn.execute("UPDATE llm_calls SET started_at=? WHERE tick_id<=?", (old, first + 2))
    e.conn.execute("UPDATE embed_calls SET started_at=? WHERE tick_id<=?", (old, first + 2))
    e.conn.execute("UPDATE tick_traces SET started_at=? WHERE tick_id<=?", (old, first + 2))
    e.conn.execute("INSERT INTO recall_logs(tick_id,query_text,candidates) VALUES(?,?,?)", (first, "q", "[]"))
    kept = {t: rows(e, f"SELECT COUNT(*) c FROM {t}")[0]["c"] for t in
            ("tick_traces", "need_math", "effect_logs", "mood_math", "event_rolls")}
    out = trace.prune(e.conn, cfg)
    assert out["llm_calls"] > 0 and out["embed_calls"] > 0 and out["recall_logs"] >= 1
    assert rows(e, "SELECT COUNT(*) c FROM llm_calls WHERE started_at=?", old)[0]["c"] == 0
    assert rows(e, "SELECT COUNT(*) c FROM llm_calls")[0]["c"] > 0                      # recent ones stay
    for t, n in kept.items():
        assert rows(e, f"SELECT COUNT(*) c FROM {t}")[0]["c"] == n, t                  # the small tables stay


def test_retention_runs_during_sleep(cfg):
    cfg.settings["trace_retention_days"] = 1
    e, b = make(cfg)
    e.tick()
    old = (datetime.now() - timedelta(days=5)).isoformat(timespec="milliseconds")
    e.conn.execute("UPDATE llm_calls SET started_at=?", (old,))
    e.state["sim_time"] = "2026-11-18T23:00"
    e.state["energy"] = -45.0
    e.tick()                                                  # falls asleep: cycle + retention
    assert rows(e, "SELECT COUNT(*) c FROM llm_calls WHERE started_at=?", old)[0]["c"] == 0


# ---------- the pieces ----------
def test_explain_needs_describes_ignored_and_invalid_gains(cfg):
    before = {k: 50.0 for k in cfg.character["needs"]}
    after = character.apply_gains(character.decay(before, 60, cfg), {"sustenance": "++", "joy": "huge"}, cfg)
    rows_ = {r["need"]: r for r in character.explain_needs(before, 60, {"sustenance": "++", "joy": "huge"}, cfg, after)}
    assert "ignored" in rows_["sustenance"]["bucket"] and rows_["sustenance"]["gain_raw"] == 0
    assert "invalid" in rows_["joy"]["bucket"]
    eaten = character.apply_gains(character.decay(before, 60, cfg), {}, cfg, consumed=True)
    r = {x["need"]: x for x in character.explain_needs(before, 60, {}, cfg, eaten, consumed=True)}
    assert r["sustenance"]["gain_raw"] == cfg.settings["gain_values"][cfg.settings["consume_min_gain"]]


def test_rng_stream_is_unchanged_by_event_tracing(cfg, conn):
    from cat import clock
    from cat.world import WorldState
    cfg.world["events"] = [{"text": f"e{i}", "chance": 0.5} for i in range(6)]
    cfg.settings["events_max_per_tick"] = 2
    dt = clock.parse(T0)
    a = WorldState.seed(conn, cfg, T0)
    b2 = WorldState.seed(conn, cfg, T0)
    r1, r2 = random.Random(5), random.Random(5)
    tr: list = []
    for _ in range(30):
        f1 = [e["text"] for e in a.roll_events(r1, dt, "kitchen", T0, trace=tr)]
        f2 = [e["text"] for e in b2.roll_events(r2, dt, "kitchen", T0)]
        assert f1 == f2
    assert any("skipped" in (t["filtered_reason"] or "") for t in tr)


def test_ollama_backend_captures_exact_call_details(cfg):
    def h(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"message": {"content": "<think>x</think>{\"action\": \"nap\"}"},
                                         "prompt_eval_count": 120, "eval_count": 30, "load_duration": 2_000_000,
                                         "prompt_eval_duration": 40_000_000, "eval_duration": 90_000_000,
                                         "total_duration": 150_000_000})
    b = OllamaBackend(cfg, client=httpx.Client(transport=httpx.MockTransport(h)))
    calls = []
    b.recorder = lambda *a: calls.append(a)
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]
    call_json(b, "brain", "qwen3:8b", msgs, {"type": "object"}, lambda r: r, retries=0)
    task, model, attempt, info, parsed, err = calls[0]
    assert (task, model, attempt, err) == ("brain", "qwen3:8b", 1, None) and parsed == {"action": "nap"}
    assert info["system_prompt"] == "SYS" and info["user_prompt"] == "USER" and info["schema"] == {"type": "object"}
    assert info["raw_response"].startswith("<think>")                       # raw, before any cleaning
    assert (info["prompt_tokens"], info["completion_tokens"]) == (120, 30)
    assert (info["load_ms"], info["prompt_eval_ms"], info["eval_ms"], info["total_ms"]) == (2, 40, 90, 150)
    assert info["options"]["temperature"] == cfg.settings["llm_temperature"]["brain"] and info["options"]["think"] is False
    assert info["valid_json"] == 1


def test_ollama_invalid_json_keeps_the_raw_text(cfg):
    b = OllamaBackend(cfg, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"message": {"content": "not json {"}}))))
    calls = []
    b.recorder = lambda *a: calls.append(a)
    with pytest.raises(BackendError):
        call_json(b, "narrator", "m", [], {}, lambda r: r, retries=0)
    info = calls[0][3]
    assert info["raw_response"] == "not json {" and info["valid_json"] == 0 and "invalid JSON" in calls[0][5]


# ---------- structured logs ----------
@pytest.fixture
def clean_root_logger():
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)


def test_json_log_format_and_file(cfg, tmp_path, clean_root_logger):
    import io
    cfg.settings["log_dir"] = str(tmp_path / "logs")
    buf = io.StringIO()
    logs.setup_logging("engine", cfg, stream=buf)
    lg = logging.getLogger("cat.test")
    lg.warning("hello %s", "world", extra={"event": "greeting", "extra": {"k": 1}})
    logs.set_tick_id(42)
    lg.info("inside a tick")
    try:
        raise ValueError("boom")
    except ValueError:
        lg.exception("it broke")
    logs.set_tick_id(None)
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert lines[0]["event"] == "greeting" and lines[0]["extra"] == {"k": 1} and lines[0]["tick_id"] is None
    assert {"ts", "level", "service", "tick_id", "event", "message", "extra"} <= set(lines[0])
    assert lines[0]["service"] == "engine" and lines[0]["level"] == "WARNING" and lines[0]["message"] == "hello world"
    assert lines[1]["tick_id"] == 42 and lines[1]["event"] == "cat.test"
    assert "ValueError: boom" in lines[2]["extra"]["exception"]
    on_disk = [json.loads(x) for x in (tmp_path / "logs" / "engine.jsonl").read_text().splitlines()]
    assert [x["message"] for x in on_disk] == [x["message"] for x in lines]


def test_log_file_rotates_by_size(cfg, tmp_path, clean_root_logger):
    import io
    cfg.settings.update(log_dir=str(tmp_path / "logs"), log_max_bytes=2000, log_backup_count=2)
    logs.setup_logging("web", cfg, stream=io.StringIO())
    for i in range(200):
        logging.getLogger("cat.rot").warning("line %d %s", i, "x" * 40)
    files = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert files == ["web.jsonl", "web.jsonl.1", "web.jsonl.2"]
    assert all(p.stat().st_size <= 2200 for p in (tmp_path / "logs").iterdir())


def test_setup_logging_is_idempotent_and_survives_unwritable_dir(cfg, tmp_path, clean_root_logger, capsys):
    import io
    cfg.settings["log_dir"] = str(tmp_path / "logs")
    for _ in range(3):
        logs.setup_logging("engine", cfg, stream=io.StringIO())
    assert sum(getattr(h, "_cat_structured", False) for h in logging.getLogger().handlers) == 2
    (tmp_path / "afile").write_text("x")
    cfg.settings["log_dir"] = str(tmp_path / "afile" / "sub")             # cannot be created
    logs.setup_logging("engine", cfg, stream=io.StringIO())
    logging.getLogger("cat.x").warning("still logging")                    # no crash
    assert "cannot write log files" in capsys.readouterr().err


def test_engine_ticks_log_with_their_tick_id(cfg, tmp_path, clean_root_logger):
    import io
    cfg.settings["log_dir"] = str(tmp_path / "logs")
    buf = io.StringIO()
    logs.setup_logging("engine", cfg, stream=buf)
    e, b = make(cfg)
    info = e.tick()
    recs = [json.loads(x) for x in buf.getvalue().splitlines()]
    tick_lines = [r for r in recs if r["event"] == "tick"]
    assert tick_lines and tick_lines[-1]["tick_id"] == info["id"]
    assert tick_lines[-1]["extra"]["kind"] == "tick" and tick_lines[-1]["extra"]["ms"] >= 0
    cfg.settings["log_tick_events"] = False
    buf.truncate(0); buf.seek(0)
    e.tick()
    assert not [x for x in buf.getvalue().splitlines() if json.loads(x)["event"] == "tick"]


def test_trace_settings_validated(cfg, tmp_path):
    import shutil, yaml
    from cat.config import CONFIG_DIR, ConfigError, load_config
    d = tmp_path / "c3"
    shutil.copytree(CONFIG_DIR, d)
    data = yaml.safe_load((d / "settings.yaml").read_text())
    data["trace_level"] = "everything"
    (d / "settings.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="trace_level"):
        load_config(d)

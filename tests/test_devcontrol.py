"""Part B2: the dev-command queue. The dashboard only inserts rows into dev.db; the engine (the
only writer of game state) runs them."""
import json
import random
import sqlite3

import pytest
import yaml

from cat import db, devqueue
from cat.devqueue import CommandError, DevQueue
from cat.engine import Engine
from cat.llm import MockBackend

HOUR = 3600.0


def make(cfg, seed=1):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    e = Engine(cfg, b, rng=random.Random(seed))
    return e, b, DevQueue(cfg.dev_db_path)        # a second connection = what the dashboard has


def cmd(q, name, **args):
    return q.enqueue(name, args)


def status_of(q, cid):
    r = q.conn.execute("SELECT status, result FROM dev_commands WHERE id=?", (cid,)).fetchone()
    return r["status"], r["result"]


def n_ticks(e):
    return e.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]


# ---------- the queue itself ----------
def test_enqueue_validates_commands_and_args(cfg):
    q = DevQueue(cfg.dev_db_path)
    with pytest.raises(CommandError, match="unknown command"):
        q.enqueue("rm_rf")
    for bad in ({"seconds": -1}, {"seconds": "fast"}, {"seconds": 10 ** 9}, {"seconds": 5, "ticks": 0},
                {"seconds": True}):
        with pytest.raises(CommandError):
            q.enqueue("set_interval", bad)
    with pytest.raises(CommandError):
        q.enqueue("inject_event", {"text": "  "})
    with pytest.raises(CommandError):
        q.enqueue("inject_event", {"text": "x", "spawn": {"name": ""}})
    assert q.enqueue("pause") and q.enqueue("set_interval", {"seconds": None})
    assert [r["status"] for r in q.recent()] == ["pending", "pending"]


def test_queue_status_and_config_roundtrip(cfg):
    q = DevQueue(cfg.dev_db_path)
    q.set_status({"state": "running", "n": 3})
    q.set_status({"state": "paused"})
    st = q.get_status()
    assert st["state"] == "paused" and st["n"] == 3 and st["_updated_at"]
    q.set_config("settings", "a: 1\n", "sha")
    q.set_config("settings", "a: 2\n", "sha2")
    assert q.get_config()["settings"]["content"] == "a: 2\n"


def test_dev_tables_live_in_dev_db_not_in_the_game_db(cfg):
    e, b, q = make(cfg)
    game = {r[0] for r in e.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    dev = {r[0] for r in q.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"dev_commands", "playground_runs", "engine_status", "engine_config"} <= dev
    assert not game & {"dev_commands", "playground_runs", "engine_status", "engine_config"}
    assert cfg.dev_db_path != cfg.db_path


# ---------- pause / step / resume, driven through the real run() loop ----------
def test_pause_step_resume_with_the_mock_backend(cfg):
    e, b, q = make(cfg)
    log = []
    ids = {}

    def fake_sleep(sec):                      # plays the dashboard: called while the loop idles
        n = len(log)
        log.append((n_ticks(e), e.paused))
        if n == 0:
            assert n_ticks(e) == 0                                           # paused before the first tick
            assert status_of(q, ids["pause"])[0] == "done"
            ids["s1"] = cmd(q, "step")
        elif n == 1:
            assert n_ticks(e) == 1                                           # exactly one tick ran
            assert status_of(q, ids["s1"]) == ("done", "ran tick #1 (tick)")
            ids["s2"] = cmd(q, "step")
        elif n == 2:
            assert n_ticks(e) == 2
            assert q.get_status()["state"] == "paused"
            ids["resume"] = cmd(q, "resume")

    ids["pause"] = cmd(q, "pause")
    done = e.run(ticks=5, interval=0, sleep=fake_sleep)
    assert done == 5 and n_ticks(e) == 5                                     # 2 steps + 3 after resuming
    assert status_of(q, ids["resume"]) == ("done", "resumed")
    assert len(log) == 3                                                     # idled exactly 3 times
    assert q.get_status()["state"] == "stopped"


def test_paused_iterations_do_not_count_as_ticks_and_step_needs_pause(cfg):
    e, b, q = make(cfg)
    a = cmd(q, "step")                                                       # not paused: rejected
    e.dev.process()
    st, res = status_of(q, a)
    assert st == "rejected" and "pause first" in res
    cmd(q, "pause")
    e.dev.process()
    s1 = cmd(q, "step")
    s2 = cmd(q, "step")
    e.dev.process()
    assert status_of(q, s1)[0] == "running" and status_of(q, s2)[0] == "rejected"
    assert "already queued" in status_of(q, s2)[1]
    assert e.paused and n_ticks(e) == 0                                      # a queued step is not a tick yet


def test_pause_during_the_wait_between_ticks_takes_effect_quickly(cfg):
    cfg.settings["dev_poll_seconds"] = 2
    e, b, q = make(cfg)
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) == 1:
            cmd(q, "pause")                                                  # arrives mid-wait
        if len(sleeps) == 3:
            assert n_ticks(e) == 1 and e.paused
            cmd(q, "resume")
    e.run(ticks=2, interval=60, sleep=fake_sleep)
    assert sleeps[:3] == [2, 2, 2] and n_ticks(e) == 2                       # sliced, not one 60s sleep
    assert sum(sleeps) < 60 * 2


def test_stop_while_paused_exits_and_records_clean_stop(cfg):
    e, b, q = make(cfg)
    cmd(q, "pause")
    e.run(ticks=3, interval=0, sleep=lambda s: setattr(e, "stop_requested", True))
    assert n_ticks(e) == 0 and db.get_meta(e.conn, "clean_stop") == "1"


def test_a_pause_is_not_downtime(cfg):
    cfg.settings.update(downtime_policy="catch_up", tick_interval_seconds=90, paused_alive_seconds=30)
    e, b, q = make(cfg)
    clock = {"t": 1_000_000.0}
    e.time = lambda: clock["t"]
    e.tick()
    cmd(q, "pause")

    def fake_sleep(sec):
        clock["t"] += 600                                                    # 10 real minutes pass per poll
        if clock["t"] > 1_000_000 + 4 * HOUR:
            e.stop_requested = True
    e.run(ticks=5, interval=0, sleep=fake_sleep)
    # crash (not clean stop) after hours of pause: last-alive was refreshed while paused
    db.set_meta(e.conn, "clean_stop", 0)
    e2 = Engine(cfg, b, conn=e.conn, rng=random.Random(2))
    e2.time = lambda: float(db.get_meta(e.conn, "last_alive_epoch")) + 60
    assert e2.startup_downtime() is None
    assert float(db.get_meta(e.conn, "last_alive_epoch")) > 1_000_000 + 3 * HOUR


# ---------- set_interval ----------
def test_set_interval_override_and_revert(cfg):
    e, b, q = make(cfg)
    base = cfg.settings["tick_interval_seconds"]
    assert e.effective_interval() == base
    a = cmd(q, "set_interval", seconds=5, ticks=2)
    e.dev.process()
    assert status_of(q, a)[0] == "done" and "5" in status_of(q, a)[1]
    assert e.effective_interval() == 5.0
    waits = []
    cfg.settings["dev_commands_enabled"] = False
    e.dev.q = None                                                           # plain sleeps to read the interval
    e.run(ticks=3, interval=None, sleep=waits.append)
    assert waits == [5.0, 5.0]                                               # two ticks at 5s, then back to settings
    assert e.effective_interval() == base
    cmd_q = q.enqueue("set_interval", {"seconds": 7})
    e.dev.q = q
    e.dev.process()
    assert e.effective_interval() == 7.0
    q.enqueue("set_interval", {"seconds": None})
    e.dev.process()
    assert e.effective_interval() == base


def test_interval_change_applies_to_a_wait_in_progress(cfg):
    cfg.settings["dev_poll_seconds"] = 2
    e, b, q = make(cfg)
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) == 1:
            cmd(q, "set_interval", seconds=3)                                # shorten the running wait
    e.run(ticks=2, interval=100, sleep=fake_sleep)
    assert sleeps == [2, 1]                                                  # 2s elapsed, new target 3s => 1s more


# ---------- force_sleep ----------
def test_force_sleep_runs_the_cycle_and_attaches_it_to_the_latest_tick(cfg):
    cfg.settings["sleep"]["fall_asleep_energy_below"] = 0                    # no natural sleep in this test
    e, b, q = make(cfg)
    b.handlers["brain"] = lambda m, s: {"reaction": "ok", "thought": "", "action": "watch the rain", "intent": "",
                                        "feeling": "positive", "wants_sleep": False}
    e.run(ticks=12, interval=0)
    last = e.conn.execute("SELECT MAX(id) FROM ticks").fetchone()[0]
    nights_before = e.conn.execute("SELECT COUNT(*) FROM sleep_log").fetchone()[0]
    c = cmd(q, "force_sleep")
    e.dev.process()
    st, res = status_of(q, c)
    assert st == "done" and "sleep cycle done" in res
    log = e.conn.execute("SELECT * FROM sleep_log ORDER BY id DESC").fetchall()
    assert len(log) == nights_before + 1 and log[0]["tick_id"] == last
    assert e.conn.execute("SELECT COUNT(*) FROM memories WHERE kind='short'").fetchone()[0] == 0
    kinds = {r[0] for r in e.conn.execute("SELECT kind FROM llm_calls WHERE tick_id=?", (last,))}
    assert {"sleep_consolidate", "sleep_tendencies", "sleep_preferences"} <= kinds
    stages = json.loads(e.conn.execute("SELECT stage_timings FROM tick_traces WHERE tick_id=?", (last,)).fetchone()[0])
    assert "sleep" in stages and "brain" in stages
    assert e.conn.execute("SELECT COUNT(*) FROM sleep_traces WHERE sleep_log_id=?", (log[0]["id"],)).fetchone()[0] == 3


def test_force_sleep_fails_clearly_when_offline_or_empty(cfg):
    e, b, q = make(cfg)
    c = cmd(q, "force_sleep")
    e.dev.process()
    assert status_of(q, c)[0] == "failed" and "no ticks yet" in status_of(q, c)[1]
    e.run(ticks=3, interval=0)
    b.online = False
    c = cmd(q, "force_sleep")
    e.dev.process()
    assert status_of(q, c)[0] == "failed" and "offline" in status_of(q, c)[1]
    b.online = True
    b.fail_next = 2                                                          # the first call fails twice
    c = cmd(q, "force_sleep")
    e.dev.process()
    assert status_of(q, c)[0] == "failed" and "postponed" in status_of(q, c)[1]
    assert db.get_meta(e.conn, "sleep_pending") == "1"                       # stays pending, nothing half-written
    c = cmd(q, "force_sleep")
    e.dev.process()
    assert status_of(q, c)[0] == "done"


# ---------- reload_settings ----------
def test_reload_settings_applies_live_and_reports_changes(cfg):
    e, b, q = make(cfg)
    before = cfg.settings["tick_interval_seconds"]
    path = cfg.config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data.update(tick_interval_seconds=12, brain_model="llama3.1:8b")
    data["repetition_penalty"]["window"] = 3
    path.write_text(yaml.safe_dump(data))
    same_dict = e.cfg.settings
    c = cmd(q, "reload_settings")
    e.dev.process()
    st, res = status_of(q, c)
    assert st == "done" and "tick_interval_seconds" in res and "brain_model" in res and "repetition_penalty" in res
    assert e.cfg.settings is same_dict and e.cfg.settings["tick_interval_seconds"] == 12 != before
    assert e.recent_action_vecs.maxlen == 3
    e.tick()
    assert e.conn.execute("SELECT model FROM llm_calls WHERE kind='brain'").fetchone()[0] == "llama3.1:8b"
    assert "llama3.1:8b" in q.get_config()["settings"]["content"]            # the snapshot is refreshed
    c = cmd(q, "reload_settings")
    e.dev.process()
    assert "nothing changed" in status_of(q, c)[1]


def test_reload_with_invalid_yaml_changes_nothing(cfg):
    e, b, q = make(cfg)
    snapshot = dict(e.cfg.settings)
    (cfg.config_dir / "settings.yaml").write_text("tick_interval_seconds: [unclosed")
    c = cmd(q, "reload_settings")
    e.dev.process()
    st, res = status_of(q, c)
    assert st == "failed" and "settings.yaml" in res and e.cfg.settings == snapshot
    (cfg.config_dir / "settings.yaml").write_text(yaml.safe_dump({**snapshot, "downtime_policy": "nope"}))
    c = cmd(q, "reload_settings")
    e.dev.process()
    assert status_of(q, c)[0] == "failed" and "downtime_policy" in status_of(q, c)[1]
    assert e.tick()["kind"] in ("tick", "asleep")                            # the engine is fine


def test_reload_keeps_cli_overrides(cfg):
    from cat.config import load_config
    c2 = load_config(cfg.config_dir, {"ollama_host": "http://127.0.0.1:11434"})
    e = Engine(c2, MockBackend(), conn=db.connect(cfg.db_path))
    e.reload_settings()
    assert e.cfg.settings["ollama_host"] == "http://127.0.0.1:11434"


# ---------- inject_event ----------
def test_inject_event_reaches_the_next_tick_prompt_events_and_trace(cfg):
    e, b, q = make(cfg)
    prompts = []
    b.handlers["brain"] = lambda m, s: (prompts.append(m[-1]["content"]), {
        "reaction": "", "thought": "", "action": "sit", "intent": "", "feeling": "neutral", "wants_sleep": False})[1]
    cfg.world["events"] = []
    c = cmd(q, "inject_event", text="A stranger leaves a parcel.",
            spawn={"name": "a parcel", "tags": ["movable", "not_allowed"], "area": "living_room"})
    e.dev.process()
    assert status_of(q, c)[0] == "done"
    info = e.tick()
    assert "A stranger leaves a parcel." in prompts[0]
    assert info["fired"] == ["A stranger leaves a parcel."]
    assert e.conn.execute("SELECT text FROM events WHERE tick_id=?", (info["id"],)).fetchone()[0] == "A stranger leaves a parcel."
    o = e.conn.execute("SELECT * FROM objects WHERE name='a parcel'").fetchone()
    assert o["created_by"] == "event" and json.loads(o["tags"]) == ["movable"]       # unknown tag filtered
    changes = json.loads(e.conn.execute("SELECT changes FROM ticks WHERE id=?", (info["id"],)).fetchone()[0])
    assert changes[0]["op"] == "create" and changes[0]["source"] == "event"
    roll = e.conn.execute("SELECT * FROM event_rolls WHERE tick_id=?", (info["id"],)).fetchone()
    assert roll["fired"] == 1 and "injected" in roll["filtered_reason"]
    nxt = e.tick()
    assert nxt["fired"] == []                                                # one-shot


def test_inject_event_rejects_unknown_areas(cfg):
    e, b, q = make(cfg)
    c = cmd(q, "inject_event", text="x", area="the_moon")
    e.dev.process()
    st, res = status_of(q, c)
    assert st == "failed" and "unknown area" in res
    c = cmd(q, "inject_event", text="x", spawn={"name": "y", "area": "mars"})
    e.dev.process()
    assert status_of(q, c)[0] == "failed"


# ---------- snapshot ----------
def test_snapshot_makes_a_consistent_backup(cfg):
    e, b, q = make(cfg)
    e.run(ticks=8, interval=0)
    c = cmd(q, "snapshot")
    e.dev.process()
    st, res = status_of(q, c)
    assert st == "done" and "-snapshot.db" in res
    path = res.split(" (")[0]
    snap = sqlite3.connect(path)
    assert snap.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 8
    assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    snap.close()


# ---------- robustness ----------
def test_unknown_and_malformed_rows_are_rejected_not_fatal(cfg):
    e, b, q = make(cfg)
    q.conn.execute("INSERT INTO dev_commands(created_at,command,args,status) VALUES('t','format_c','{}','pending')")
    q.conn.execute("INSERT INTO dev_commands(created_at,command,args,status) VALUES('t','set_interval','{\"seconds\": -4}','pending')")
    q.conn.execute("INSERT INTO dev_commands(created_at,command,args,status) VALUES('t','pause','not json','pending')")
    assert e.dev.process() == 3
    sts = [r["status"] for r in q.conn.execute("SELECT status FROM dev_commands ORDER BY id")]
    assert sts == ["rejected", "rejected", "rejected"] and not e.paused


def test_process_never_raises(cfg, monkeypatch):
    e, b, q = make(cfg)
    monkeypatch.setattr(e.dev.q, "pending", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    assert e.dev.process() == 0
    monkeypatch.undo()
    c = cmd(q, "snapshot")
    monkeypatch.setattr(e, "snapshot_db", lambda: (_ for _ in ()).throw(OSError("disk full")))
    e.dev.process()
    assert status_of(q, c)[0] == "failed" and "disk full" in status_of(q, c)[1]
    assert e.tick()["kind"] in ("tick", "asleep")


def test_engine_runs_without_a_dev_queue(cfg, tmp_path):
    cfg.settings["dev_commands_enabled"] = False
    e = Engine(cfg, MockBackend(), conn=db.connect(cfg.db_path))
    assert not e.dev.enabled
    waits = []
    assert e.run(ticks=3, interval=4, sleep=waits.append) == 3 and waits == [4, 4]
    cfg.settings["dev_commands_enabled"] = True
    cfg.settings["dev_db_path"] = str(tmp_path / "afile" / "dev.db")
    (tmp_path / "afile").write_text("not a directory")                       # cannot be created
    e2 = Engine(cfg, MockBackend(), conn=db.connect(cfg.db_path))
    assert not e2.dev.enabled and e2.run(ticks=2, interval=0) == 2


def test_status_is_published_for_the_dashboard(cfg):
    e, b, q = make(cfg)
    e.run(ticks=3, interval=0)
    s = q.get_status()
    assert s["state"] == "stopped" and s["last_tick_id"] == 3 and s["last_tick_ms"] >= 0
    assert s["interval_seconds"] == cfg.settings["tick_interval_seconds"] and s["pid"] and s["downtime_policy"] == "pause"
    cfg_rows = q.get_config()
    assert set(cfg_rows) == {"settings", "character", "world"}
    assert yaml.safe_load(cfg_rows["settings"]["content"])["downtime_policy"] == "pause" and cfg_rows["settings"]["raw_sha"]
    b.online = False
    e.dev.publish(force=True)
    e.tick()
    e.dev.publish(force=True)
    assert q.get_status()["state"] == "dozing"


def test_1000_ticks_with_dev_queue_enabled_still_clean(cfg):
    e, b, q = make(cfg, seed=7)
    assert e.run(ticks=1000, interval=0) == 1000
    assert n_ticks(e) == 1000

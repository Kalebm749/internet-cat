import random

import pytest

from cat import character, clock, db, downtime
from cat.config import ConfigError, load_config
from cat.engine import Engine
from cat.llm import MockBackend
from cat.web import views

HOUR = 3600.0


def make(cfg, policy="catch_up", interval=90, seed=1):
    cfg.settings["downtime_policy"] = policy
    cfg.settings["tick_interval_seconds"] = interval
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    e = Engine(cfg, b, rng=random.Random(seed))
    e.time = lambda: 1_000_000.0
    return e, b


def restart(cfg, e, b, later):
    """A new engine on the same DB, `later` real seconds after the old one's last sign of life."""
    e2 = Engine(cfg, b, conn=e.conn, rng=random.Random(2))
    e2.time = lambda: float(db.get_meta(e.conn, "last_alive_epoch")) + later
    return e2


def kinds(e):
    return [r[0] for r in e.conn.execute("SELECT kind FROM ticks ORDER BY id")]


# ---------- config ----------
def test_policy_validated(cfg, tmp_path):
    import shutil, yaml
    from cat.config import CONFIG_DIR
    d = tmp_path / "c2"
    shutil.copytree(CONFIG_DIR, d)
    p = d / "settings.yaml"
    data = yaml.safe_load(p.read_text())
    data["downtime_policy"] = "yolo"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="downtime_policy"):
        load_config(d)
    data.update(downtime_policy="catch_up", max_catch_up_hours=0)
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="max_catch_up_hours"):
        load_config(d)


def test_default_is_pause(cfg):
    assert load_config().settings["downtime_policy"] == "pause"
    assert load_config().settings["max_catch_up_hours"] == 8


# ---------- pure planning ----------
def test_plan_converts_real_gap_to_sim_minutes(cfg):
    cfg.settings.update(tick_interval_seconds=90, sim_minutes_per_tick_default=20)
    cu = downtime.plan(cfg, now=10 * 90 + 90 + 1000, last_alive=1000, clean_stop=False)
    assert cu.sim_minutes == 200 and not cu.capped and cu.gap_seconds == 900     # 10 ticks x 20 min


def test_plan_clean_stop_is_exact_crash_subtracts_one_interval(cfg):
    cfg.settings.update(tick_interval_seconds=100, sim_minutes_per_tick_default=20)
    assert downtime.plan(cfg, 1000 + 1000, 1000, clean_stop=True).sim_minutes == 200
    assert downtime.plan(cfg, 1000 + 1000, 1000, clean_stop=False).sim_minutes == 180


def test_plan_ignores_short_gaps_and_zero_interval(cfg):
    cfg.settings.update(tick_interval_seconds=90, catch_up_min_gap_ticks=3)
    assert downtime.plan(cfg, 1000 + 2 * 90, 1000, clean_stop=True) is None      # a normal restart
    assert downtime.plan(cfg, 1000 + 3 * 90, 1000, clean_stop=True) is not None
    cfg.settings["tick_interval_seconds"] = 0
    assert downtime.plan(cfg, 10 ** 9, 0, clean_stop=True) is None               # no pace to convert with


def test_plan_cap_is_in_sim_hours(cfg):
    cfg.settings.update(tick_interval_seconds=90, sim_minutes_per_tick_default=20, max_catch_up_hours=8)
    cu = downtime.plan(cfg, 100 * HOUR, 0, clean_stop=True)
    assert cu.sim_minutes == 480 and cu.capped and cu.raw_sim_minutes > 480


def test_description_night_vs_day(cfg):
    ph = cfg.settings["phases"]
    assert downtime.description("2026-11-18T22:00", 480, ph) == "dozed through the night"
    assert downtime.description("2026-11-18T02:00", 60, ph) == "dozed through the night"
    assert downtime.description("2026-11-18T12:30", 120, ph) == "dozed through the afternoon"
    assert downtime.description("2026-11-18T08:30", 60, ph) == "dozed through the morning"


# ---------- engine: pause (the default) ----------
def test_pause_keeps_sim_time_and_needs_exactly(cfg):
    e, b = make(cfg, policy="pause")
    e.run(ticks=5, interval=0)
    before = db.get_state(e.conn)
    e2 = restart(cfg, e, b, later=48 * HOUR)
    assert e2.startup_downtime() is None
    after = db.get_state(e2.conn)
    assert after["sim_time"] == before["sim_time"] and after["needs"] == before["needs"]
    e2.run(ticks=1, interval=0)
    assert "catchup" not in kinds(e2)


# ---------- engine: catch_up ----------
def test_catch_up_decays_advances_clock_and_writes_one_entry(cfg):
    e, b = make(cfg)
    e.run(ticks=5, interval=0)
    st0 = db.get_state(e.conn)
    n0 = len(kinds(e))
    e2 = restart(cfg, e, b, later=4 * HOUR)               # clean stop, 4 real hours => capped at 8 sim hours
    info = e2.startup_downtime()
    assert info["kind"] == "catchup" and "dozed through the" in info["outcome"]
    st = db.get_state(e2.conn)
    assert st["sim_time"] == clock.advance(st0["sim_time"], 480)       # 4h = 160 ticks x 20min, capped to 480
    for nid, spec in cfg.character["needs"].items():
        assert st["needs"][nid] == pytest.approx(max(0, st0["needs"][nid] - spec["decay_per_hour"] * 8))
    assert kinds(e2)[n0:] == ["catchup"]                               # exactly one entry
    row = e2.conn.execute("SELECT * FROM ticks WHERE kind='catchup'").fetchone()
    assert row["duration"] == 480 and row["sim_time"] == st0["sim_time"] and row["action"] is None
    assert db.get_meta(e2.conn, "clean_stop") == "0"                   # applied via the normal commit


def test_catch_up_below_cap_uses_the_real_gap(cfg):
    e, b = make(cfg)
    e.run(ticks=3, interval=0)
    t0 = db.get_state(e.conn)["sim_time"]
    e2 = restart(cfg, e, b, later=900)                     # 10 ticks' worth of real time
    e2.startup_downtime()
    assert db.get_state(e2.conn)["sim_time"] == clock.advance(t0, 200)


def test_catch_up_after_crash_subtracts_the_idle_wait(cfg):
    e, b = make(cfg)
    e.run(ticks=3, interval=0)
    db.set_meta(e.conn, "clean_stop", 0)                   # as if killed, not stopped
    t0 = db.get_state(e.conn)["sim_time"]
    e2 = restart(cfg, e, b, later=990)
    e2.startup_downtime()
    assert db.get_state(e2.conn)["sim_time"] == clock.advance(t0, 200)  # (990 - 90s) / 90 * 20


def test_normal_quick_restart_does_nothing(cfg):
    e, b = make(cfg)
    e.run(ticks=3, interval=0)
    e2 = restart(cfg, e, b, later=30)
    assert e2.startup_downtime() is None and "catchup" not in kinds(e2)


def test_first_start_has_nothing_to_catch_up(cfg):
    e, b = make(cfg)
    assert e.startup_downtime() is None


def test_asleep_through_downtime_restores_energy(cfg):
    e, b = make(cfg)
    e.run(ticks=2, interval=0)
    e.state["is_asleep"] = True
    e.state["energy"] = -40.0
    with db.transaction(e.conn):
        db.save_state(e.conn, e.state, "now")
    e2 = restart(cfg, e, b, later=2 * HOUR)
    e2.startup_downtime()
    st = db.get_state(e2.conn)
    assert st["is_asleep"] == 1 and st["energy"] > -40               # slept through it
    assert st["energy"] == pytest.approx(min(50, -40 + cfg.settings["sleep"]["restore_per_sim_hour"] * 8))


def test_catch_up_decay_multiplier_setting(cfg):
    cfg.settings["catch_up_decay_multiplier"] = 0.5
    e, b = make(cfg)
    e.run(ticks=2, interval=0)
    st0 = db.get_state(e.conn)
    restart(cfg, e, b, later=4 * HOUR).startup_downtime()
    st = db.get_state(e.conn)
    assert st["needs"]["joy"] == pytest.approx(max(0, st0["needs"]["joy"] - cfg.character["needs"]["joy"]["decay_per_hour"] * 4))


def test_run_applies_catch_up_before_the_first_tick(cfg):
    e, b = make(cfg)
    e.run(ticks=3, interval=0)
    e2 = restart(cfg, e, b, later=3 * HOUR)
    e2.run(ticks=2, interval=0)
    tail = kinds(e2)[-3:]
    assert tail[0] == "catchup" and "catchup" not in tail[1:]      # then normal life (it may be tired and nap)
    assert len(tail) == 3 and tail[1] in ("tick", "asleep")


def test_run_records_clean_stop_and_ticks_clear_it(cfg):
    e, b = make(cfg)
    e.run(ticks=2, interval=0)
    assert db.get_meta(e.conn, "clean_stop") == "1"
    assert float(db.get_meta(e.conn, "last_alive_epoch")) == 1_000_000.0
    e.tick()
    assert db.get_meta(e.conn, "clean_stop") == "0"          # running again: a kill now is a crash


def test_catch_up_failure_does_not_stop_the_engine(cfg, monkeypatch):
    e, b = make(cfg)
    e.run(ticks=2, interval=0)
    e2 = restart(cfg, e, b, later=3 * HOUR)
    monkeypatch.setattr(e2, "_apply_catch_up", lambda cu: (_ for _ in ()).throw(RuntimeError("boom")))
    assert e2.run(ticks=2, interval=0) == 2
    assert "catchup" not in kinds(e2)


def test_brain_is_told_it_dozed_after_catch_up(cfg):
    e, b = make(cfg)
    e.run(ticks=2, interval=0)
    prompts = []
    b.handlers["brain"] = lambda m, s: (prompts.append(m[-1]["content"]), {
        "reaction": "", "thought": "", "action": "sit", "intent": "", "feeling": "neutral", "wants_sleep": False})[1]
    e2 = restart(cfg, e, b, later=3 * HOUR)
    e2.startup_downtime()
    e2.state["is_asleep"] = False
    e2.tick()
    assert "hazy doze" in prompts[0]


def test_zero_interval_never_catches_up(cfg):
    e, b = make(cfg, interval=0)
    e.run(ticks=2, interval=0)
    assert restart(cfg, e, b, later=100 * HOUR).startup_downtime() is None


# ---------- website ----------
def test_catch_up_shows_in_the_diary(cfg):
    e, b = make(cfg)
    e.run(ticks=5, interval=0)
    restart(cfg, e, b, later=3 * HOUR).startup_downtime()
    entries, _ = views.diary_view(db.connect(cfg.db_path), cfg, limit=50)
    c = next(x for x in entries if x["key"].startswith("c"))
    assert c["type"] == "doze" and "dozed through the" in c["text"] and " from " in c["text"]
    assert {"from_label", "to_label", "day_number", "id"} <= set(c)
    assert sum(1 for x in entries if x["key"].startswith("c")) == 1


def test_pagination_with_catch_up_entries_is_still_complete(cfg):
    e, b = make(cfg)
    for _ in range(4):
        e.run(ticks=15, interval=0)
        e = restart(cfg, e, b, later=3 * HOUR)
    conn = db.connect(cfg.db_path)
    everything, _ = views.diary_view(conn, cfg, limit=1000)
    seen, before = [], None
    while True:
        page, before = views.diary_view(conn, cfg, before=before, limit=7)
        seen += page
        if not before:
            break
    assert [x["key"] for x in seen] == [x["key"] for x in everything]
    assert len({x["key"] for x in seen}) == len(seen)

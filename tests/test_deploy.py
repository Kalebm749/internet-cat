"""Deployment: compose runs the new engine, models, health, backups before migrations, the measurement script."""
import json
import subprocess
import sys
from pathlib import Path

import yaml

from cat import content, healthcheck, store, tuning
from cat.llm import MockBackend, wanted_models
from cat.loop import FakeClock, Loop

ROOT = Path(__file__).parent.parent


def test_compose_runs_the_new_engine_with_content_mounted():
    c = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert c["services"]["engine"]["command"] == ["python", "-m", "cat.nit"]
    vols = c["x-app"]["volumes"]
    assert "./content:/app/content:ro" in vols and "./config:/app/config:ro" in vols
    assert c["services"]["web"]["command"][1] == "cat.web.app:app"
    assert c["services"]["devdash"]["command"][1] == "cat.devdash.app:app"
    assert "COPY content ./content" in (ROOT / "Dockerfile").read_text()


def test_the_models_to_pull_are_the_creatures(cfg):
    assert wanted_models(cfg.settings) == ["qwen3:4b", "qwen3:14b", "nomic-embed-text"]


def test_engine_heartbeat_and_health(cfg):
    t = tuning.load()
    fc = FakeClock()
    lp = Loop(store.connect(cfg.db_path), content.load(), t, MockBackend(), clock=fc, cfg=cfg)
    assert not healthcheck.check_engine(cfg)[0]
    lp.step()
    ok, msg = healthcheck.check_engine(cfg)
    assert ok, msg


def test_a_life_is_backed_up_before_a_migration(tmp_path):
    path = tmp_path / "nit.db"
    conn = store.connect(path)
    for stmt in store.MIGRATIONS[0]:
        conn.execute(stmt)
    conn.execute("PRAGMA user_version = 1")
    assert store.migrate(conn) == list(range(2, store.SCHEMA_VERSION + 1))
    backups = list((tmp_path / "backups").glob("nit-*-pre-migration.db"))
    assert len(backups) == 1
    assert store.migrate(store.connect(tmp_path / "fresh.db"))                 # a new life: nothing to back up
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_backup_script_names_the_new_database():
    text = (ROOT / "deploy" / "backup.sh").read_text()
    assert 'DB="${CAT_DB:-$ROOT/data/nit.db}"' in text and "nit-????-??-??.db" in text


def test_measure_script_reports_a_run(cfg):
    t = tuning.load()
    fc = FakeClock()
    lp = Loop(store.connect(cfg.db_path), content.load(), t, MockBackend(seed=1), clock=fc, seed=1, cfg=cfg)
    lp.run(int(26 * 3600 / (t["time_scale"] * t["loop"]["step_seconds"])), sleep=fc.sleep)
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "measure_nit.py"), "--db", str(cfg.db_path), "--json"],
                         capture_output=True, text=True, check=True).stdout
    r = json.loads(out)
    assert r["run"]["decisions"] > 50 and r["validity"]["fallbacks"] == 0
    assert r["nights"]["count"] >= 1 and r["list_bias"]["choices"] > 0
    assert set(r) >= {"speed", "prompt", "hunger", "behaviour", "letters", "body"}

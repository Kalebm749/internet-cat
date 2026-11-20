"""scripts/measure_run.py: the numbers it reports come from the run, it can tell a list-order bias from an even
spread, and it only ever reads the database."""
import importlib.util
import random
import subprocess
import sys
from pathlib import Path

from cat import db
from cat.engine import Engine
from cat.llm import MockBackend
from tests.helpers import menu_of

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("measure_run", ROOT / "scripts" / "measure_run.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)


def _run(cfg, policy, ticks=60, seed=3):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    rng = random.Random(seed)

    def brain(m, s):
        items = menu_of(m)
        mid = items[0][0] if policy == "first" else rng.choice(items)[0]
        return {"reaction": "", "thought": "", "move": mid, "why": "", "feeling": "neutral", "entry": ""}
    b.handlers["brain"] = brain
    e = Engine(cfg, b, rng=random.Random(seed))
    e.run(ticks=ticks, interval=0)
    e.conn.close()
    return db.connect(cfg.db_path, readonly=True)


def test_measures_a_run(cfg):
    conn = _run(cfg, "random")
    m = mr.measure(conn, cfg)
    assert m["run"]["decisions"] == conn.execute("SELECT COUNT(*) FROM ticks WHERE kind='tick'").fetchone()[0] > 0
    assert m["brain"]["attempts"] >= m["run"]["decisions"] and m["brain"]["ok_rate_pct"] == 100.0
    assert m["position"]["picks"] > 0 and 0.2 < m["position"]["mean_position"] < 0.8
    assert abs(sum(m["position"]["deciles_pct"]) - 100) < 1
    for d in m["needs"]["bands_pct"].values():
        assert abs(sum(d.values()) - 100) < 1
    assert m["choices"]["distinct_moves"] > 1
    text = mr.report(m)
    assert "== brain" in text and "first tenth" in text and "some need empty" in text


def test_a_brain_that_always_takes_the_first_move_shows_up_as_list_bias(cfg):
    conn = _run(cfg, "first")
    p = mr.measure(conn, cfg)["position"]
    assert p["mean_position"] == 0.0 and p["first_tenth_share_pct"] == 100.0


def test_the_script_only_reads(cfg, tmp_path):
    conn = _run(cfg, "random", ticks=10)
    conn.close()
    before = Path(cfg.db_path).read_bytes()
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "measure_run.py"), "--db", cfg.db_path,
                          "--config-dir", str(Path(cfg.config_dir)), "--json"],
                         capture_output=True, text=True, check=True)
    assert '"position"' in out.stdout
    assert Path(cfg.db_path).read_bytes() == before
    assert "readonly=True" in (ROOT / "scripts" / "measure_run.py").read_text()

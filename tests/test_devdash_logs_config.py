"""Part B7: Logs and Config pages."""
import io
from cat.llm import BackendError
import json
import logging
import random
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from cat import logs
from cat.devdash import data
from cat.devdash.app import create_app
from cat.engine import Engine
from cat.llm import MockBackend

STATIC = Path(__file__).resolve().parent.parent / "cat" / "devdash" / "static"
HDR = {"X-Requested-With": "devdash"}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)


def client(cfg):
    return TestClient(create_app(cfg))


def line(ts, svc="engine", level="INFO", tick=None, event="x", msg="hello", extra=None):
    return json.dumps({"ts": ts, "level": level, "service": svc, "tick_id": tick, "event": event, "message": msg, "extra": extra or {}})


def write(cfg, name, lines):
    d = cfg.log_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(lines) + "\n")


def make_engine(cfg, seed=3):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    return Engine(cfg, b, rng=random.Random(seed)), b


# =================================== logs ===================================
def test_merge_order_and_filters(cfg):
    write(cfg, "engine.jsonl", [line("2026-11-18T10:00:01.000", "engine", "INFO", 1, "tick", "tick 1 done"),
                                line("2026-11-18T10:00:03.000", "engine", "WARNING", 2, "effects", "rejected change {'op': 'x'}"),
                                line("2026-11-18T10:00:05.000", "engine", "ERROR", 3, "cat.engine", "tick failed", {"exception": "Traceback ... KeyError"})])
    write(cfg, "web.jsonl", [line("2026-11-18T10:00:02.000", "web", "INFO", None, "cat.web", "GET /api/state"),
                             line("2026-11-18T10:00:04.000", "web", "WARNING", None, "cat.web", "slow request")])
    r = data.read_logs(cfg)
    assert [x["ts"][-6:-4] for x in r["lines"]] == ["05", "04", "03", "02", "01"]                 # newest first, services interleaved
    assert r["services"] == ["engine", "web"]
    assert [x["service"] for x in data.read_logs(cfg, service="web")["lines"]] == ["web", "web"]
    assert {x["level"] for x in data.read_logs(cfg, level="warning")["lines"]} == {"WARNING", "ERROR"}   # >=
    assert [x["level"] for x in data.read_logs(cfg, level="ERROR")["lines"]] == ["ERROR"]
    assert [x["message"] for x in data.read_logs(cfg, tick_id=2)["lines"]] == ["rejected change {'op': 'x'}"]
    assert len(data.read_logs(cfg, q="SLOW")["lines"]) == 1 and len(data.read_logs(cfg, q="keyerror")["lines"]) == 1   # also searches extra
    assert len(data.read_logs(cfg, q="effects")["lines"]) == 1                                  # ... and the event name
    assert len(data.read_logs(cfg, limit=2)["lines"]) == 2 and data.read_logs(cfg, limit=2)["lines"][0]["ts"].endswith("05.000")
    assert [x["ts"][-6:-4] for x in data.read_logs(cfg, since="2026-11-18T10:00:04.000")["lines"]] == ["05", "04"]
    assert data.read_logs(cfg, service="engine", level="warning", tick_id=3)["lines"][0]["message"] == "tick failed"


def test_malformed_lines_and_bad_service_names(cfg):
    write(cfg, "engine.jsonl", ["Traceback (most recent call last):", line("2026-11-18T10:00:00.000"), "{not json", "[1, 2]"])
    r = data.read_logs(cfg)["lines"]
    raw = [x for x in r if x["event"] == "raw"]
    assert len(raw) == 3 and raw[0]["level"] == "?" and raw[0]["service"] == "engine" and any("Traceback" in x["message"] for x in raw)
    assert all(x["id"] for x in r) and len({x["id"] for x in r}) == len(r)
    for bad in ("../../etc/passwd", "engine/../web", "..", "e;rm", "x" * 80):
        assert data.read_logs(cfg, service=bad)["lines"] == []
    assert data.read_logs(cfg, service="nope")["lines"] == []


def test_rotated_files_are_read_when_the_current_one_is_short(cfg):
    d = cfg.log_dir
    d.mkdir(parents=True)
    (d / "engine.jsonl").write_text("\n".join(line(f"2026-11-18T12:00:0{i}.000", msg=f"new {i}") for i in range(3)) + "\n")
    (d / "engine.jsonl.1").write_text("\n".join(line(f"2026-11-18T11:00:0{i}.000", msg=f"older {i}") for i in range(3)) + "\n")
    (d / "engine.jsonl.2").write_text("\n".join(line(f"2026-11-18T10:00:0{i}.000", msg=f"oldest {i}") for i in range(3)) + "\n")
    (d / "engine.jsonl.bak").write_text(line("2030-01-01T00:00:00.000", msg="not a rotation") + "\n")
    msgs = [x["message"] for x in data.read_logs(cfg, limit=8)["lines"]]
    assert msgs == ["new 2", "new 1", "new 0", "older 2", "older 1", "older 0", "oldest 2", "oldest 1"]
    assert "not a rotation" not in msgs and data.log_services(cfg) == ["engine"]
    assert len(data.read_logs(cfg, limit=2)["lines"]) == 2


@pytest.mark.parametrize("n,want", [(0, 5), (1, 5), (3, 5), (5, 5), (50, 5), (4000, 7)])
def test_tail_lines_across_block_boundaries(tmp_path, n, want):
    p = tmp_path / "f.log"
    p.write_text("".join(f"line number {i:06d} " + "x" * 30 + "\n" for i in range(n)))
    got = data.tail_lines(p, want, block=64)                                  # tiny blocks: many reads, partial first lines
    expect = [f"line number {i:06d} " + "x" * 30 for i in range(n)][-want:]
    assert got == expect


def test_tail_lines_edge_cases(tmp_path):
    assert data.tail_lines(tmp_path / "missing", 5) == []
    p = tmp_path / "e.log"
    p.write_text("")
    assert data.tail_lines(p, 5) == []
    p.write_text("a\nb\nc")                                                    # no trailing newline
    assert data.tail_lines(p, 2) == ["b", "c"]
    p.write_bytes(b"ok\n\xff\xfe broken utf8\nend\n")
    assert data.tail_lines(p, 3)[-1] == "end" and len(data.tail_lines(p, 3)) == 3


def test_big_log_file_is_tailed_from_the_end_quickly(cfg):
    d = cfg.log_dir
    d.mkdir(parents=True)
    with open(d / "engine.jsonl", "w") as f:
        for i in range(150_000):
            f.write(line(f"2026-11-18T10:{i // 3600 % 60:02d}:{i % 60:02d}.{i % 1000:03d}", tick=i, msg=f"line {i}") + "\n")
    assert (d / "engine.jsonl").stat().st_size > 20_000_000
    t0 = time.perf_counter()
    r = data.read_logs(cfg, limit=50)
    assert time.perf_counter() - t0 < 1.5 and len(r["lines"]) == 50
    assert max(int(x["message"].split()[1]) for x in r["lines"]) == 149_999
    assert r["scanned"] <= 5000                                               # never read the whole file


def test_logs_page_links_ticks_and_marks_levels(cfg):
    write(cfg, "engine.jsonl", [line("2026-11-18T10:00:01.000", "engine", "ERROR", 7, "boom", "tick failed", {"exception": "KeyError: 'x'"}),
                                line("2026-11-18T10:00:00.000", "engine", "INFO", None, "start", "<b>hi</b>")])
    html = client(cfg).get("/logs").text
    assert 'href="/ticks/7"' in html and "lv-error" in html and "KeyError" in html and "<details class=\"inline\">" in html
    assert "&lt;b&gt;hi&lt;/b&gt;" in html and "<b>hi</b>" not in html               # message text is escaped
    assert 'id="live-tail"' in html and "logs.js" in html and 'name="service"' in html and "WARNING" in html
    assert "no log lines match" not in html
    assert len(client(cfg).get("/logs?level=error").text.split("<tbody>")[1].split("</tbody>")[0].split("<tr ")) == 2


def test_logs_page_without_a_log_directory(cfg):
    assert not cfg.log_dir.exists()
    html = client(cfg).get("/logs").text
    assert "No log files" in html and "no log lines match" in html
    assert client(cfg).get("/api/dev/logs").json()["lines"] == []


def test_logs_api_and_live_tail_since(cfg):
    write(cfg, "engine.jsonl", [line("2026-11-18T10:00:01.000", msg="a"), line("2026-11-18T10:00:02.000", msg="b")])
    c = client(cfg)
    j = c.get("/api/dev/logs").json()
    assert [x["message"] for x in j["lines"]] == ["b", "a"] and j["newest"] == "2026-11-18T10:00:02.000"
    with open(cfg.log_dir / "engine.jsonl", "a") as f:
        f.write(line("2026-11-18T10:00:03.000", msg="c") + "\n")
    j2 = c.get(f"/api/dev/logs?since={j['newest']}").json()
    assert [x["message"] for x in j2["lines"]] == ["c", "b"]                            # >= since: the client de-duplicates by id
    assert {x["id"] for x in j2["lines"]} & {x["id"] for x in j["lines"]} == {j["lines"][0]["id"]}
    assert c.get("/api/dev/logs?limit=0").status_code == 422


def test_engine_structured_logs_show_up_with_their_tick_ids(cfg, root_logger):
    logs.setup_logging("engine", cfg, stream=io.StringIO())
    e, b = make_engine(cfg)
    def down(m, s):
        raise BackendError("the journal is down")                                          # a WARNING per tick
    b.handlers["journal"] = down
    e.run(ticks=15, interval=0)
    c = client(cfg)
    ticks = [x for x in data.read_logs(cfg, q="\"event\": \"tick\"", limit=100)["lines"] if x["event"] == "tick"]
    assert len(ticks) == 15 and sorted(x["tick_id"] for x in ticks) == list(range(1, 16))
    only = data.read_logs(cfg, tick_id=5)["lines"]
    assert only and all(x["tick_id"] == 5 for x in only) and any(x["event"] == "tick" for x in only)
    html = c.get("/logs?tick_id=5").text
    assert 'href="/ticks/5"' in html and c.get("/ticks/5").status_code == 200
    warn = data.read_logs(cfg, level="warning")["lines"]
    assert len(warn) >= 15 and all(x["level"] in ("WARNING", "ERROR") for x in warn)    # the failed journal entries
    narr = {x["tick_id"] for x in warn if "journal entry unavailable" in x["message"]}
    assert len(narr) >= 10 and narr <= set(range(1, 16))   # each tagged with ITS tick


def test_live_tail_script_parses():
    if shutil.which("node"):
        assert subprocess.run(["node", "--check", str(STATIC / "logs.js")], capture_output=True).returncode == 0


# ================================== config ==================================
def started(cfg):
    e, b = make_engine(cfg)
    return e, b


def edit_settings(cfg, **kv):
    p = cfg.config_dir / "settings.yaml"
    d = yaml.safe_load(p.read_text())
    d.update(kv)
    p.write_text(yaml.safe_dump(d, sort_keys=False))


def state_of(cfg):
    return {f["name"]: f["state"] for f in data.config_view(cfg)["files"]}


def test_config_matches_disk_right_after_start(cfg):
    started(cfg)
    v = data.config_view(cfg)
    assert v["engine_known"] and v["changed"] == [] and state_of(cfg) == {"settings": "same", "character": "same", "world": "same", "prompts": "same", "moves": "same"}
    f = v["files"][0]
    assert yaml.safe_load(f["loaded"])["downtime_policy"] == "pause" and f["loaded_at"] and f["applies"].startswith("live")
    assert [x["applies"] for x in v["files"][1:]] == ["on restart", "on restart", "live (reload_settings)", "live (reload_settings)"]


def test_config_shows_a_diff_when_files_change_on_disk(cfg):
    started(cfg)
    edit_settings(cfg, tick_interval_seconds=12, brain_model="llama3.1:8b")
    c = data.config_view(cfg)
    assert c["changed"] == ["settings"] and state_of(cfg)["character"] == "same"
    f = c["files"][0]
    assert f["state"] == "changed" and "-tick_interval_seconds: 90" in f["diff"] and "+tick_interval_seconds: 12" in f["diff"]
    assert "+brain_model: llama3.1:8b" in f["diff"] and "as loaded by the engine" in f["diff"]
    assert "tick_interval_seconds: 90" in f["loaded"]                                    # the loaded copy is untouched
    # character.yaml: flagged, and noted as needing a restart
    p = cfg.config_dir / "character.yaml"
    p.write_text(p.read_text().replace("name: Gregorovitch", "name: Mochi"))
    c = data.config_view(cfg)
    assert c["changed"] == ["settings", "character"] and c["files"][1]["applies"] == "on restart" and "+name: Mochi" in c["files"][1]["diff"]


def test_comment_only_changes_are_not_called_changes(cfg):
    started(cfg)
    p = cfg.config_dir / "world.yaml"
    p.write_text(p.read_text() + "\n# a harmless note\n")
    assert state_of(cfg)["world"] == "cosmetic" and data.config_view(cfg)["changed"] == []


def test_invalid_or_missing_files_are_reported_not_fatal(cfg):
    started(cfg)
    (cfg.config_dir / "settings.yaml").write_text("tick_interval_seconds: [oops")
    (cfg.config_dir / "world.yaml").unlink()
    v = data.config_view(cfg)
    st = {f["name"]: f for f in v["files"]}
    assert st["settings"]["state"] == "invalid" and "invalid YAML" in st["settings"]["error"] and st["settings"]["loaded"]
    assert st["world"]["state"] == "invalid" and "missing" in st["world"]["error"]
    html = client(cfg).get("/config").text
    assert "invalid YAML on disk" in html and "file is missing on disk" in html and "tick_interval_seconds: 90" in html


def test_config_before_the_engine_has_ever_run(cfg):
    v = data.config_view(cfg)
    assert not v["engine_known"] and {f["state"] for f in v["files"]} == {"not-loaded"} and v["files"][0]["disk"]
    html = client(cfg).get("/config").text
    assert "has not published its config yet" in html and "downtime_policy" in html


def test_reload_settings_button_flow_clears_the_settings_diff(cfg):
    e, b = started(cfg)
    edit_settings(cfg, tick_interval_seconds=12)
    p = cfg.config_dir / "character.yaml"
    p.write_text(p.read_text().replace("name: Gregorovitch", "name: Mochi"))
    c = client(cfg)
    html = c.get("/config").text
    assert "Changed on disk since the engine loaded them" in html and "settings, character" in html and 'data-command="reload_settings"' in html
    assert c.post("/api/dev/commands", json={"command": "reload_settings", "confirm": True}, headers=HDR).status_code == 202
    e.dev.process()                                                                      # the engine runs it
    assert e.cfg.settings["tick_interval_seconds"] == 12
    assert state_of(cfg) == {"settings": "same", "character": "changed", "world": "same", "prompts": "same", "moves": "same"}   # settings applied; character needs a restart
    assert "yaml.safe_load" not in c.get("/config").text and "tick_interval_seconds: 12" in c.get("/config").text


def test_config_page_is_read_only(cfg):
    started(cfg)
    html = client(cfg).get("/config").text
    assert "<textarea" not in html and "<form" not in html and html.count('class="copy"') >= 3
    assert "settings.yaml" in html and "character.yaml" in html and "world.yaml" in html and "matches disk" in html
    assert client(cfg).get("/api/dev/config").json()["files"][0]["state"] == "same"


# ================== filter forms as a browser submits them ==================
def test_empty_filter_fields_are_treated_as_unset_not_as_errors(cfg):
    """A browser sends an empty number box as `name=`; that used to 422 the whole page."""
    e, b = make_engine(cfg)
    e.run(ticks=10, interval=0)
    write(cfg, "engine.jsonl", [line("2026-11-18T10:00:00.000", tick=3)])
    c = client(cfg)
    for url in ("/ticks?status=&kind=&area=&q=&slow_ms=", "/llm?kind=&model=&valid=&slow_ms=&tick_id=",
                "/logs?service=&level=&tick_id=&q=&limit=300", "/api/dev/ticks?slow_ms=&q=", "/api/dev/llm?tick_id=&slow_ms=",
                "/api/dev/logs?tick_id=&since=&q=", "/playground?call_id="):
        assert c.get(url).status_code == 200, url
    assert c.get("/ticks?slow_ms=abc").status_code == 422 and c.get("/logs?tick_id=x").status_code == 422   # real errors still are
    assert len(c.get("/api/dev/logs?tick_id=").json()["lines"]) == 1
    assert c.get("/api/dev/ticks?slow_ms=").json()["total"] == 10

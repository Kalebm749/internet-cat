"""The Docker Compose deployment: structure checks (nothing is started), healthchecks, scripts."""
import io
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from cat import db, healthcheck, pullmodels
from cat.config import load_config
from cat.engine import Engine
from cat.llm import MockBackend
from cat.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "compose.yaml").read_text())
SERVICES = COMPOSE["services"]


# ---------- compose.yaml ----------
def test_services_present():
    assert {"ollama", "engine", "web", "devdash", "model-pull"} <= set(SERVICES)


def test_everything_restarts_and_has_healthchecks():
    for name in ("ollama", "engine", "web"):
        assert SERVICES[name]["restart"] == "unless-stopped", name
        hc = SERVICES[name]["healthcheck"]
        assert hc["test"] and hc["interval"] and hc["retries"], name


def test_ollama_is_gpu_enabled_and_not_published():
    o = SERVICES["ollama"]
    dev = o["deploy"]["resources"]["reservations"]["devices"][0]
    assert dev["driver"] == "nvidia" and dev["count"] == "all" and "gpu" in dev["capabilities"]
    assert o["environment"]["OLLAMA_KEEP_ALIVE"] == "-1"
    assert o["environment"]["OLLAMA_MAX_LOADED_MODELS"] == "2"
    assert o["environment"]["OLLAMA_NUM_PARALLEL"] == "1"
    for port in o.get("ports", []):                     # if published at all: loopback only
        assert str(port).startswith("127.0.0.1:"), port
    assert any(v.startswith("ollama:") for v in o["volumes"])
    assert "ollama" in COMPOSE["volumes"]


def test_only_the_website_is_exposed_to_the_network():
    for name, svc in SERVICES.items():
        for port in svc.get("ports", []):
            p = str(port)
            if p.startswith("127.0.0.1:"):
                continue
            if name == "devdash":                        # loopback unless DEVDASH_BIND is set in .env
                assert p == "${DEVDASH_BIND:-127.0.0.1}:8090:8090", p
                continue
            assert name == "web" and p.endswith(":8080"), f"{name} publishes {p}"


def test_devdash_is_loopback_by_default_and_gets_its_credentials():
    d = SERVICES["devdash"]
    assert d["ports"] == ["${DEVDASH_BIND:-127.0.0.1}:8090:8090"]
    assert {"DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"} <= set(d["environment"])
    assert d["restart"] == "unless-stopped" and d["healthcheck"]["test"]
    assert "cat.devdash.app:app" in d["command"] and "8090" in d["command"]
    env = (ROOT / ".env.example").read_text()
    assert "DEVDASH_BIND=127.0.0.1" in env and "DEVDASH_USER=" in env
    assert (ROOT / ".gitignore").read_text().split().count(".env") == 1


def test_config_is_readonly_data_is_a_bind_mount():
    for name in ("engine", "web", "devdash"):
        vols = SERVICES[name]["volumes"]
        assert "./config:/app/config:ro" in vols, name
        assert "./data:/app/data" in vols, name
        assert SERVICES[name]["environment"]["CAT_CONFIG_DIR"] == "/app/config"


def test_engine_starts_even_if_ollama_is_down():
    dep = SERVICES["engine"]["depends_on"]["ollama"]
    assert dep["condition"] == "service_started"        # NOT service_healthy: it must doze, not wait


def test_commands_and_healthcheck_targets():
    assert SERVICES["engine"]["command"] == ["python", "-m", "cat.main"]
    assert SERVICES["web"]["command"][0] == "uvicorn" and "cat.web.app:app" in SERVICES["web"]["command"]
    assert "cat.healthcheck" in SERVICES["engine"]["healthcheck"]["test"]
    assert "/healthz" in " ".join(SERVICES["web"]["healthcheck"]["test"])


def test_model_pull_is_an_opt_in_init_step():
    mp = SERVICES["model-pull"]
    assert mp["profiles"] == ["init"] and mp["restart"] == "no"
    assert "cat.pullmodels" in mp["command"]


def test_same_image_for_our_services_and_nothing_privileged():
    for name in ("engine", "web", "devdash", "model-pull"):
        assert SERVICES[name]["image"] == "internet-cat:local"
        assert SERVICES[name]["build"]["target"] == "runtime"
    for svc in SERVICES.values():
        assert not svc.get("privileged") and "docker.sock" not in json.dumps(svc)


def test_compose_variables_all_have_defaults_and_documented():
    text = (ROOT / "compose.yaml").read_text()
    for var in set(re.findall(r"\$\{(\w+)(:-[^}]*)?\}", text)):
        assert var[1], f"{var[0]} has no default"
        assert re.search(rf"^{var[0]}=", (ROOT / ".env.example").read_text(), re.M), f"{var[0]} not in .env.example"


@pytest.mark.skipif(not shutil.which("docker"), reason="docker not installed")
def test_docker_compose_config_validates():
    for extra in ([], ["--profile", "init"]):
        r = subprocess.run(["docker", "compose", *extra, "config", "-q"], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr


# ---------- Dockerfile / ignore files ----------
def test_dockerfile_is_slim_nonroot_multistage():
    d = (ROOT / "Dockerfile").read_text()
    stages = re.findall(r"^FROM (\S+)(?: AS (\w+))?", d, re.M)
    assert stages[0][0].startswith("python:3.12-slim")
    assert [s[1] for s in stages] == ["base", "test", "runtime"]      # runtime last = default target
    runtime = d.split("AS runtime")[1]
    assert re.search(r"^USER cat$", runtime, re.M)
    assert "requirements-dev.txt" not in runtime and "COPY tests" not in runtime
    assert "ARG CAT_UID" in d


def test_dockerignore_and_gitignore_keep_state_and_secrets_out():
    di = (ROOT / ".dockerignore").read_text().split()
    for entry in ("data/", ".git", ".env", ".venv/"):
        assert entry in di, entry
    assert ".env" in (ROOT / ".gitignore").read_text().split()


def test_default_ollama_host_is_the_compose_service(cfg):
    assert cfg.settings["ollama_host"] == "http://ollama:11434"


# ---------- engine heartbeat + healthcheck CLI ----------
def test_heartbeat_roundtrip(tmp_path):
    p = tmp_path / "sub" / "hb"
    assert healthcheck.heartbeat_age(p) is None
    healthcheck.write_heartbeat(p)
    assert 0 <= healthcheck.heartbeat_age(p) < 5
    healthcheck.write_heartbeat(tmp_path / "x" / "y" / "hb")   # creates directories
    assert not list(tmp_path.glob("sub/*.tmp"))


def test_write_heartbeat_never_raises(tmp_path):
    (tmp_path / "file").write_text("x")
    healthcheck.write_heartbeat(tmp_path / "file" / "hb")      # parent is a file: swallowed


def test_engine_loop_writes_heartbeat(cfg):
    e = Engine(cfg, MockBackend(), conn=db.connect(cfg.db_path))
    assert healthcheck.heartbeat_age(cfg.heartbeat_path) is None
    e.run(ticks=1, interval=0)
    assert healthcheck.heartbeat_age(cfg.heartbeat_path) < 5
    assert healthcheck.check_engine(cfg)[0]


def test_engine_unhealthy_without_or_with_stale_heartbeat(cfg):
    ok, msg = healthcheck.check_engine(cfg)
    assert not ok and "no heartbeat" in msg
    healthcheck.write_heartbeat(cfg.heartbeat_path)
    old = time.time() - cfg.settings["healthcheck"]["engine_max_age_seconds"] - 60
    os.utime(cfg.heartbeat_path, (old, old))
    ok, msg = healthcheck.check_engine(cfg)
    assert not ok and "old" in msg


def test_engine_limit_scales_with_tick_interval(cfg):
    cfg.settings["tick_interval_seconds"] = 1000
    assert healthcheck.engine_limit(cfg) == 3000
    healthcheck.write_heartbeat(cfg.heartbeat_path)
    old = time.time() - 2000
    os.utime(cfg.heartbeat_path, (old, old))
    assert healthcheck.check_engine(cfg)[0]                  # slow ticks are not "unhealthy"


def test_cli_exit_codes(cfg, monkeypatch, capsys):
    monkeypatch.setenv("CAT_CONFIG_DIR", str(cfg.root / "config"))
    assert healthcheck.main(["engine"]) == 1
    healthcheck.write_heartbeat(load_config().heartbeat_path)
    assert healthcheck.main(["engine"]) == 0
    assert "healthy" in capsys.readouterr().out
    assert healthcheck.main([]) == 2 and healthcheck.main(["http"]) == 2


def test_http_check_against_a_real_server(cfg, monkeypatch):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/ok" else 503)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        assert healthcheck.check_http(base + "/ok", 2)[0]
        assert not healthcheck.check_http(base + "/bad", 2)[0]
    finally:
        srv.shutdown()
    assert not healthcheck.check_http(base + "/ok", 1)[0]    # server gone


def test_config_dir_env_var(cfg, monkeypatch):
    monkeypatch.setenv("CAT_CONFIG_DIR", str(cfg.root / "config"))
    c = load_config()
    assert c.root == cfg.root and c.heartbeat_path == cfg.root / "data" / "engine.heartbeat"


# ---------- /healthz ----------
def test_healthz_without_and_with_db(cfg):
    c = TestClient(create_app(cfg))
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "db": False}    # up even before the engine
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    conn.close()
    assert c.get("/healthz").json() == {"status": "ok", "db": True}


# ---------- model pull ----------
def pull_client(have, pull_lines=None, fail_pull=False, calls=None):
    def h(req):
        if calls is not None:
            calls.append((req.method, req.url.path, req.content))
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in have]})
        if req.url.path == "/api/pull":
            if fail_pull:
                return httpx.Response(200, content=b'{"error": "pull model manifest: file does not exist"}\n')
            body = pull_lines or ['{"status":"pulling manifest"}', '{"status":"downloading","total":100,"completed":50}',
                                  '{"status":"downloading","total":100,"completed":100}', '{"status":"success"}']
            return httpx.Response(200, content=("\n".join(body) + "\n").encode())
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(h))


def test_wanted_models_deduplicated_in_order(cfg):
    assert pullmodels.wanted_models(cfg.settings) == ["qwen3:8b", "nomic-embed-text"]
    cfg.settings["narrator_model"] = "other:7b"
    assert pullmodels.wanted_models(cfg.settings) == ["qwen3:8b", "other:7b", "nomic-embed-text"]


def test_pull_skips_present_and_pulls_missing(cfg):
    calls, out = [], io.StringIO()
    rc = pullmodels.run(pull_client(["qwen3:8b"], calls=calls), "http://x", cfg.settings, out=out)
    pulled = [json.loads(c[2])["model"] for c in calls if c[1] == "/api/pull"]
    assert rc == 0 and pulled == ["nomic-embed-text"]
    assert "qwen3:8b: already present" in out.getvalue() and "all models ready" in out.getvalue()


def test_pull_force_and_bare_name_counts_as_latest(cfg):
    calls = []
    pullmodels.run(pull_client(["qwen3:8b", "nomic-embed-text:latest"], calls=calls), "http://x",
                   cfg.settings, out=io.StringIO())
    assert not [c for c in calls if c[1] == "/api/pull"]
    calls.clear()
    pullmodels.run(pull_client(["qwen3:8b", "nomic-embed-text:latest"], calls=calls), "http://x",
                   cfg.settings, force=True, out=io.StringIO())
    assert len([c for c in calls if c[1] == "/api/pull"]) == 2


def test_pull_failure_and_unreachable_are_nonzero(cfg):
    out = io.StringIO()
    assert pullmodels.run(pull_client([], fail_pull=True), "http://x", cfg.settings, out=out) == 1
    assert "does not exist" in out.getvalue() and "failed" in out.getvalue()

    def refused(req):
        raise httpx.ConnectError("refused", request=req)
    out = io.StringIO()
    rc = pullmodels.run(httpx.Client(transport=httpx.MockTransport(refused)), "http://x", cfg.settings, out=out)
    assert rc == 1 and "cannot reach Ollama" in out.getvalue()


# ---------- shell scripts ----------
@pytest.mark.parametrize("script", ["backup.sh", "update.sh", "pull-models.sh"])
def test_scripts_are_executable_and_parse(script):
    p = ROOT / "deploy" / script
    assert p.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["bash", "-n", str(p)], capture_output=True).returncode == 0
    assert "set -euo pipefail" in p.read_text()


FAKE_SQLITE3 = '''#!/usr/bin/env python3
# stand-in for the sqlite3 CLI (only what backup.sh uses), so the test does not need it installed
import re, sqlite3, sys
db, cmd = sys.argv[1], sys.argv[2]
m = re.match(r"\\.backup '(.*)'$", cmd)
if m:
    src = sqlite3.connect(db); dst = sqlite3.connect(m.group(1)); src.backup(dst); dst.close(); src.close()
elif cmd.strip().upper().startswith("PRAGMA INTEGRITY_CHECK"):
    print(sqlite3.connect(db).execute("PRAGMA integrity_check").fetchone()[0])
else:
    sys.exit("unsupported: " + cmd)
'''


@pytest.fixture
def backup_env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "sqlite3"
    fake.write_text(FAKE_SQLITE3)
    fake.chmod(0o755)
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "settings.yaml").write_text("backup_keep: 3   # keep\n")
    conn = db.connect(root / "data" / "cat.db")
    db.init_db(conn)
    db.set_meta(conn, "hello", "world")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "CAT_ROOT": str(root)}
    return root, env, conn


def run_backup(env):
    return subprocess.run(["bash", str(ROOT / "deploy" / "backup.sh")], env=env, capture_output=True, text=True)


def test_backup_creates_consistent_copy_while_db_is_open(backup_env):
    root, env, conn = backup_env
    r = run_backup(env)
    assert r.returncode == 0, r.stderr
    copies = list((root / "data" / "backups").glob("cat-*.db"))
    assert len(copies) == 1 and re.match(r"cat-\d{4}-\d\d-\d\d\.db", copies[0].name)
    assert not list((root / "data" / "backups").glob("*.partial"))
    assert db.get_meta(db.connect(copies[0]), "hello") == "world"


def test_backup_keeps_only_the_newest_n(backup_env):
    root, env, conn = backup_env
    dest = root / "data" / "backups"
    dest.mkdir()
    for i in range(6):                                          # older copies, oldest first
        f = dest / f"cat-2026-01-0{i + 1}.db"
        f.write_text("old")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    assert run_backup(env).returncode == 0                      # keep=3 from settings.yaml
    left = sorted(p.name for p in dest.glob("cat-*.db"))
    assert len(left) == 3 and any(not n.startswith("cat-2026-01") for n in left)
    assert "cat-2026-01-01.db" not in left and "cat-2026-01-02.db" not in left
    env2 = {**env, "CAT_BACKUP_KEEP": "1"}
    assert run_backup(env2).returncode == 0 and len(list(dest.glob("cat-*.db"))) == 1


def test_backup_fails_clearly(backup_env, tmp_path):
    root, env, conn = backup_env
    (root / "data" / "cat.db").unlink()
    r = run_backup(env)
    assert r.returncode == 1 and "no database" in r.stderr
    nosql = {**env, "PATH": "/usr/bin:/bin"}
    (tmp_path / "bin" / "sqlite3").unlink()
    r = run_backup({**nosql, "CAT_ROOT": str(root)})
    assert r.returncode == 1 and ("sqlite3 is not installed" in r.stderr or "no database" in r.stderr)

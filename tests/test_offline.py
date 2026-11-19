"""Brain offline: the world keeps running (dozing), and the engine recovers on its own."""
import json
import random

import httpx
import pytest

from cat import db
from cat.engine import Engine
from cat.llm import MockBackend, OllamaBackend, hash_embed
from tests.helpers import pick


# ---------- a fake Ollama we can take down and bring back ----------
class FakeOllama:
    def __init__(self):
        self.models = ["qwen3:14b", "nomic-embed-text:latest"]
        self.mode = "up"            # up | down | hang (tags answer, chat times out)
        self.prompts = []
        self.requests = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.mode == "down":
            raise httpx.ConnectError("connection refused", request=req)
        path = req.url.path
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if self.mode == "hang":
            raise httpx.ReadTimeout("model is stuck", request=req)
        body = json.loads(req.content)
        if path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [hash_embed(t) for t in body["input"]]})
        props = body["format"]["properties"]
        if "reaction" in props:
            self.prompts.append(body["messages"][-1]["content"])
            out = pick(body["messages"], feeling="positive")
        elif "outcome" in props:
            out = {"outcome": "It smells of dust.", "gains": {"curiosity": "+"}, "effort": "low",
                   "duration": 15, "changes": []}
        elif "memories" in props:
            out = {"summary": "a day", "memories": [{"text": "sniffed the sofa", "importance": "low"}]}
        elif "changes" in props:
            out = {"changes": {}}
        else:
            out = {"preferences": []}
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(out)}})


@pytest.fixture
def rig(cfg):
    fake = FakeOllama()
    backend = OllamaBackend(cfg, client=httpx.Client(transport=httpx.MockTransport(fake)))
    return Engine(cfg, backend, rng=random.Random(1)), fake


def kinds(e, n):
    return [e.tick()["kind"] for _ in range(n)]


def test_kill_ollama_midrun_engine_keeps_going_and_recovers(rig, cfg):
    e, fake = rig
    assert kinds(e, 5) == ["tick"] * 5
    t_before = db.get_state(e.conn)["sim_time"]
    needs_before = dict(db.get_state(e.conn)["needs"])

    fake.mode = "down"                                      # Ollama goes away
    assert kinds(e, 8) == ["doze"] * 8
    st = db.get_state(e.conn)
    assert st["brain_online"] == 0
    assert st["sim_time"] > t_before                        # the clock kept moving
    assert st["needs"]["joy"] < needs_before["joy"]         # needs kept decaying
    assert all(0 <= v <= 100 for v in st["needs"].values())
    doze_rows = e.conn.execute("SELECT * FROM ticks WHERE kind='doze'").fetchall()
    assert len(doze_rows) == 8 and all(r["outcome"] == "dozing" and r["action"] is None for r in doze_rows)
    n_prompts = len(fake.prompts)

    fake.mode = "up"                                        # Ollama returns
    assert kinds(e, 3) == ["tick"] * 3
    assert db.get_state(e.conn)["brain_online"] == 1
    assert len(fake.prompts) == n_prompts + 3
    assert "just come round from a long, hazy doze" in fake.prompts[n_prompts]
    assert "just come round" not in fake.prompts[n_prompts + 1]      # only the first prompt
    assert db.get_meta(e.conn, "brain_last_seen")


def test_doze_restores_a_little_energy(rig, cfg):
    e, fake = rig
    e.tick()
    e.state["energy"] = -10.0
    needs0 = dict(e.state["needs"])
    fake.mode = "down"
    e.tick()
    s = cfg.settings
    assert e.state["energy"] == pytest.approx(-10 + s["doze"]["energy_restore"])
    drop = cfg.character["needs"]["joy"]["decay_per_hour"] * s["doze"]["sim_minutes"] / 60
    assert e.state["needs"]["joy"] == pytest.approx(needs0["joy"] - drop)       # needs only decay


def test_hung_model_is_survived_via_retry_then_doze(rig):
    e, fake = rig
    fake.mode = "hang"                                      # /api/tags fine, chat times out
    info = e.tick()
    assert info["kind"] == "doze" and not info["brain_online"]
    fake.mode = "up"
    assert e.tick()["kind"] == "tick"


def test_health_check_cached_once_per_tick(rig):
    e, fake = rig
    tags = []
    orig = e.backend.is_online
    e.backend.is_online = lambda: (tags.append(1), orig())[1]
    e.tick()
    assert len(tags) == 1


def test_one_failure_among_good_calls_does_not_doze(cfg):
    b = MockBackend()
    b.need_ids = list(cfg.character["needs"])
    e = Engine(cfg, b, rng=random.Random(1))
    b.fail_next = 1                                         # retried transparently
    assert e.tick()["kind"] == "tick"


def test_events_still_fire_while_dozing(cfg):
    cfg.world["events"] = [{"text": "It rains.", "chance": 1.0, "set_weather": "rain"}]
    b = MockBackend()
    b.online = False
    e = Engine(cfg, b, rng=random.Random(1))
    info = e.tick()
    assert info["kind"] == "doze" and info["fired"] == ["It rains."]
    assert db.get_state(e.conn)["weather"] == "rain"
    assert e.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_no_just_woke_note_after_sleeping_through_a_doze(cfg):
    b = MockBackend()
    prompts = []
    b.handlers["brain"] = lambda m, s: (prompts.append(m[-1]["content"]), pick(m))[1]
    e = Engine(cfg, b, rng=random.Random(1))
    b.online = False
    e.tick()
    b.online = True
    e.state["sim_time"] = "2026-11-18T23:00"
    e.state["energy"] = -45.0
    while not e.tick()["outcome"].startswith("wakes"):
        pass
    e.tick()
    assert "hazy doze" not in prompts[-1]


def test_restart_during_outage_still_dozes_then_recovers(cfg):
    fake = FakeOllama()
    mk = lambda: OllamaBackend(cfg, client=httpx.Client(transport=httpx.MockTransport(fake)))
    e = Engine(cfg, mk(), rng=random.Random(1))
    e.tick()
    fake.mode = "down"
    e.tick()
    e2 = Engine(cfg, mk(), conn=e.conn, rng=random.Random(2))   # engine restarted mid-outage
    assert e2.tick()["kind"] == "doze"
    fake.mode = "up"
    e2.tick()
    assert "hazy doze" in fake.prompts[-1]


def test_run_loop_survives_outage_and_random_flapping(cfg):
    b = MockBackend(seed=4)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    e = Engine(cfg, b, rng=random.Random(4))
    flap = random.Random(7)

    def maybe_flap(info):
        b.online = flap.random() > 0.3
        if flap.random() < 0.05:
            b.fail_next = 2
    assert e.run(ticks=1000, interval=0, on_tick=maybe_flap) == 1000
    k = dict(e.conn.execute("SELECT kind, COUNT(*) FROM ticks GROUP BY kind").fetchall())
    assert sum(k.values()) == 1000 and k["doze"] > 100 and k["tick"] > 100
    st = db.get_state(e.conn)
    assert all(0 <= v <= 100 for v in st["needs"].values())


def test_error_backoff_sleeps_longer_after_unexpected_failure(cfg, monkeypatch):
    cfg.settings["dev_commands_enabled"] = False             # plain single-sleep waits
    b = MockBackend()
    e = Engine(cfg, b, rng=random.Random(1))
    orig, calls = e._tick, {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return orig()
    monkeypatch.setattr(e, "_tick", flaky)
    waits = []
    e.run(ticks=3, interval=5, sleep=waits.append)
    assert waits == [cfg.settings["error_backoff_seconds"], 5]


def test_models_not_pulled_yet_is_a_clean_doze_then_recovers(rig, cfg):
    e, fake = rig
    fake.models = []                                       # fresh install, before deploy/pull-models.sh
    assert [e.tick()["kind"] for _ in range(3)] == ["doze"] * 3
    assert fake.prompts == []                              # no brain calls were even attempted
    fake.models = ["qwen3:14b", "nomic-embed-text"]         # bare name == :latest
    assert e.tick()["kind"] == "tick"

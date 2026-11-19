import json
import random

import pytest

from cat import db, memory as mem
from cat.engine import Engine
from cat.llm import MockBackend, hash_embed
from tests.helpers import pick

T0 = "2026-11-18T08:00"


@pytest.fixture
def M(cfg, conn):
    return mem.Memory(conn, cfg, MockBackend())


def add_long(M, text, importance=0.5, sim=T0, embed=True):
    m = M.prepare(text, importance, sim)
    if not embed:
        m["embedding"], m["embed_model"] = None, None
    M.save(m, "long")
    return m


# ---- notability ----
def test_notable_reasons(cfg):
    b = {"sustenance": 50.0, "joy": 50.0}
    assert mem.notable_reasons(cfg, b, dict(b), "neutral", [], []) == []
    assert "big change" in mem.notable_reasons(cfg, b, {"sustenance": 75.0, "joy": 50.0}, "neutral", [], [])[0]
    assert mem.notable_reasons(cfg, b, b, "positive", [], []) == ["felt positive"]
    assert mem.notable_reasons(cfg, b, b, "neutral", [{"op": "create"}], []) == ["something new appeared"]
    assert mem.notable_reasons(cfg, b, b, "neutral", [], [{"reason": "x"}]) == ["odd outcome"]
    assert len(mem.notable_reasons(cfg, b, b, "negative", [{"op": "create"}], [{"x": 1}])) == 3


def test_importance_scales_and_caps(cfg):
    assert mem.importance_for(cfg, 1) == pytest.approx(0.5)
    assert mem.importance_for(cfg, 99) == 1.0


# ---- storage ----
def test_blob_roundtrip_and_text_kept(M, conn):
    m = M.prepare("the oak was lovely", 0.7, T0)
    M.save(m, "short")
    row = conn.execute("SELECT * FROM memories").fetchone()
    assert row["text"] == "the oak was lovely" and row["embed_model"] == "nomic-embed-text"
    assert list(mem.from_blob(row["embedding"])) == pytest.approx(hash_embed("the oak was lovely"), abs=1e-6)


def test_embedding_failure_stores_memory_without_index(cfg, conn):
    b = MockBackend()
    b.online = False
    M = mem.Memory(conn, cfg, b)
    m = M.prepare("x", 0.5, T0)
    assert m["embedding"] is None and m["embed_model"] is None
    M.save(m, "short")
    assert M.recent_short()[0]["text"] == "x"


def test_recent_short_order_and_clear(M):
    for i in range(12):
        M.save(M.prepare(f"m{i}", 0.3, T0), "short")
    got = [m["text"] for m in M.recent_short()]
    assert got == [f"m{i}" for i in range(4, 12)]          # newest 8, oldest first
    M.clear_short()
    assert M.recent_short() == []


# ---- recall ----
def test_recall_relevance_ranks_similar_text_first(M):
    add_long(M, "climbed the old oak tree at sunset")
    add_long(M, "ate a bowl of fish in the kitchen")
    add_long(M, "chased a red ball down the hallway")
    out = M.recall(hash_embed("sitting under the old oak tree at sunset"), T0, k=3)
    assert out[0]["text"].startswith("climbed the old oak")
    assert all("embedding" not in o for o in out)


def test_recall_importance_and_k(M, cfg):
    cfg.settings["recall"]["w_relevance"] = 0
    add_long(M, "boring", importance=0.1)
    add_long(M, "vital", importance=0.9)
    out = M.recall(None, T0, k=1)
    assert [o["text"] for o in out] == ["vital"]


def test_recency_decays_with_half_life(M, cfg):
    cfg.settings["recall"].update(w_relevance=0, w_importance=0)
    add_long(M, "old", sim="2026-11-10T08:00")
    add_long(M, "new", sim="2026-11-18T07:00")
    assert M.recall(None, T0, k=1)[0]["text"] == "new"
    h = cfg.settings["recency_half_life_sim_hours"]
    assert M._recency("2026-11-18T08:00", clock_plus(h)) == pytest.approx(0.5)


def clock_plus(hours):
    from cat import clock
    return clock.advance(T0, hours * 60)


def test_recalled_memories_stay_fresh(M, conn, cfg):
    cfg.settings["recall"].update(w_relevance=0, w_importance=0)
    a = add_long(M, "a", sim="2026-11-01T08:00")
    add_long(M, "b", sim="2026-11-05T08:00")
    ida = conn.execute("SELECT id FROM memories WHERE text='a'").fetchone()["id"]
    assert M.recall(None, T0, k=1)[0]["text"] == "b"
    M.mark_recalled([ida], "2026-11-18T07:00")
    assert M.recall(None, T0, k=1)[0]["text"] == "a"


def test_recall_without_embeddings_or_query_falls_back(M):
    add_long(M, "no index", importance=0.9, embed=False)
    assert M.recall(hash_embed("anything"), T0)[0]["text"] == "no index"
    assert M.recall(None, T0)[0]["text"] == "no index"


def test_recall_ignores_other_embed_model_and_empty(M, conn, cfg):
    assert M.recall(None, T0) == []
    add_long(M, "oak", 0.5)
    conn.execute("UPDATE memories SET embed_model='other'")
    M._dirty = True
    out = M.recall(hash_embed("oak"), T0)
    assert out and M._matrix is None                         # unusable index => no crash


def test_matrix_reloads_after_new_long_memory(M):
    add_long(M, "first")
    M.recall(None, T0)
    add_long(M, "second thing about cats")
    out = M.recall(hash_embed("second thing about cats"), T0, k=1)
    assert out[0]["text"] == "second thing about cats"


# ---- pruning / reembed ----
def test_prune_long_keeps_most_valuable(M, cfg, conn):
    cfg.settings["long_term_cap"] = 3
    for i in range(6):
        add_long(M, f"m{i}", importance=i / 10)
    assert M.prune_long() == 3
    left = {r["text"] for r in conn.execute("SELECT text FROM memories")}
    assert left == {"m3", "m4", "m5"}
    assert M.prune_long() == 0


def test_reembed_all(M, conn):
    add_long(M, "one", embed=False)
    M.save(M.prepare("two", 0.5, T0), "short")
    assert mem.reembed_all(conn, MockBackend(), "new-model") == 2
    assert {r["embed_model"] for r in conn.execute("SELECT embed_model FROM memories")} == {"new-model"}


# ---- engine integration ----
def engine(cfg, seed=1):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    return Engine(cfg, b, rng=random.Random(seed)), b


def test_notable_tick_creates_short_memory_shown_in_next_prompt(cfg):
    e, b = engine(cfg)
    prompts = []
    b.handlers["brain"] = lambda m, s: (prompts.append(m[-1]["content"]), pick(m, "look out and watch the world for a while", feeling="positive", reaction="wow"))[1]
    e.tick()
    shorts = e.memory.all_short()
    assert len(shorts) == 1 and "look out and watch the world for a while" in shorts[0]["text"] and 'You felt: "wow"' in shorts[0]["text"]
    assert shorts[0]["embedding"] is not None and shorts[0]["importance"] >= 0.5
    e.tick()
    assert "Earlier today:" in prompts[1] and "look out and watch the world for a while" in prompts[1]
    assert "Earlier today" not in prompts[0]


def test_quiet_tick_makes_no_memory(cfg):
    e, b = engine(cfg)
    b.handlers["brain"] = lambda m, s: pick(m)
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok", "gains": {}, "effort": "low",
                                           "duration": 10, "changes": []}
    e.tick()
    assert e.memory.all_short() == []


def test_long_memory_recalled_into_prompt_and_marked(cfg):
    e, b = engine(cfg)
    m = e.memory.prepare("the red ball under the sofa was a lovely secret", 0.9, "2026-11-17T10:00")
    with db.transaction(e.conn):
        e.memory.save(m, "long")
    prompts = []
    b.handlers["brain"] = lambda msgs, s: (prompts.append(msgs[-1]["content"]), pick(msgs))[1]
    e.tick()
    assert "You remember: the red ball under the sofa" in prompts[0]
    row = e.conn.execute("SELECT last_recalled FROM memories WHERE kind='long'").fetchone()
    assert row["last_recalled"] == "2026-11-18T07:00"


def test_embed_outage_does_not_break_tick(cfg):
    e, b = engine(cfg)
    orig = b.embed

    def flaky(model, texts):
        from cat.llm import BackendError
        raise BackendError("no embeddings today")
    b.embed = flaky
    kinds = {e.tick()["kind"] for _ in range(20)}
    assert "tick" in kinds and "doze" not in kinds           # embedding outage never dozes the cat
    assert any(m["embedding"] is None for m in e.memory.all_short())


def test_1000_ticks_with_memory(cfg):
    e, _ = engine(cfg, seed=5)
    e.run(ticks=1000, interval=0)
    assert e.conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1000
    # short-term memories are consolidated into long-term ones at sleep
    assert e.conn.execute("SELECT COUNT(*) FROM memories WHERE kind='long'").fetchone()[0] > 0

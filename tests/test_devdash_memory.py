"""Part B5: memory tables, embedding map (PCA), search that scores exactly like the engine."""
import json
import random
import sqlite3

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from cat import db
from cat.devdash import data
from cat.devdash.app import create_app
from cat.engine import Engine
from cat.llm import MockBackend, hash_embed
from cat.memory import Memory, to_blob

REAL_CLIENT = httpx.Client
TOPICS = {
    "oak": ["climbed the old oak tree at sunset", "the old oak tree creaked in the wind", "sat high in the old oak tree"],
    "fish": ["ate a bowl of fish in the kitchen", "the kitchen smelled of fish and butter", "stole a fish from the kitchen bowl"],
    "ball": ["chased the red ball down the hallway", "the red ball bounced along the hallway", "batted the red ball under the sofa"],
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)
    data._probe_cache.clear()


def make_engine(cfg, seed=3):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    return Engine(cfg, b, rng=random.Random(seed)), b


def add_memories(e, texts, kind="long", importance=0.5, day=17):
    ids = []
    for i, t in enumerate(texts):
        m = e.memory.prepare(t, importance, f"2026-11-{day:02d}T{(8 + i) % 24:02d}:{(i // 16) * 5:02d}")
        with db.transaction(e.conn):
            ids.append(e.memory.save(m, kind))
    return ids


@pytest.fixture
def topics(cfg):
    e, b = make_engine(cfg)
    ids = {}
    for name, texts in TOPICS.items():
        ids[name] = add_memories(e, texts, importance={"oak": 0.9, "fish": 0.5, "ball": 0.2}[name])
    return e, ids


def patch_embed(monkeypatch, fail=False):
    def h(req):
        if fail:
            raise httpx.ConnectError("refused", request=req)
        body = json.loads(req.content)
        return httpx.Response(200, json={"embeddings": [hash_embed(t) for t in body["input"]]})
    monkeypatch.setattr(httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(h), **kw))


def client(cfg):
    return TestClient(create_app(cfg))


# ---------- the map ----------
def test_map_is_a_real_pca_of_the_stored_embeddings(cfg, topics):
    e, ids = topics
    m = data.embedding_map(cfg)
    assert m["n"] == 9 and m["dims"] == 64 and m["model"] == "nomic-embed-text" and len(m["explained"]) == 2
    # independent PCA in the test
    rows = e.conn.execute("SELECT id, embedding FROM memories ORDER BY id DESC").fetchall()
    X = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]).astype(np.float64)
    Xc = X - X.mean(axis=0)
    _, S, Vt = np.linalg.svd(Xc, full_matrices=False)
    P = Xc @ Vt[:2].T
    assert m["explained"][0] == pytest.approx(S[0] ** 2 / (S ** 2).sum(), abs=1e-3)
    got = {p["id"]: (p["x"], p["y"]) for p in m["points"]}
    span = max(np.ptp(P[:, 0]), np.ptp(P[:, 1]))
    for r, p in zip(rows, P):
        gx, gy = got[r["id"]]
        # same geometry as the independent PCA (up to the sign of each axis, and the uniform scaling)
        assert abs(gx - 0.5) == pytest.approx(abs((p[0] - (P[:, 0].min() + P[:, 0].max()) / 2) / span) * 0.92, abs=2e-3)
        assert abs(gy - 0.5) == pytest.approx(abs((p[1] - (P[:, 1].min() + P[:, 1].max()) / 2) / span) * 0.92, abs=2e-3)
    xs, ys = [q[0] for q in got.values()], [q[1] for q in got.values()]
    assert min(xs + ys) >= 0 and max(xs + ys) <= 1


def test_map_keeps_topics_together(cfg, topics):
    e, ids = topics
    pts = {p["id"]: p for p in data.embedding_map(cfg)["points"]}
    dist = lambda a, b: ((pts[a]["x"] - pts[b]["x"]) ** 2 + (pts[a]["y"] - pts[b]["y"]) ** 2) ** .5
    within = np.mean([dist(a, b) for name in ids for a in ids[name] for b in ids[name] if a < b])
    across = np.mean([dist(a, b) for n1 in ids for n2 in ids if n1 < n2 for a in ids[n1] for b in ids[n2]])
    assert within < across


def test_map_point_attributes(cfg, topics):
    e, ids = topics
    add_memories(e, ["something from today"], kind="short", importance=0.3, day=18)
    pts = {p["id"]: p for p in data.embedding_map(cfg)["points"]}
    assert {pts[i]["kind"] for i in ids["oak"]} == {"long"} and any(p["kind"] == "short" for p in pts.values())
    assert all(pts[i]["importance"] == 0.9 for i in ids["oak"]) and all(pts[i]["importance"] == 0.2 for i in ids["ball"])
    ages = [p["age"] for p in pts.values()]
    assert min(ages) == 0.0 and max(ages) == 1.0 and all(0 <= a <= 1 for a in ages)      # newest = 0, oldest = 1
    assert pts[ids["oak"][0]]["text"] == TOPICS["oak"][0]


@pytest.mark.parametrize("n", [0, 1, 2])
def test_map_handles_tiny_data_sets(cfg, n):
    e, b = make_engine(cfg)
    add_memories(e, [f"memory number {i}" for i in range(n)])
    m = data.embedding_map(cfg)
    assert m["n"] == n and len(m["points"]) == n
    for p in m["points"]:
        assert 0 <= p["x"] <= 1 and 0 <= p["y"] <= 1
    if n == 0:
        assert m["note"] == "no memories yet"


def test_map_identical_vectors_do_not_crash(cfg):
    e, b = make_engine(cfg)
    add_memories(e, ["the very same sentence"] * 5)
    m = data.embedding_map(cfg)
    assert m["n"] == 5 and all(p["x"] == p["y"] == 0.5 for p in m["points"])


def test_map_excludes_other_models_and_dimensions_and_caps_points(cfg, topics):
    e, ids = topics
    e.conn.execute("UPDATE memories SET embed_model='old-model' WHERE id=?", (ids["oak"][0],))
    e.conn.execute("UPDATE memories SET embedding=? WHERE id=?", (to_blob([0.1, 0.2, 0.3]), ids["fish"][0]))
    m = data.embedding_map(cfg)
    assert m["n"] == 7 and "2 memories skipped" in m["note"]
    assert ids["oak"][0] not in {p["id"] for p in m["points"]} and ids["fish"][0] not in {p["id"] for p in m["points"]}
    cfg.settings["devdash"]["embedding_map_max_points"] = 3
    assert data.embedding_map(cfg)["n"] == 3
    e.conn.execute("UPDATE memories SET embed_model='old-model'")
    assert "current embed model" in data.embedding_map(cfg)["note"]


def test_map_page_draws_one_point_per_memory_with_hover_text(cfg, topics):
    e, ids = topics
    add_memories(e, ["<script>alert(1)</script> sneaky"], kind="short")
    html = client(cfg).get("/memory").text
    assert html.count('<circle') == 10 and "<title>" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert(1)" not in html
    assert 'href="#mem-' in html and 'id="mem-' in html


def test_api_map(cfg, topics):
    j = client(cfg).get("/api/dev/memory/map").json()
    assert j["n"] == 9 and {"x", "y", "text", "kind", "importance", "age", "recalled"} <= set(j["points"][0])


# ---------- search: exactly as the engine would compute it ----------
def test_search_scores_equal_the_engines_recall(cfg, topics, monkeypatch):
    e, ids = topics
    patch_embed(monkeypatch)
    query = "sitting in the old oak tree at sunset"
    s = data.memory_search(cfg, query)
    assert s["used_embedding"] and s["dims"] == 64 and s["error"] is None
    now = db.get_state(db.connect(cfg.db_path))["sim_time"]
    engine_top = e.memory.recall(hash_embed(query), now, k=cfg.settings["recall"]["k"])      # the engine's own code
    assert [r["memory_id"] for r in s["results"] if r["selected"]] == [m["id"] for m in engine_top]
    for r in s["results"]:
        if r["selected"]:
            ref = next(m for m in engine_top if m["id"] == r["memory_id"])
            assert r["score"] == pytest.approx(ref["score"], abs=1e-3)
        assert r["score"] == pytest.approx(r["terms"]["recency"] + r["terms"]["importance"] + r["terms"]["relevance"], abs=2e-4)
    assert s["results"][0]["memory_id"] in ids["oak"]                                       # the oak memory wins
    scores = [r["score"] for r in s["results"]]
    assert scores == sorted(scores, reverse=True) and sum(r["selected"] for r in s["results"]) == cfg.settings["recall"]["k"]
    assert {"recency", "importance", "relevance"} == set(s["results"][0]["terms"]) and s["weights"]["w_relevance"] == 1.5


def test_search_relevance_is_cosine_similarity(cfg, topics, monkeypatch):
    e, ids = topics
    patch_embed(monkeypatch)
    s = data.memory_search(cfg, TOPICS["fish"][0])
    top = next(r for r in s["results"] if r["memory_id"] == ids["fish"][0])
    assert top["relevance"] == pytest.approx(1.0, abs=1e-3)                                 # identical text => cosine 1
    other = next(r for r in s["results"] if r["memory_id"] == ids["ball"][0])
    assert other["relevance"] < 0.9


def test_search_when_embedding_is_down_falls_back_like_the_engine(cfg, topics, monkeypatch):
    patch_embed(monkeypatch, fail=True)
    s = data.memory_search(cfg, "anything")
    assert not s["used_embedding"] and "ConnectError" in s["error"]
    assert all(r["relevance"] == 0 for r in s["results"]) and s["results"][0]["memory_id"] in topics[1]["oak"]   # importance 0.9 wins
    html = client(cfg).get("/memory?q=anything").text
    assert "Could not embed the query" in html and "relevance = 0" in html


def test_search_page_and_api(cfg, topics, monkeypatch):
    patch_embed(monkeypatch)
    c = client(cfg)
    html = c.get("/memory?q=old+oak+tree").text
    assert "✓ selected" in html and "relevance (cosine)" in html and "your search" in html and 'class="qmark"' in html
    assert html.count("pt-sel") >= cfg.settings["recall"]["k"] - 1                          # selected points are ringed on the map
    j = c.get("/api/dev/memory/search?q=old+oak+tree").json()
    assert "vector" not in j and j["results"][0]["text"] and j["dims"] == 64
    assert c.get("/api/dev/memory/search").status_code == 422


def test_search_with_no_memories(cfg, monkeypatch):
    patch_embed(monkeypatch)
    s = data.memory_search(cfg, "hello")
    assert s["results"] == [] and "no long-term memories" in client(cfg).get("/memory?q=hello").text


# ---------- tables and recall stats ----------
def test_recall_stats_match_the_recall_logs(cfg):
    e, b = make_engine(cfg, seed=5)
    add_memories(e, [t for ts in TOPICS.values() for t in ts])
    e.run(ticks=60, interval=0)
    counts: dict[int, int] = {}
    n = 0
    for r in e.conn.execute("SELECT candidates FROM recall_logs"):
        n += 1
        for c in json.loads(r[0]):
            if c["selected"]:
                counts[c["memory_id"]] = counts.get(c["memory_id"], 0) + 1
    assert n > 20
    t = data.memory_tables(cfg)
    assert t["recall_events"] == n
    got = {m["id"]: m["times_recalled"] for m in t["long"]}
    for mid, c in counts.items():
        assert got[mid] == c
    never = {m["id"] for m in t["never_recalled"]}
    assert never == {m["id"] for m in t["long"] if m["times_recalled"] == 0} and not never & set(counts)
    assert [m["times_recalled"] for m in t["most_recalled"]] == sorted((m["times_recalled"] for m in t["most_recalled"]), reverse=True)
    assert t["never_total"] == len(never)


def test_tables_show_text_importance_created_recalled_and_links(cfg):
    e, b = make_engine(cfg, seed=2)
    e.run(ticks=60, interval=0)
    t = data.memory_tables(cfg)
    assert t["long_total"] == e.conn.execute("SELECT COUNT(*) FROM memories WHERE kind='long'").fetchone()[0] > 0
    m = t["long"][0]
    assert {"id", "text", "importance", "created_label", "last_recalled", "times_recalled", "has_embedding", "tick_id", "embed_model"} <= set(m)
    assert m["has_embedding"] and m["tick_id"]                                              # written by a known (sleep) tick
    html = client(cfg).get("/memory").text
    from markupsafe import escape
    assert str(escape(m["text"])) in html and f'href="/ticks/{m["tick_id"]}"' in html and "times recalled" in html


def test_long_term_table_pagination(cfg):
    e, b = make_engine(cfg)
    add_memories(e, [f"memory {i}" for i in range(23)])
    seen, page = [], 1
    while True:
        t = data.memory_tables(cfg, page, 10)
        seen += [m["id"] for m in t["long"]]
        if page >= t["pages"]:
            break
        page += 1
    assert len(seen) == len(set(seen)) == 23 and t["pages"] == 3
    assert len(client(cfg).get("/api/dev/memory?limit=5").json()["long"]) == 5


def test_memory_page_works_with_no_database(cfg):
    assert not cfg.db_path.exists()
    c = client(cfg)
    assert c.get("/memory").status_code == 200 and c.get("/api/dev/memory").status_code == 200
    assert "no memories yet" in c.get("/memory").text


def test_memory_page_never_writes_the_game_db(cfg, topics, monkeypatch):
    patch_embed(monkeypatch)
    before = [tuple(r) for r in sqlite3.connect(cfg.db_path).execute("SELECT id, last_recalled, importance FROM memories")]
    c = client(cfg)
    for u in ("/memory", "/memory?q=oak", "/api/dev/memory", "/api/dev/memory/map", "/api/dev/memory/search?q=fish"):
        assert c.get(u).status_code == 200
    assert [tuple(r) for r in sqlite3.connect(cfg.db_path).execute("SELECT id, last_recalled, importance FROM memories")] == before

"""Memory (docs/rewrite-contracts.md §5.1, §5.3): today's notable moments, long-term and core memories, recall,
and the learned preferences that colour consequences and the menu (§2.4).

    experiences      code's facts of everything that happened (written by cat/execute.py)
    notable moments  today's experiences that asked to be remembered or are important enough (a query, no table)
    long-term        written only by the sleep cycle, with links to the experiences they came from
    core             a few deliberately kept thoughts; never forgotten; always part of who it is

Embeddings are only a search index; text is the memory. Embedding calls happen outside transactions, and
when the embedding model is unavailable recall falls back to recency and importance.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

import numpy as np

from cat import clock
from cat.llm import BackendError, LLMBackend

log = logging.getLogger("cat.memories")


def to_blob(vec: list[float] | None) -> bytes | None:
    return None if vec is None else np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(b: bytes | None) -> np.ndarray | None:
    return None if not b else np.frombuffer(b, dtype=np.float32)


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None or len(a) != len(b):
        return 0.0
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    return 0.0 if na == 0 or nb == 0 else float(np.dot(a, b) / (na * nb))


def embed(backend: LLMBackend | None, model: str, texts: list[str]) -> list[list[float] | None]:
    """Embed texts; on failure every vector is None (recall then ignores relevance)."""
    if backend is None or not texts:
        return [None] * len(texts)
    try:
        return list(backend.embed(model, texts))
    except BackendError as e:
        log.warning("embedding unavailable: %s", e)
        return [None] * len(texts)


@dataclass
class Memory:
    id: int
    kind: str
    text: str
    importance: float
    embedding: np.ndarray | None
    created_at: str
    sources: list[int]
    recall_count: int
    last_recalled: str | None
    strength: float = 0.0            # what it went in with (0: the importance the sleep model gave)
    occurrences: int = 1             # repeats folded into this one memory
    feeling: str | None = None       # good | bad | flat: how it left the creature
    action_id: str | None = None
    location: str | None = None
    recall_days: int = 0             # distinct days it came back
    gist: str | None = None


def _row(r: sqlite3.Row) -> Memory:
    return Memory(r["id"], r["kind"], r["text"], r["importance"], from_blob(r["embedding"]), r["created_at"],
                  json.loads(r["sources"]), r["recall_count"], r["last_recalled"], r["strength"], r["occurrences"],
                  r["feeling"], r["action_id"], r["location"], r["recall_days"], r["gist"])


def all_memories(conn: sqlite3.Connection, kind: str | None = None) -> list[Memory]:
    if kind:
        return [_row(r) for r in conn.execute("SELECT * FROM memories WHERE kind = ? ORDER BY id", (kind,))]
    return [_row(r) for r in conn.execute("SELECT * FROM memories ORDER BY id")]


def core_texts(conn: sqlite3.Connection) -> list[str]:
    return [m.text for m in all_memories(conn, "core")]


def last_wake(conn: sqlite3.Connection, sleep_ids: list[str]) -> str:
    """When it last woke up ('' if it never slept). `sleep_ids`: the content's builtin sleep action ids."""
    if not sleep_ids:
        return ""
    marks = ",".join("?" * len(sleep_ids))
    r = conn.execute(f"SELECT MAX(ended_at) FROM activities WHERE status = 'finished' AND action_id IN ({marks})",
                     sleep_ids).fetchone()
    return r[0] or ""


def notable_today(conn: sqlite3.Connection, tuning: dict, since: str) -> list[str]:
    """Today's notable moments, with near-duplicate thoughts collapsed, newest last."""
    m = tuning["memory"]
    if m["notable_k"] == 0:
        return []
    rows = conn.execute(
        "SELECT e.text, t.embedding FROM experiences e "
        "LEFT JOIN activities a ON a.id = e.activity_id "
        "LEFT JOIN thoughts t ON t.decision_id = a.decision_id "
        "WHERE e.sim_time >= ? AND (e.remember = 1 OR e.importance >= ?) "
        "ORDER BY e.id DESC LIMIT ?", (since, m["notable_min_importance"], m["notable_k"] * 4)).fetchall()
    threshold = tuning["repetition"]["thought_dup_threshold"]
    selected: list[tuple[str, Any]] = []
    for row in rows:
        text, embedding = row["text"], from_blob(row["embedding"])
        key = " ".join(text.casefold().split())
        if any(key == " ".join(old.casefold().split()) or
               (embedding is not None and old_vec is not None and cosine(embedding, old_vec) >= threshold)
               for old, old_vec in selected):
            continue
        selected.append((text, embedding))
        if len(selected) >= m["notable_k"]:
            break
    return [text for text, _ in reversed(selected)]


def recency(half_life_days: float, then: str, now: str) -> float:
    age_days = max(0.0, (clock.parse(now) - clock.parse(then)).total_seconds() / 86400)
    return 0.5 ** (age_days / half_life_days)


def _minutes_since(then: str | None, now: str) -> float | None:
    if not then:
        return None
    return max(0.0, (clock.parse(now) - clock.parse(then)).total_seconds() / 60)


def strength_now(mem: Memory, now: str, m: dict) -> float:
    """What a memory is worth now: how strong it went in, faded by the time since it was last touched (formed, met
    again or recalled) with the half-life of its layer, and held up by how often it has come back."""
    base = mem.strength or mem.importance
    half = m["short_half_life_days"] if mem.kind == "short" else m["long_half_life_days"]
    touched = max([t for t in (mem.created_at, mem.last_recalled) if t])
    held = 1 + m["recall_boost"] * min(mem.recall_count, 10)
    return min(1.0, base * recency(half, touched, now) * held)


def significance(importance: float, mood_base: float, remember: bool, freshness: float,
                 temperament: dict | None, m: dict) -> float:
    """How much an experience mattered to *this* creature: what the world says it was worth, how hard it landed
    emotionally (a volatile creature feels more), whether it was new, whether it asked to remember it."""
    from cat.inner import scale
    emotional = min(1.0, abs(mood_base) * m["emotion_scale"]) * scale(temperament, "volatility")
    s = max(importance, emotional) + (m["novelty_bonus"] if freshness >= 0.8 else 0.0) + (m["remember_bonus"] if remember else 0.0)
    return min(1.0, s)


def feeling_of(mood_base: float) -> str:
    return "good" if mood_base > 0.02 else "bad" if mood_base < -0.02 else "flat"


def add_memory(conn: sqlite3.Connection, m: dict, now: str, text: str, vec: list[float] | None, strength: float, *,
               feeling: str, action_id: str | None, location: str | None, source: int | None) -> str:
    """A new impression. The same thing met again strengthens the memory it already has (no duplicate); a new one must
    be worth more than the weakest short-term memory when that layer is full, or it never gets in. Returns what
    happened: reinforced | added | direct | dropped. Inside the caller's transaction."""
    v = None if vec is None else np.asarray(vec, dtype=np.float32)
    today = now[:10]
    if v is not None:
        for old in [x for k in ("short", "long") for x in all_memories(conn, k) if x.embedding is not None]:
            if cosine(old.embedding, v) >= m["dup_threshold"]:
                src = sorted(set(old.sources) | ({source} if source else set()))
                new_day = conn.execute("SELECT last_recall_day FROM memories WHERE id = ?", (old.id,)).fetchone()[0] != today
                conn.execute("UPDATE memories SET occurrences = occurrences + 1, strength = MIN(1.0, MAX(strength, importance, ?) "
                             "+ ?), recall_days = recall_days + ?, last_recall_day = ?, last_recalled = ?, sources = ? "
                             "WHERE id = ?", (strength, m["reinforce_gain"], int(new_day), today, now, json.dumps(src), old.id))
                return "reinforced"
    kind = "long" if strength >= m["long_direct"] else "short"
    if kind == "short":
        shorts = all_memories(conn, "short")
        if len(shorts) >= m["short_cap"]:
            weakest = min(shorts, key=lambda x: strength_now(x, now, m))
            if strength <= strength_now(weakest, now, m):
                return "dropped"
            conn.execute("DELETE FROM memories WHERE id = ?", (weakest.id,))
    conn.execute("INSERT INTO memories(kind, text, importance, strength, embedding, created_at, sources, feeling, action_id, "
                 "location, recall_days, last_recall_day) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                 (kind, text, strength, strength, to_blob(vec), now, json.dumps([source] if source else []), feeling,
                  action_id, location, today))
    return "direct" if kind == "long" else "added"


def faded_cue(conn: sqlite3.Connection, m: dict, query_vec: list[float] | None) -> list[str]:
    """A faded memory comes back only when something in front of the creature touches it (and it knows it is old)."""
    if query_vec is None:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    hits = [(cosine(x.embedding, q), x) for x in all_memories(conn, "faded") if x.embedding is not None]
    hits = [h for h in hits if h[0] >= m["faded_cue"]]
    hits.sort(key=lambda h: -h[0])
    return [x.gist or x.text for _, x in hits[:1]]


def feelings_by_action(conn: sqlite3.Connection, m: dict, now: str) -> dict[str, float]:
    """For each action it remembers: -1 (it went badly) .. 1 (it went well), weighted by how strong the memories are.
    Only what is still strong enough to matter is counted."""
    tally: dict[str, list[float]] = {}
    for x in all_memories(conn):
        if x.kind in ("short", "long") and x.action_id and x.feeling in ("good", "bad"):
            w = strength_now(x, now, m) * (1 + 0.5 * min(x.occurrences - 1, 4))
            if w >= m["memory_note_min"]:
                tally.setdefault(x.action_id, []).append(w if x.feeling == "good" else -w)
    return {a: max(-1.0, min(1.0, sum(v) / max(1.0, len(v)))) for a, v in tally.items()}


def gist_of(text: str) -> str:
    """One worn sentence: the first few words, the detail gone."""
    words = text.replace("\n", " ").split()
    return " ".join(words[:9]) + ("..." if len(words) > 9 else "")


def score(mem: Memory, query: np.ndarray | None, now: str, m: dict) -> dict:
    """Recency is how long ago the memory was *formed*: being recalled must not keep a memory fresh, or the same
    few memories fill every prompt and become everything the creature thinks about. A memory that was on its mind
    in the last `recall_rest_minutes` rests (its score is damped), so others get their turn."""
    rec = recency(m["recency_half_life_days"], mem.created_at, now)
    rel = cosine(mem.embedding, query) if query is not None else 0.0
    value = strength_now(mem, now, m) if mem.kind == "short" else mem.importance
    total = m["w_recency"] * rec + m["w_importance"] * value + m["w_relevance"] * rel
    since = _minutes_since(mem.last_recalled, now)
    resting = since is not None and since < m["recall_rest_minutes"]
    if resting:
        total *= m["recall_rest_factor"]
    return {"id": mem.id, "text": mem.text, "recency": rec, "importance": value, "relevance": rel,
            "resting": resting, "score": total}


def recall(conn: sqlite3.Connection, tuning: dict, query_vec: list[float] | None, now: str) -> list[dict]:
    """The best long-term memories for this moment (scored; core memories are part of identity, not recalled)."""
    m = tuning["memory"]
    if m["recall_k"] == 0:
        return []
    q = None if query_vec is None else np.asarray(query_vec, dtype=np.float32)
    items = all_memories(conn, "long") + all_memories(conn, "short")
    by_id = {item.id: item for item in items}
    scored = [score(x, q, now, m) for x in items]
    if q is not None and m["recall_min_relevance"] > 0:         # only what the moment actually touches
        scored = [s for s in scored if s["relevance"] >= m["recall_min_relevance"]]
    scored.sort(key=lambda s: -s["score"])
    selected: list[dict] = []
    selected_items: list[Memory] = []
    threshold = m["dup_threshold"]
    for candidate in scored:
        item = by_id[candidate["id"]]
        if any(" ".join(item.text.casefold().split()) == " ".join(old.text.casefold().split()) or
               (item.embedding is not None and old.embedding is not None and
                cosine(item.embedding, old.embedding) >= threshold) for old in selected_items):
            continue
        selected.append(candidate)
        selected_items.append(item)
        if len(selected) >= m["recall_k"] - m["wander_slots"]:
            break
    # a mind wanders: the remaining slots go to important memories it has not thought about for a long while
    chosen = {s["id"] for s in selected}
    idle = []
    for item in items:
        if item.id in chosen:
            continue
        since = _minutes_since(item.last_recalled, now)
        idle_min = since if since is not None else _minutes_since(item.created_at, now) or 0.0
        if idle_min >= m["wander_after_minutes"]:
            idle.append((item.importance * min(1.0, idle_min / (4 * m["wander_after_minutes"])), item))
    idle.sort(key=lambda x: -x[0])
    for weight, item in idle[: max(0, m["recall_k"] - len(selected))]:
        selected.append({"id": item.id, "text": item.text, "recency": 0.0, "importance": item.importance,
                         "relevance": 0.0, "resting": False, "wandered": True, "score": weight})
    return selected


def mark_recalled(conn: sqlite3.Connection, ids: list[int], now: str, gain: float = 0.0) -> None:
    conn.executemany("UPDATE memories SET recall_count = recall_count + 1, last_recalled = ?, "
                     "recall_days = recall_days + CASE WHEN last_recall_day IS NULL OR last_recall_day != ? THEN 1 ELSE 0 END, "
                     "last_recall_day = ?, strength = MIN(1.0, MAX(strength, importance) + ?) WHERE id = ?",
                     [(now, now[:10], now[:10], gain, i) for i in ids])


def query_text(location_name: str, activity: dict | None, perception: list[str], letters: list[dict]) -> str:
    bits = [location_name]
    if activity:
        bits.append(activity["name"])
    bits += perception[-3:]
    bits += [l["text"] for l in letters[:2]]
    return " | ".join(b for b in bits if b)


def retention(mem: Memory, now: str, m: dict) -> float:
    rec = recency(m["recency_half_life_days"], mem.last_recalled or mem.created_at, now)
    return mem.importance * rec * (1 + mem.recall_count * m["recall_boost"])


# ---------------------------------------------------------------- preferences (§2.4)

@dataclass
class Preference:
    subject_kind: str
    subject_id: str
    valence: float
    confidence: float
    words: str | None
    evidence: list[int]
    updated_at: str

    @property
    def strength(self) -> float:
        return self.valence * self.confidence


def preferences(conn: sqlite3.Connection) -> dict[tuple[str, str], Preference]:
    return {(r["subject_kind"], r["subject_id"]): Preference(r["subject_kind"], r["subject_id"], r["valence"],
                                                           r["confidence"], r["words"], json.loads(r["evidence"]),
                                                           r["updated_at"])
            for r in conn.execute("SELECT * FROM preferences")}


def subjects_of(action: Any, location: str) -> list[tuple[str, str]]:
    return [("action", action.id), ("family", action.family), ("location", location)]


def mood_factor(prefs: dict, action: Any, location: str, weight: float) -> float:
    """Mood effects of an action touching liked/disliked subjects are multiplied by this."""
    f = 1.0
    for key in subjects_of(action, location):
        p = prefs.get(key)
        if p:
            f *= 1 + weight * p.strength
    return max(0.0, f)


def note_for(prefs: dict, action: Any, min_strength: float) -> int:
    """+1 liked, -1 disliked, 0 neither: the strongest of the action's own and its family's preference."""
    best = 0.0
    for key in (("action", action.id), ("family", action.family)):
        p = prefs.get(key)
        if p and abs(p.strength) > abs(best):
            best = p.strength
    return 0 if abs(best) < min_strength else (1 if best > 0 else -1)

"""Memory: short-term (today's notable moments), long-term (consolidated at sleep) and recall.

Memories are stored as text; the embedding (float32 blob) is only a search index. Embedding HTTP
calls are made *before* the tick transaction (`prepare`), the inserts inside it (`save`)."""
from __future__ import annotations

import logging
from sqlite3 import Connection
from typing import Any

import numpy as np

from cat import character, clock
from cat.config import Config
from cat.llm import BackendError, LLMBackend

log = logging.getLogger("cat.memory")


def to_blob(vec: list[float] | None) -> bytes | None:
    return None if vec is None else np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(b: bytes | None) -> np.ndarray | None:
    return None if not b else np.frombuffer(b, dtype=np.float32)


def notable_reasons(cfg: Config, before: dict[str, float], after: dict[str, float],
                    feeling: str | None, applied: list[dict], rejected: list[dict]) -> list[str]:
    """Why (if at all) this tick is worth remembering. Empty list = not notable."""
    n = cfg.settings["notable"]
    why: list[str] = []
    for k in after:
        if abs(after[k] - before.get(k, after[k])) >= n["need_change"]:
            why.append(f"big change in {character.need_label(k, cfg)}")
            break
    if feeling in n["strong_feelings"]:
        why.append(f"felt {feeling}")
    if n["new_object"] and any(c.get("op") == "create" for c in applied):
        why.append("something new appeared")
    if n["rejected_changes"] and rejected:
        why.append("odd outcome")
    return why


def importance_for(cfg: Config, n_reasons: int) -> float:
    n = cfg.settings["notable"]
    return min(1.0, n["importance_base"] + n["importance_per_trigger"] * n_reasons)


def build_query(situation_bits: dict[str, Any]) -> str:
    """Short description of the current situation, embedded to find relevant memories."""
    parts = [f"In {situation_bits['area']}.", "Here: " + (", ".join(situation_bits["objects"]) or "nothing") + ".",
             f"Feeling {situation_bits['mood']}."]
    if situation_bits.get("last_outcome"):
        parts.append(situation_bits["last_outcome"])
    return " ".join(parts)


class Memory:
    def __init__(self, conn: Connection, cfg: Config, backend: LLMBackend):
        self.conn, self.cfg, self.backend = conn, cfg, backend
        self._ids: list[int] = []
        self._matrix: np.ndarray | None = None
        self._dirty = True

    # ---------- writing ----------
    def prepare(self, text: str, importance: float, sim_time: str) -> dict:
        """Embed the text (HTTP, outside any transaction). If embedding fails the memory is still
        kept, just without a search index."""
        vec = self.embed_one(text)
        return {"text": text, "importance": importance, "created_sim_time": sim_time,
                "embedding": to_blob(vec),
                "embed_model": self.cfg.settings["embed_model"] if vec else None}

    def embed_one(self, text: str) -> list[float] | None:
        try:
            return self.backend.embed(self.cfg.settings["embed_model"], [text])[0]
        except BackendError as e:
            log.warning("embedding failed, storing without index: %s", e)
            return None

    def save(self, mem: dict, kind: str) -> int:
        """Insert a prepared memory. Call inside the caller's transaction."""
        cur = self.conn.execute(
            "INSERT INTO memories(kind,text,importance,created_sim_time,last_recalled,embedding,"
            "embed_model) VALUES(?,?,?,?,?,?,?)",
            (kind, mem["text"], mem["importance"], mem["created_sim_time"], None,
             mem["embedding"], mem["embed_model"]))
        if kind == "long":
            self._dirty = True
        return cur.lastrowid

    def mark_recalled(self, ids: list[int], sim_time: str) -> None:
        """Feeds recency, so memories that keep coming up stay 'fresh'. Inside the tick txn."""
        self.conn.executemany("UPDATE memories SET last_recalled=? WHERE id=?",
                              [(sim_time, i) for i in ids])

    def clear_short(self) -> None:
        self.conn.execute("DELETE FROM memories WHERE kind='short'")

    def prune_long(self) -> int:
        """Keep at most `long_term_cap` long-term memories, dropping the least valuable
        (importance x recency). Returns how many were removed. Inside a transaction."""
        cap = self.cfg.settings["long_term_cap"]
        rows = self.conn.execute(
            "SELECT id, importance, created_sim_time, last_recalled FROM memories "
            "WHERE kind='long'").fetchall()
        if len(rows) <= cap:
            return 0
        latest = max(clock.parse(r["last_recalled"] or r["created_sim_time"]) for r in rows)
        scored = sorted(rows, key=lambda r: r["importance"] * self._recency(
            r["last_recalled"] or r["created_sim_time"], clock.fmt(latest)))
        drop = [r["id"] for r in scored[:len(rows) - cap]]
        self.conn.executemany("DELETE FROM memories WHERE id=?", [(i,) for i in drop])
        self._dirty = True
        return len(drop)

    # ---------- reading ----------
    def recent_short(self, n: int | None = None) -> list[dict]:
        n = n or self.cfg.settings["short_term_shown"]
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE kind='short' ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows][::-1]

    def all_short(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM memories WHERE kind='short' ORDER BY id")]

    def reload(self) -> None:
        """(Re)load every usable long-term embedding into one normalised numpy matrix."""
        rows = self.conn.execute(
            "SELECT id, embedding, embed_model FROM memories WHERE kind='long' "
            "AND embedding IS NOT NULL").fetchall()
        model = self.cfg.settings["embed_model"]
        vecs, ids = [], []
        for r in rows:
            v = from_blob(r["embedding"])
            if r["embed_model"] == model and v is not None and v.size:
                vecs.append(v)
                ids.append(r["id"])
        dims = {v.size for v in vecs}
        if len(dims) > 1:  # mixed dimensions: keep the most common, ignore the rest
            keep = max(dims, key=lambda d: sum(v.size == d for v in vecs))
            pairs = [(i, v) for i, v in zip(ids, vecs) if v.size == keep]
            ids, vecs = [p[0] for p in pairs], [p[1] for p in pairs]
        if vecs:
            m = np.vstack(vecs).astype(np.float32)
            norms = np.linalg.norm(m, axis=1, keepdims=True)
            self._matrix = m / np.where(norms == 0, 1, norms)
        else:
            self._matrix = None
        self._ids = ids
        self._dirty = False

    def _recency(self, ref_sim_time: str, now_sim_time: str) -> float:
        hours = max(0.0, (clock.parse(now_sim_time) - clock.parse(ref_sim_time)).total_seconds() / 3600)
        return 0.5 ** (hours / self.cfg.settings["recency_half_life_sim_hours"])

    def recall(self, query_vec: list[float] | None, now_sim_time: str,
               k: int | None = None) -> list[dict]:
        """Top-k long-term memories by
        w_recency*recency + w_importance*importance + w_relevance*cosine(query, memory).
        Without a query vector (embedding down) relevance is 0, so it falls back to
        recency + importance."""
        rc = self.cfg.settings["recall"]
        k = k or rc["k"]
        rows = self.conn.execute("SELECT * FROM memories WHERE kind='long'").fetchall()
        if not rows:
            return []
        if self._dirty:
            self.reload()
        rel: dict[int, float] = {}
        if query_vec is not None and self._matrix is not None:
            q = np.asarray(query_vec, dtype=np.float32)
            if q.size == self._matrix.shape[1]:
                n = float(np.linalg.norm(q))
                if n:
                    sims = self._matrix @ (q / n)
                    rel = {i: max(0.0, float(s)) for i, s in zip(self._ids, sims)}
        scored = []
        for r in rows:
            ref = r["last_recalled"] or r["created_sim_time"]
            if clock.parse(r["created_sim_time"]) > clock.parse(ref):
                ref = r["created_sim_time"]
            score = (rc["w_recency"] * self._recency(ref, now_sim_time)
                     + rc["w_importance"] * float(r["importance"])
                     + rc["w_relevance"] * rel.get(r["id"], 0.0))
            scored.append((score, r["id"], dict(r)))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        out = []
        for score, _, d in scored[:k]:
            d.pop("embedding", None)
            d["score"] = score
            out.append(d)
        return out


def reembed_all(conn: Connection, backend: LLMBackend, model: str, batch: int = 16) -> int:
    """One-off: re-embed every memory with `model` (after changing embed_model)."""
    rows = conn.execute("SELECT id, text FROM memories").fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        vecs = backend.embed(model, [r["text"] for r in chunk])
        conn.execute("BEGIN IMMEDIATE")
        try:
            for r, v in zip(chunk, vecs):
                conn.execute("UPDATE memories SET embedding=?, embed_model=? WHERE id=?",
                             (to_blob(v), model, r["id"]))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        done += len(chunk)
    return done

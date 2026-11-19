"""LLM backends. The engine only talks to the `LLMBackend` interface.

OllamaBackend arrives in M4; MockBackend lets everything run and be tested offline.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
from abc import ABC, abstractmethod
from typing import Any, Callable

import numpy as np

EMBED_DIM = 64


class BackendError(Exception):
    """The backend is unreachable, timed out, or returned unusable output."""


class LLMBackend(ABC):
    @abstractmethod
    def is_online(self) -> bool:
        """Cheap health check (Ollama: GET /api/tags, ~3s timeout). Never raises."""

    @abstractmethod
    def chat_json(self, task: str, model: str, messages: list[dict], schema: dict) -> dict:
        """Return a dict matching `schema`. `task` is one of brain|narrator|consolidate|
        tendencies|preferences. Raises BackendError on failure."""

    @abstractmethod
    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """Raises BackendError on failure."""


def hash_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic bag-of-words embedding: similar wording => higher cosine similarity."""
    v = np.zeros(dim, dtype=np.float32)
    for w in re.findall(r"[a-z']+", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 8) & 1 else -1.0
    n = float(np.linalg.norm(v))
    if n == 0:
        v[0] = 1.0
        n = 1.0
    return (v / n).tolist()


class MockBackend(LLMBackend):
    """Deterministic, seedable stand-in for Ollama.

    - `online` can be flipped to simulate the laptop going away; calls then raise BackendError.
    - `handlers[task]` may be a callable(messages, schema) -> dict to script exact behaviour.
    - `fail_next` makes the next N chat/embed calls raise (to test retry/doze).
    Default outputs are generic but schema-valid; later milestones extend them.
    """

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)
        self.online = True
        self.fail_next = 0
        self.handlers: dict[str, Callable[[list[dict], dict], dict]] = {}
        self.calls: list[tuple[str, str]] = []  # (task, model)
        self.need_ids: list[str] = []  # set by the engine so mock gains name real needs

    def is_online(self) -> bool:
        return self.online

    def _check(self) -> None:
        if not self.online:
            raise BackendError("mock backend offline")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise BackendError("mock backend injected failure")

    def chat_json(self, task: str, model: str, messages: list[dict], schema: dict) -> dict:
        self._check()
        self.calls.append((task, model))
        if task in self.handlers:
            return self.handlers[task](messages, schema)
        return self._default(task)

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        self._check()
        self.calls.append(("embed", model))
        return [hash_embed(t) for t in texts]

    def _default(self, task: str) -> dict[str, Any]:
        r = self.rng
        if task == "brain":
            act = r.choice(["looks out of the window", "bats at the red ball", "naps on the sofa",
                            "sniffs around", "washes a paw"])
            return {"reaction": "hm, ok.", "thought": "what next?", "action": act,
                    "intent": "feeling idle", "feeling": r.choice(["negative", "neutral", "positive"]),
                    "wants_sleep": False}
        if task == "narrator":
            gains = {n: r.choice(["0", "+", "+", "++"]) for n in r.sample(
                self.need_ids, min(2, len(self.need_ids)))}
            return {"outcome": "Nothing much happens.",
                    "gains": gains, "effort": r.choice(["low", "medium"]),
                    "duration": r.choice([10, 20, 30]), "changes": []}
        if task == "consolidate":
            return {"memories": [{"text": "A quiet day at home.", "importance": 0.4}],
                    "summary": "An uneventful day."}
        if task == "tendencies":
            return {"changes": {}}
        if task == "preferences":
            return {"preferences": []}
        raise BackendError(f"mock: unknown task '{task}'")


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0

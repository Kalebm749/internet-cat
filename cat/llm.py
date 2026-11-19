"""LLM backends. The engine only talks to the `LLMBackend` interface.

OllamaBackend arrives in M4; MockBackend lets everything run and be tested offline.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
from abc import ABC, abstractmethod
from typing import Any, Callable

import httpx
import numpy as np

log = logging.getLogger("cat.llm")

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


def call_json(backend: LLMBackend, task: str, model: str, messages: list[dict], schema: dict,
              parse: Callable[[dict], Any], retries: int) -> Any:
    """chat_json + parse, retried `retries` times on any BackendError (timeout, bad JSON,
    unusable content). After the last failure raises BackendError => the engine dozes."""
    last: BackendError | None = None
    for attempt in range(retries + 1):
        try:
            return parse(backend.chat_json(task, model, messages, schema))
        except BackendError as e:
            last = e
            log.warning("%s call failed (attempt %d/%d): %s", task, attempt + 1, retries + 1, e)
    assert last is not None
    raise last


def extract_json(text: str) -> dict:
    """Parse model output as a JSON object. Tolerates <think> blocks and ``` fences."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
    except json.JSONDecodeError as e:
        raise BackendError(f"invalid JSON from model: {e}") from None
    if not isinstance(v, dict):
        raise BackendError("model JSON is not an object")
    return v


class OllamaBackend(LLMBackend):
    """Talks to Ollama over HTTP: /api/chat (JSON-schema `format`), /api/embed, /api/tags."""

    def __init__(self, cfg, client: httpx.Client | None = None):
        s = cfg.settings
        self.s = s
        self.base = s["ollama_host"].rstrip("/")
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(s["llm_timeout_seconds"], connect=s["health_timeout_seconds"]))

    def is_online(self) -> bool:
        try:
            r = self.client.get(f"{self.base}/api/tags", timeout=self.s["health_timeout_seconds"])
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def _post(self, path: str, payload: dict) -> dict:
        try:
            r = self.client.post(f"{self.base}{path}", json=payload)
            r.raise_for_status()
            return r.json()
        except httpx.TimeoutException as e:
            raise BackendError(f"timeout calling {path}") from e
        except httpx.HTTPStatusError as e:
            raise BackendError(f"{path} returned HTTP {e.response.status_code}: "
                               f"{e.response.text[:200]}") from e
        except (httpx.HTTPError, ValueError) as e:
            raise BackendError(f"{path} failed: {e}") from e

    def chat_json(self, task: str, model: str, messages: list[dict], schema: dict) -> dict:
        payload: dict[str, Any] = {
            "model": model, "messages": messages, "stream": False, "format": schema,
            "options": {"temperature": self.s["llm_temperature"].get(
                "sleep" if task in ("consolidate", "tendencies", "preferences") else task, 0.7),
                "num_ctx": self.s["llm_num_ctx"]}}
        if "ollama_think" in self.s:
            payload["think"] = bool(self.s["ollama_think"])
        data = self._post("/api/chat", payload)
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise BackendError("no message content in /api/chat response")
        return extract_json(content)

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        data = self._post("/api/embed", {"model": model, "input": texts})
        vecs = data.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts) or not all(vecs):
            raise BackendError("unexpected /api/embed response")
        return vecs


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
        self.tendency_names: list[str] = []  # set by the engine so mock sleep output names real tendencies
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
        return self._default(task, messages)

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        self._check()
        self.calls.append(("embed", model))
        return [hash_embed(t) for t in texts]

    def _mock_changes(self, messages: list[dict]) -> list[dict]:
        """Random, mostly-valid world changes (plus some deliberately bad ones) so the effects
        validator gets exercised by mock runs."""
        r = self.rng
        ctx = _parse_narrator_context(messages)
        if "area" not in ctx or r.random() > 0.6:
            return []
        movable = [o for o in ctx["here"] if "movable" in o["tags"]]
        edible = [o for o in ctx["here"] + ctx["inv"] if "edible" in o["tags"]]
        choices: list[dict] = []
        if ctx["exits"]:
            choices.append({"op": "move_self", "area": r.choice(ctx["exits"])})
        if movable:
            choices.append({"op": "take", "object_id": r.choice(movable)["id"]})
        if ctx["inv"]:
            choices.append({"op": "drop", "object_id": r.choice(ctx["inv"])["id"]})
        if edible:
            choices.append({"op": "consume", "object_id": r.choice(edible)["id"]})
        if ctx["here"]:
            choices.append({"op": "set_state", "object_id": r.choice(ctx["here"])["id"],
                            "key": "touched", "value": "true"})
        choices.append({"op": "create", "name": r.choice(["a cushion fort", "a paper boat"]),
                        "tags": ["movable", "soft", "bogus_tag"]})
        out = [r.choice(choices)]
        if r.random() < 0.2:  # bad ones: nonexistent object / non-adjacent area / unknown op
            out.append(r.choice([{"op": "consume", "object_id": "ghost_fish"},
                                 {"op": "move_self", "area": "the_moon"},
                                 {"op": "teleport", "area": "garden"}]))
        return out

    def _default(self, task: str, messages: list[dict]) -> dict[str, Any]:
        r = self.rng
        if task == "brain":
            act = r.choice(["looks out of the window", "bats at the red ball", "naps on the sofa",
                            "sniffs around", "washes a paw"])
            tired = any(w in (messages[-1]["content"] if messages else "")
                        for w in ("Energy: tired", "Energy: exhausted"))
            return {"reaction": "hm, ok.", "thought": "what next?", "action": act,
                    "intent": "feeling idle", "feeling": r.choice(["negative", "neutral", "positive"]),
                    "wants_sleep": tired and r.random() < 0.5}
        if task == "narrator":
            gains = {n: r.choice(["0", "+", "+", "++"]) for n in r.sample(
                self.need_ids, min(2, len(self.need_ids)))}
            return {"outcome": "Nothing much happens.",
                    "gains": gains, "effort": r.choice(["low", "medium"]),
                    "duration": r.choice([10, 20, 30]),
                    "changes": self._mock_changes(messages)}
        if task == "consolidate":
            first = re.search(r"^- (.+)$", messages[-1]["content"] if messages else "", re.M)
            text = first.group(1)[:120] if first else "A quiet day at home."
            return {"memories": [{"text": text, "importance": r.choice(["low", "medium", "high"])}],
                    "summary": "A day like any other."}
        if task == "tendencies":
            # deliberately over-eager (+-1) so the engine's clamp is exercised
            return {"changes": {t: r.choice([-1, 0, 1, 5]) for t in self.tendency_names}}
        if task == "preferences":
            if r.random() < 0.5:
                return {"preferences": []}
            return {"preferences": [{"subject": r.choice(["the old tree", "the sofa", "rain"]),
                                     "valence": r.choice([-0.8, 0.6, 0.9]), "reason": "mock"}]}
        raise BackendError(f"mock: unknown task '{task}'")


def _parse_narrator_context(messages: list[dict]) -> dict:
    """Read the engine's narrator context (see Engine._narrator_context)."""
    text = messages[-1]["content"] if messages else ""
    ctx: dict[str, Any] = {"exits": [], "here": [], "inv": []}
    section = None
    for line in text.splitlines():
        if line.startswith("Location:"):
            ctx["area"] = line.split()[1]
        elif line.startswith("Exits:"):
            ctx["exits"] = [e.split(" (")[0].strip() for e in line[6:].split(";") if e.strip()]
        elif line.startswith("Objects here"):
            section = "here"
        elif line.startswith("Inventory"):
            section = "inv"
        elif line.startswith("- ") and section:
            m = re.match(r"- (\S+): .*\| tags: ([^|]*)\|", line)
            if m:
                tags = [t.strip() for t in m.group(2).split(",") if t.strip() != "none"]
                ctx[section].append({"id": m.group(1), "tags": tags})
    return ctx


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0

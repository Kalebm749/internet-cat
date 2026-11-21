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
import time
import uuid
from datetime import datetime
from abc import ABC, abstractmethod
from typing import Any, Callable

import httpx
import numpy as np

log = logging.getLogger("cat.llm")

EMBED_DIM = 64


class BackendError(Exception):
    """The backend is unreachable, timed out, or returned unusable output."""


def split_messages(messages: list[dict]) -> tuple[str, str]:
    """(system prompt, user prompt) exactly as sent."""
    sysm = "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    user = "\n\n".join(m.get("content", "") for m in messages if m.get("role") != "system")
    return sysm, user


def ollama_metrics(data: dict) -> dict:
    """Token counts and durations (ns -> ms) from an Ollama /api/chat response."""
    ms = lambda k: int((data.get(k) or 0) // 1_000_000)
    return {"prompt_tokens": data.get("prompt_eval_count"), "completion_tokens": data.get("eval_count"),
            "load_ms": ms("load_duration"), "prompt_eval_ms": ms("prompt_eval_duration"),
            "eval_ms": ms("eval_duration"), "total_ms": ms("total_duration")}


class LLMBackend(ABC):
    # Tracing hooks. `last_call` describes the most recent chat_json attempt (exact prompts, raw
    # response, timings...); `recorder` is called by call_json after every attempt. Both optional.
    last_call: dict | None = None
    recorder: Callable[..., None] | None = None

    @abstractmethod
    def is_online(self) -> bool:
        """Cheap health check (Ollama: GET /api/tags, ~3s timeout). Never raises."""

    @abstractmethod
    def chat_json(self, task: str, model: str, messages: list[dict], schema: dict) -> dict:
        """Return a dict matching `schema`. `task` is decide, sleep_<stage> or a capability's name.
        Raises BackendError on failure."""

    @abstractmethod
    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """Raises BackendError on failure."""


def _record(backend: LLMBackend, task: str, model: str, attempt: int, parsed: Any,
            error: str | None) -> None:
    """Hand one attempt to the tracer. Tracing must never break the creature."""
    rec = getattr(backend, "recorder", None)
    if rec is None:
        return
    try:
        rec(task, model, attempt, dict(backend.last_call or {}), parsed, error)
    except Exception:
        log.warning("trace recorder failed", exc_info=True)


def call_json(backend: LLMBackend, task: str, model: str, messages: list[dict], schema: dict,
              parse: Callable[[dict], Any], retries: int) -> Any:
    """chat_json + parse, retried `retries` times on any BackendError (timeout, bad JSON,
    unusable content). After the last failure raises BackendError => the engine dozes.
    Every attempt is reported to `backend.recorder` (if set)."""
    last: BackendError | None = None
    for attempt in range(1, retries + 2):
        backend.last_call = None
        try:
            raw = backend.chat_json(task, model, messages, schema)
        except BackendError as e:
            _record(backend, task, model, attempt, None, str(e))
            last = e
            log.warning("%s call failed (attempt %d/%d): %s", task, attempt, retries + 1, e)
            continue
        try:
            result = parse(raw)
        except BackendError as e:
            _record(backend, task, model, attempt, raw, str(e))
            last = e
            log.warning("%s call failed (attempt %d/%d): %s", task, attempt, retries + 1, e)
            continue
        _record(backend, task, model, attempt, raw, None)
        return result
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


def wanted_models(settings: dict) -> list[str]:
    """The models the engine uses (settings `nit.models`), each once."""
    out: list[str] = []
    for m in (settings.get("nit") or {}).get("models", {}).values():
        if m not in out:
            out.append(m)
    return out


def model_present(wanted: str, have: list[str]) -> bool:
    """Ollama lists 'name:tag'; a bare name means ':latest'."""
    norm = lambda m: m if ":" in m else m + ":latest"
    return norm(wanted) in {norm(h) for h in have}


class OllamaBackend(LLMBackend):
    """Talks to Ollama over HTTP: /api/chat (JSON-schema `format`), /api/embed, /api/tags."""

    def __init__(self, cfg, client: httpx.Client | None = None):
        s = cfg.settings
        self.s = s
        self._warned_missing: list[str] = []
        self.call_observer: Callable[[dict], None] | None = None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(s["llm_timeout_seconds"], connect=s["health_timeout_seconds"]))

    @property
    def base(self) -> str:
        return self.s["ollama_host"].rstrip("/")      # read live: settings can be reloaded

    def missing_models(self) -> list[str] | None:
        """Configured models that are not pulled yet; None if Ollama is unreachable."""
        try:
            r = self.client.get(f"{self.base}/api/tags", timeout=self.s["health_timeout_seconds"])
            if r.status_code != 200:
                return None
            have = [m.get("name", "") for m in r.json().get("models", [])]
        except (httpx.HTTPError, ValueError):
            return None
        wanted = set(wanted_models(self.s))
        return sorted(m for m in wanted if not model_present(m, have))

    def is_online(self) -> bool:
        """Reachable and every configured model is pulled. A fresh
        install before the model pull is therefore a clean doze, not a stream of failed calls."""
        missing = self.missing_models()
        if missing is None:
            return False
        if missing:
            if missing != self._warned_missing:
                log.warning("Ollama is up but models are missing: %s (run deploy/pull-models.sh)", missing)
                self._warned_missing = missing
            return False
        self._warned_missing = []
        return True

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

    def _notify_call(self, event: dict) -> None:
        if self.call_observer is None:
            return
        try:
            self.call_observer(event)
        except Exception:
            log.warning("LLM call observer failed", exc_info=True)

    @staticmethod
    def _call_event(call_id: str, operation: str, task: str, model: str, input_chars: int,
                    started_at: str, started_ts: float, status: str, *, info: dict | None = None,
                    error: str | None = None) -> dict:
        info = info or {}
        return {"id": call_id, "operation": operation, "task": task, "model": model, "status": status,
                "started_at": started_at, "started_ts": started_ts,
                "input_tokens_estimate": (input_chars + 3) // 4,
                "input_tokens": info.get("prompt_tokens"), "output_tokens": info.get("completion_tokens"),
                "duration_ms": info.get("total_ms"), "error": error}

    def chat_json(self, task: str, model: str, messages: list[dict], schema: dict) -> dict:
        options = {"temperature": self.s["llm_temperature"].get("sleep" if task.startswith("sleep_") else task, 0.7),
            "num_ctx": self.s["llm_num_ctx"]}
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False,
                                   "format": schema, "options": options}
        if "ollama_think" in self.s:
            payload["think"] = bool(self.s["ollama_think"])
        sysm, user = split_messages(messages)
        info: dict[str, Any] = {
            "started_at": datetime.now().isoformat(timespec="milliseconds"), "task": task,
            "model": model, "options": {**options, **({"think": payload["think"]} if "think" in payload else {})},
            "system_prompt": sysm, "user_prompt": user, "schema": schema, "raw_response": None,
            "valid_json": 0, "error": None}
        self.last_call = info
        call_id, started_ts = uuid.uuid4().hex, time.time()
        self._notify_call(self._call_event(call_id, "chat", task, model, len(sysm) + len(user),
                                           info["started_at"], started_ts, "running"))
        t0 = time.perf_counter()
        try:
            data = self._post("/api/chat", payload)
            info.update(ollama_metrics(data))
            content = (data.get("message") or {}).get("content")
            if not isinstance(content, str):
                raise BackendError("no message content in /api/chat response")
            info["raw_response"] = content
            out = extract_json(content)
            info["valid_json"] = 1
            return out
        except BackendError as e:
            info["error"] = str(e)
            raise
        finally:
            if not info.get("total_ms"):
                info["total_ms"] = int((time.perf_counter() - t0) * 1000)
            self._notify_call(self._call_event(call_id, "chat", task, model, len(sysm) + len(user),
                                               info["started_at"], started_ts,
                                               "failed" if info.get("error") else "completed", info=info,
                                               error=info.get("error")))

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        started_at = datetime.now().isoformat(timespec="milliseconds")
        started_ts = time.time()
        call_id = uuid.uuid4().hex
        chars = sum(map(len, texts))
        info: dict[str, Any] = {}
        self._notify_call(self._call_event(call_id, "embedding", "embed", model, chars,
                                           started_at, started_ts, "running"))
        t0 = time.perf_counter()
        error = None
        try:
            data = self._post("/api/embed", {"model": model, "input": texts})
            info.update(ollama_metrics(data))
            vecs = data.get("embeddings")
            if not isinstance(vecs, list) or len(vecs) != len(texts) or not all(vecs):
                raise BackendError("unexpected /api/embed response")
            return vecs
        except BackendError as e:
            error = str(e)
            raise
        finally:
            info.setdefault("total_ms", int((time.perf_counter() - t0) * 1000))
            self._notify_call(self._call_event(call_id, "embedding", "embed", model, chars,
                                               started_at, started_ts, "failed" if error else "completed",
                                               info=info, error=error))


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

    - `online` can be flipped to simulate Ollama going away; calls then raise BackendError.
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

    def is_online(self) -> bool:
        return self.online

    def _check(self) -> None:
        if not self.online:
            raise BackendError("mock backend offline")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise BackendError("mock backend injected failure")

    def chat_json(self, task: str, model: str, messages: list[dict], schema: dict) -> dict:
        sysm, user = split_messages(messages)
        info: dict[str, Any] = {
            "started_at": datetime.now().isoformat(timespec="milliseconds"), "task": task,
            "model": model, "options": {"mock": True}, "system_prompt": sysm, "user_prompt": user,
            "schema": schema, "raw_response": None, "valid_json": 0, "error": None}
        self.last_call = info
        t0 = time.perf_counter()
        try:
            self._check()
            self.calls.append((task, model))
            out = self.handlers[task](messages, schema) if task in self.handlers else self._default(task, messages, schema)
            info["raw_response"] = json.dumps(out)
            info["valid_json"] = 1
            # rough token counts (4 chars/token) so the dashboard has something to chart
            info["prompt_tokens"] = (len(sysm) + len(user)) // 4
            info["completion_tokens"] = len(info["raw_response"]) // 4
            return out
        except BackendError as e:
            info["error"] = str(e)
            raise
        finally:
            info["total_ms"] = int((time.perf_counter() - t0) * 1000)
            info.setdefault("load_ms", 0)

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        self._check()
        self.calls.append(("embed", model))
        return [hash_embed(t) for t in texts]

    def _default(self, task: str, messages: list[dict], schema: dict | None = None) -> dict[str, Any]:
        r = self.rng
        if task.startswith("sleep_"):      # the rewrite's sleep cycle (cat/sleepcycle.py): cite what the prompt shows
            text = messages[-1]["content"] if messages else ""
            ids = [int(i) for i in re.findall(r"\[e(\d+)\]", text)]
            if task == "sleep_consolidate":
                return {"summary": "I went about my day and noticed what I could.",
                        "mattered": [{"id": i, "why": "it stayed with me"} for i in ids[:3]]}
            if task == "sleep_core":
                return {"keep": [{"id": i, "text": ""} for i in ids[:1]]}
            if task == "sleep_long_term":
                return {"memories": [{"text": f"I remember this moment ({i}).", "importance": 0.6, "sources": [i]}
                                     for i in ids[:2]]}
            if task == "sleep_learned":
                return {"words": [{"action": a, "words": f"I can {a.replace('_', ' ')}."}
                                  for a in re.findall(r"\[a:([a-z0-9_]+)\]", text)]}
            if task == "sleep_preferences":
                return {"verdicts": [{"subject": s, "keep": True, "words": ""}
                                     for s in re.findall(r"\[p:([a-z_]+:[a-z0-9_]+)\]", text)]}
            if task == "sleep_develop":
                return {"change": False, "kind": "background_note", "target": "", "value": "", "rationale": "",
                        "evidence": []}
            if task == "sleep_conflict":
                return {"conflict": False, "why": "it fits"}
        if task == "decide":       # the rewrite's waking decision (cat/decision.py): pick from the schema
            props = (schema or {}).get("properties", {})
            ids = props.get("action", {}).get("enum") or ["idle"]
            action = "continue" if "continue" in ids and r.random() < 0.7 else r.choice(ids)
            inputs = {}
            for name, spec in props.get("inputs", {}).get("properties", {}).items():
                if "enum" in spec:
                    inputs[name] = r.choice(spec["enum"])
                elif spec.get("type") == "integer":
                    inputs[name] = spec.get("minimum", 0)
                else:
                    inputs[name] = "something small and true"
            thought = r.choice(["The water keeps moving, even with no wind.", "I wonder who left the coins here.",
                                "Something about this place feels familiar.", "I am not sure what I am looking for.",
                                "The light has changed since this morning.", "I want to know what comes next."])
            return {"thought": thought, "reasoning": "I want to see what happens.",
                    "action": action, "inputs": inputs, "preserve": False, "letter": None}
        raise BackendError(f"mock: unknown task '{task}'")


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0

"""Narrator call: resolves an action into an outcome. M2: minimal prompt; M4 improves it.
`changes` is passed through unvalidated here; effects.py (M3) validates every change."""
from __future__ import annotations

from typing import Any

from cat.config import Config
from cat.llm import BackendError, LLMBackend

EFFORTS = ["low", "medium", "high"]
OPS = ["move_self", "move_object", "take", "drop", "consume", "set_state", "create"]


def schema(cfg: Config) -> dict:
    buckets = sorted(cfg.settings["gain_values"])
    return {
        "type": "object",
        "properties": {
            "outcome": {"type": "string"},
            "gains": {"type": "object",
                      "properties": {n: {"type": "string", "enum": buckets}
                                     for n in cfg.character["needs"]}},
            "effort": {"type": "string", "enum": EFFORTS},
            "duration": {"type": "integer"},
            "changes": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": OPS},
                    "object_id": {"type": "string"},
                    "area": {"type": "string"},
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "name": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["op"],
            }},
        },
        "required": ["outcome", "gains", "effort", "duration", "changes"],
    }


def build_messages(cfg: Config, action: str, context: str) -> list[dict]:
    system = ("You are a neutral narrator for a simulated world. Say what actually happens when "
              "the creature does the action. Reply with JSON only.")
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"{context}\n\nAction: {action}"}]


def parse(raw: dict, cfg: Config) -> dict[str, Any]:
    s = cfg.settings
    outcome = str(raw.get("outcome") or "").strip()
    if not outcome:
        raise BackendError("narrator returned no outcome")
    gains = {k: str(v) for k, v in (raw.get("gains") or {}).items()
             if k in cfg.character["needs"] and str(v) in s["gain_values"]}
    effort = raw.get("effort") if raw.get("effort") in EFFORTS else "medium"
    try:
        duration = int(raw.get("duration"))
    except (TypeError, ValueError):
        duration = s["sim_minutes_per_tick_default"]
    duration = max(s["sim_minutes_min"], min(s["sim_minutes_max"], duration))
    changes = [c for c in (raw.get("changes") or []) if isinstance(c, dict)]
    return {"outcome": outcome, "gains": gains, "effort": effort, "duration": duration,
            "changes": changes}


def resolve(backend: LLMBackend, cfg: Config, action: str, context: str) -> dict[str, Any]:
    raw = backend.chat_json("narrator", cfg.settings["narrator_model"],
                            build_messages(cfg, action, context), schema(cfg))
    return parse(raw, cfg)

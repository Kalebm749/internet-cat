"""Brain (actor) call. M2: minimal prompt. M4 replaces it with the full situation overview."""
from __future__ import annotations

from typing import Any

from cat.config import Config
from cat.llm import BackendError, LLMBackend

FEELINGS = ["negative", "neutral", "positive"]

BRAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "reaction": {"type": "string"},
        "thought": {"type": "string"},
        "action": {"type": "string"},
        "intent": {"type": "string"},
        "feeling": {"type": "string", "enum": FEELINGS},
        "wants_sleep": {"type": "boolean"},
    },
    "required": ["reaction", "thought", "action", "intent", "feeling", "wants_sleep"],
}


def build_messages(cfg: Config, situation: str) -> list[dict]:
    c = cfg.character
    system = (f"You are {c['name']}, a {c['species']}. Traits: {', '.join(c['core_traits'])}. "
              f"Voice: {c.get('voice', 'plain')}. Reply with JSON only.")
    return [{"role": "system", "content": system}, {"role": "user", "content": situation}]


def parse(raw: dict) -> dict[str, Any]:
    action = str(raw.get("action") or "").strip()
    if not action:
        raise BackendError("brain returned no action")
    feeling = raw.get("feeling")
    return {"reaction": str(raw.get("reaction") or ""), "thought": str(raw.get("thought") or ""),
            "action": action, "intent": str(raw.get("intent") or ""),
            "feeling": feeling if feeling in FEELINGS else "neutral",
            "wants_sleep": bool(raw.get("wants_sleep", False))}


def think(backend: LLMBackend, cfg: Config, situation: str) -> dict[str, Any]:
    raw = backend.chat_json("brain", cfg.settings["brain_model"],
                            build_messages(cfg, situation), BRAIN_SCHEMA)
    return parse(raw)

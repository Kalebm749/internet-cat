"""Narrator: prose only. The world (code) has already decided what a move did; the narrator turns that into
one or two sentences for the diary. It decides nothing and changes nothing: if it fails, the move's plain
templated outcome is used instead."""
from __future__ import annotations

from typing import Any

from cat import clock
from cat import prompts as P
from cat.config import Config
from cat.llm import BackendError, LLMBackend, call_json


def schema(cfg: Config) -> dict:
    return {"type": "object", "properties": {"outcome": {"type": "string"}}, "required": ["outcome"]}


def system_prompt(cfg: Config) -> str:
    return P.render(cfg, "narrator.system")


def describe_world(cfg: Config, sim_time: str, area_id: str, weather: str,
                   here: list[dict], inventory: list[dict]) -> str:
    areas = cfg.world["areas"]
    dt = clock.parse(sim_time)

    def fmt(o):
        return f"- {o['name']} ({', '.join(o['tags']) or 'no tags'})"

    return (f"Place: {areas[area_id]['name']}: {areas[area_id]['description']}. "
            f"Time: {clock.time_label(dt)}. Weather: {weather}.\n"
            "Objects here:\n" + "\n".join([fmt(o) for o in here] or ["(nothing)"])
            + "\nCarrying:\n" + "\n".join([fmt(o) for o in inventory] or ["(nothing)"]))


def build_messages(cfg: Config, action: str, context: str, result: str) -> list[dict]:
    return [{"role": "system", "content": system_prompt(cfg)},
            {"role": "user", "content": P.render(cfg, "narrator.user", context=context, action=action,
                                                 result=result)}]


def parse(raw: dict, cfg: Config) -> dict[str, Any]:
    outcome = " ".join(str(raw.get("outcome") or "").split())
    if not outcome:
        raise BackendError("narrator returned no outcome")
    return {"outcome": outcome}


def resolve(backend: LLMBackend, cfg: Config, action: str, context: str, result: str) -> dict[str, Any]:
    return call_json(backend, "narrator", cfg.settings["narrator_model"],
                     build_messages(cfg, action, context, result), schema(cfg),
                     lambda raw: parse(raw, cfg), cfg.settings["llm_retries"])

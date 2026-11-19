"""Narrator call: a neutral, personality-free resolver. It says what actually happened, scores
needs in coarse buckets and proposes world changes. Code validates the changes (effects.py)."""
from __future__ import annotations

from typing import Any

from cat import character, clock
from cat import prompts as P
from cat.config import Config
from cat.llm import BackendError, LLMBackend, call_json

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
            "project": {"type": "object", "properties": {
                "id": {"type": "integer"},
                "advance": {"type": "string", "enum": sorted(cfg.settings["projects"]["progress_values"]) + ["0"]},
                "note": {"type": "string"},
                "artifact": {"type": "object", "properties": {
                    "kind": {"type": "string"}, "title": {"type": "string"}, "text": {"type": "string"}}}}},
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


def system_prompt(cfg: Config) -> str:
    s = cfg.settings
    needs = []
    for k, n in cfg.character["needs"].items():
        note = P.render(cfg, "narrator.need_consume_note") if k == s["sustenance_need"] else ""
        needs.append(f"  - {k}{note}")
    return P.render(cfg, "narrator.system", needs_list="\n".join(needs),
                    sim_minutes_min=s["sim_minutes_min"], sim_minutes_max=s["sim_minutes_max"],
                    allowed_tags=", ".join(s["allowed_tags"]))


def describe_needs(cfg: Config, needs: dict[str, float]) -> str:
    """Each need's meaning and its current band as a word (never a number)."""
    lines = [P.render(cfg, "narrator.need_line", need=k, meaning=spec["meaning"],
                      band=character.need_band(float(needs[k]), cfg))
             for k, spec in cfg.character["needs"].items()]
    return P.render(cfg, "narrator.needs_heading") + "\n" + "\n".join(lines)


def describe_world(cfg: Config, sim_time: str, area_id: str, weather: str,
                   here: list[dict], inventory: list[dict], needs: dict[str, float] | None = None,
                   projects: list[dict] | None = None) -> str:
    areas = cfg.world["areas"]
    dt = clock.parse(sim_time)

    def fmt(o):
        state = ", ".join(f"{k}={v}" for k, v in o["state"].items()) or "none"
        return f"- {o['id']}: {o['name']} | tags: {', '.join(o['tags']) or 'none'} | state: {state}"

    exits = "; ".join(f"{a} ({areas[a]['name']})" for a in areas[area_id]["exits"])
    text = (f"Location: {area_id} ({areas[area_id]['name']}): {areas[area_id]['description']}. "
            f"Time: {clock.time_label(dt)}. Weather: {weather}.\n"
            f"Exits: {exits}\nObjects here:\n" + "\n".join([fmt(o) for o in here] or ["(nothing)"])
            + "\nInventory:\n" + "\n".join([fmt(o) for o in inventory] or ["(empty)"]))
    if needs is not None:
        text += "\n" + describe_needs(cfg, needs)
    if projects:
        R = lambda key, **kw: P.render(cfg, "narrator." + key, **kw)
        text += "\n" + R("projects_heading") + "\n" + "\n".join(
            R("project_line", id=p["id"], title=p["title"], stage=p["stage"],
              why=R("project_why", why=p["why"]) if p["why"] else "",
              note=R("project_note", note=p["note"]) if p["note"] else "") for p in projects)
    return text


def build_messages(cfg: Config, action: str, context: str) -> list[dict]:
    return [{"role": "system", "content": system_prompt(cfg)},
            {"role": "user", "content": P.render(cfg, "narrator.user", context=context, action=action)}]


def parse(raw: dict, cfg: Config) -> dict[str, Any]:
    s = cfg.settings
    outcome = str(raw.get("outcome") or "").strip()
    if not outcome:
        raise BackendError("narrator returned no outcome")
    raw_gains = raw.get("gains") if isinstance(raw.get("gains"), dict) else {}
    gains = {k: str(v) for k, v in raw_gains.items()
             if k in cfg.character["needs"] and str(v) in s["gain_values"]}
    effort = raw.get("effort") if raw.get("effort") in EFFORTS else "medium"
    try:
        duration = int(raw.get("duration"))
    except (TypeError, ValueError):
        duration = s["sim_minutes_per_tick_default"]
    duration = max(s["sim_minutes_min"], min(s["sim_minutes_max"], duration))
    changes = raw.get("changes") if isinstance(raw.get("changes"), list) else []
    project = raw.get("project") if isinstance(raw.get("project"), dict) else None
    return {"outcome": outcome, "gains": gains, "effort": effort, "duration": duration,
            "changes": [c for c in changes if isinstance(c, dict)], "project": project}


def resolve(backend: LLMBackend, cfg: Config, action: str, context: str) -> dict[str, Any]:
    return call_json(backend, "narrator", cfg.settings["narrator_model"],
                     build_messages(cfg, action, context), schema(cfg),
                     lambda raw: parse(raw, cfg), cfg.settings["llm_retries"])

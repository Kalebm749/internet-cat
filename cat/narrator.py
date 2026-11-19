"""Narrator call: a neutral, personality-free resolver. It says what actually happened, scores
needs in coarse buckets and proposes world changes. Code validates the changes (effects.py)."""
from __future__ import annotations

from typing import Any

from cat import clock
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
        note = " (rises ONLY if the creature eats something edible: use consume)" \
            if n.get("requires_consume") else ""
        needs.append(f"  - {k}{note}")
    return (
        "You are the narrator of a small simulated world. A creature tries an action and you say "
        "what actually happens. You are neutral and fair; you have no personality of your own. "
        "The creature may try anything, but you must respect the world as given below: do not "
        "invent objects, exits or abilities that are not there. If the action can't work, say "
        "so and what happens instead. Bad luck (a scraped paw, getting wet) is allowed in the "
        "story.\n\n"
        "Reply with JSON only:\n"
        "- outcome: 1-2 sentences, what actually happened\n"
        "- gains: for each need the action helped, \"+\" (a little) or \"++\" (a lot). Omit or "
        "use \"0\" for needs it did not help. Never give negative values.\n"
        + "\n".join(needs) + "\n"
        "- effort: low, medium or high (physical exertion)\n"
        f"- duration: minutes it took, {s['sim_minutes_min']} to {s['sim_minutes_max']}\n"
        "- changes: the ONLY way the saved world may change. Use only these ops, and only the "
        "ids shown in the world description:\n"
        "  - move_self {area}: the creature goes to a connected area (see Exits)\n"
        "  - move_object {object_id, area}: a movable object, here or carried, goes to here or an "
        "adjacent area\n"
        "  - take {object_id}: pick up a movable object that is here\n"
        "  - drop {object_id}: put down something carried\n"
        "  - consume {object_id}: eat/use up an edible object. It is destroyed\n"
        "  - set_state {object_id, key, value}: change an object's state (e.g. open -> true)\n"
        f"  - create {{name, tags, area}}: something newly made, in the current area. Tags from: "
        f"{', '.join(s['allowed_tags'])}\n"
        "Only list a change if the story really requires it. If nothing changes, use an empty "
        "list. If your outcome text says the creature picked something up, ate it, moved, or "
        "built something, include the matching change.")


def describe_world(cfg: Config, sim_time: str, area_id: str, weather: str,
                   here: list[dict], inventory: list[dict]) -> str:
    areas = cfg.world["areas"]
    dt = clock.parse(sim_time)

    def fmt(o):
        state = ", ".join(f"{k}={v}" for k, v in o["state"].items()) or "none"
        return f"- {o['id']}: {o['name']} | tags: {', '.join(o['tags']) or 'none'} | state: {state}"

    exits = "; ".join(f"{a} ({areas[a]['name']})" for a in areas[area_id]["exits"])
    return (f"Location: {area_id} ({areas[area_id]['name']}): {areas[area_id]['description']}. "
            f"Time: {clock.time_label(dt)}. Weather: {weather}.\n"
            f"Exits: {exits}\nObjects here:\n" + "\n".join([fmt(o) for o in here] or ["(nothing)"])
            + "\nInventory:\n" + "\n".join([fmt(o) for o in inventory] or ["(empty)"]))


def build_messages(cfg: Config, action: str, context: str) -> list[dict]:
    return [{"role": "system", "content": system_prompt(cfg)},
            {"role": "user", "content": f"{context}\n\nThe creature does: {action}"}]


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
    return {"outcome": outcome, "gains": gains, "effort": effort, "duration": duration,
            "changes": [c for c in changes if isinstance(c, dict)]}


def resolve(backend: LLMBackend, cfg: Config, action: str, context: str) -> dict[str, Any]:
    return call_json(backend, "narrator", cfg.settings["narrator_model"],
                     build_messages(cfg, action, context), schema(cfg),
                     lambda raw: parse(raw, cfg), cfg.settings["llm_retries"])

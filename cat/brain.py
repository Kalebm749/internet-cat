"""Brain (actor) call: builds the situation overview, parses the JSON reply.

The brain gets a *situation*, not a menu. Words, never raw numbers. It never says how an action
changes needs or the world; the narrator decides that."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cat import character, clock
from cat.config import Config
from cat.llm import BackendError, LLMBackend, call_json

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


@dataclass
class BrainContext:
    sim_time: str
    area_id: str
    needs: dict[str, float]
    energy: float
    mood: dict
    tendencies: dict[str, float]
    weather: str
    objects_here: list[dict]
    inventory: list[dict]
    last_outcome: str | None = None
    events: list[str] = field(default_factory=list)
    recent_actions: list[str] = field(default_factory=list)      # oldest first
    preferences: list[dict] = field(default_factory=list)        # {subject, valence, reason}
    long_memories: list[str] = field(default_factory=list)       # M5
    short_memories: list[str] = field(default_factory=list)      # M5
    just_woke_from_doze: bool = False
    self_image: list[str] = field(default_factory=list)          # what it currently thinks it is (text only)


def seed_self_image(cfg: Config) -> list[str]:
    """The optional text hint from character.yaml (empty by default)."""
    seed = (cfg.character.get("self_image") or {}).get("seed", "")
    return [seed.strip()] if isinstance(seed, str) and seed.strip() else []


def tendency_phrase(cfg: Config, name: str, value: float) -> str:
    labels = cfg.character.get("tendency_labels", {}).get(name)
    w = cfg.settings["tendency_words"]
    if labels:
        if value < w["low_below"]:
            return f"{labels[0]} ({name})"
        if value > w["high_above"]:
            return f"{labels[1]} ({name})"
        return f"neither {labels[0]} nor {labels[1]} ({name})"
    if value < w["low_below"]:
        return f"low {name}"
    if value > w["high_above"]:
        return f"high {name}"
    return f"moderate {name}"


def _obj(o: dict) -> str:
    bits = [o["name"]]
    if o["tags"]:
        bits.append(f"[{', '.join(o['tags'])}]")
    if o["state"]:
        bits.append("(" + ", ".join(f"{k}: {v}" for k, v in o["state"].items()) + ")")
    return " ".join(bits)


def system_prompt(cfg: Config) -> str:
    c = cfg.character
    pron = c.get("pronoun", "they")
    parts = [f"You are {c['name']}. You live in a small world and think and act "
             f"as {c['name']} would. You are not an assistant; you are living a life."]
    if c.get("backstory"):
        parts.append(f"Backstory: {c['backstory']}")
    parts.append(
        f"## Identity\nYou have a name ({c['name']}), a voice, core traits and tendencies. You do not "
        "know what you are, or whether you have any physical form. You can notice things about your "
        "own situation and limits, but you must not assert a form, species or body that is not in your "
        "self-image below.")
    parts.append(f"Core traits (always true of you): {', '.join(c['core_traits'])}.")
    if c.get("voice"):
        parts.append(f"Your voice (for reaction and thought): {c['voice']}")
    parts.append(
        "Each turn you are shown your situation. Respond with JSON only:\n"
        "- reaction: how you feel about what just happened, in your own voice\n"
        "- thought: what is on your mind\n"
        "- action: what you do next, in plain words (e.g. \"climb the old oak to watch the "
        "sunset\"). You may try anything that makes sense where you are; the world decides what "
        "actually happens\n"
        "- intent: why you do it\n"
        "- feeling: negative, neutral or positive: your overall feeling right now\n"
        "- wants_sleep: true only if you want to go to sleep now\n"
        "Do not describe results or consequences of your action, and do not invent how needs or "
        f"objects change. Refer to yourself as {pron} only if you need to; speak as yourself.")
    return "\n\n".join(parts)


def situation_prompt(cfg: Config, ctx: BrainContext) -> str:
    dt = clock.parse(ctx.sim_time)
    area = cfg.world["areas"][ctx.area_id]
    L: list[str] = []

    L.append("## Who you are right now")
    if ctx.self_image:
        L.append("What you currently think you are (your self-image): " + " ".join(ctx.self_image))
    else:
        L.append("Your self-image is empty: you have no picture yet of what you are or whether you have "
                 "a form.")
    if ctx.tendencies:
        L.append("Your tendencies lately: " + "; ".join(
            tendency_phrase(cfg, k, v) for k, v in ctx.tendencies.items()) + ".")
    loves = [p for p in ctx.preferences if p["valence"] > 0]
    dislikes = [p for p in ctx.preferences if p["valence"] < 0]
    if loves:
        L.append("You like: " + "; ".join(p["subject"] for p in loves) + ".")
    if dislikes:
        L.append("You dislike: " + "; ".join(p["subject"] for p in dislikes) + ".")

    L.append("\n## Now")
    L.append(f"It is {clock.time_label(dt)} ({clock.part_of_day(dt)}). Weather: {ctx.weather}.")
    L.append(f"You are in {area['name']}. {area['description']}")
    L.append(f"You feel {ctx.mood['description']}. Energy: {character.energy_word(ctx.energy, cfg)}.")
    L.append("Needs: " + "; ".join(
        f"{character.need_label(k, cfg)}: {character.need_word(k, v, cfg)}"
        for k, v in ctx.needs.items()) + ".")

    L.append("\n## Around you")
    L.append("Here: " + ("; ".join(_obj(o) for o in ctx.objects_here) or "nothing in particular") + ".")
    L.append("You are carrying: " + ("; ".join(_obj(o) for o in ctx.inventory) or "nothing") + ".")
    exits = [cfg.world["areas"][e]["name"] for e in area["exits"]]
    L.append("From here you can go to: " + ", ".join(exits) + ".")

    L.append("\n## What just happened")
    if ctx.events:
        L.append("The world: " + " ".join(ctx.events))
    if ctx.just_woke_from_doze:
        L.append("You have just come round from a long, hazy doze. Time has passed and you "
                 "can't quite remember what you were doing.")
    else:
        L.append(ctx.last_outcome or "Nothing yet. This is the start of your day.")
    if ctx.recent_actions:
        L.append("Recently you did: " + "; then ".join(ctx.recent_actions) + ".")

    if ctx.long_memories or ctx.short_memories:
        L.append("\n## Memories")
        if ctx.long_memories:
            L.append("You remember: " + " ".join(ctx.long_memories))
        if ctx.short_memories:
            L.append("Earlier today: " + " ".join(ctx.short_memories))

    L.append("\nWhat do you do next? Reply with JSON.")
    return "\n".join(L)


def build_messages(cfg: Config, ctx: BrainContext) -> list[dict]:
    return [{"role": "system", "content": system_prompt(cfg)},
            {"role": "user", "content": situation_prompt(cfg, ctx)}]


def parse(raw: dict) -> dict[str, Any]:
    action = str(raw.get("action") or "").strip()
    if not action:
        raise BackendError("brain returned no action")
    feeling = raw.get("feeling")
    return {"reaction": str(raw.get("reaction") or "").strip(),
            "thought": str(raw.get("thought") or "").strip(),
            "action": action, "intent": str(raw.get("intent") or "").strip(),
            "feeling": feeling if feeling in FEELINGS else "neutral",
            "wants_sleep": raw.get("wants_sleep") is True}


def think(backend: LLMBackend, cfg: Config, ctx: BrainContext) -> dict[str, Any]:
    return call_json(backend, "brain", cfg.settings["brain_model"], build_messages(cfg, ctx),
                     BRAIN_SCHEMA, parse, cfg.settings["llm_retries"])

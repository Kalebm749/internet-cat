"""Brain (actor) call: builds the situation overview, parses the JSON reply.

The brain gets a *situation*, not a menu. Words, never raw numbers. It never says how an action
changes needs or the world; the narrator decides that."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cat import character, clock
from cat import prompts as P
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
    stale_here: bool = False                                     # it has stayed in this area for a while
    projects: list[dict] = field(default_factory=list)           # active projects (words only, see projects.brain_view)


PRESSING_BANDS = ("empty", "low")      # the bands that pull at the entity, most pressing first


def select_needs(cfg: Config, needs: dict[str, float]) -> dict:
    """Which needs' expressions the prompt shows, and why. A need is pressing in the empty or low
    band; the lowest bands come first, ties broken by config order, at most `max_pressing_needs`
    shown. If nothing is pressing, one full-band expression (first in config order) may stand in.
    Pure words: no numbers leave this function and nothing here picks or ranks actions."""
    s = cfg.settings
    rows = []
    for i, (nid, spec) in enumerate(cfg.character["needs"].items()):
        band = character.need_band(float(needs[nid]), cfg)
        rows.append({"need": nid, "band": band, "order": i, "expression": spec["expressions"].get(band),
                     "selected": False, "reason": f"{band} band: not pressing"})
    pressing = sorted((r for r in rows if r["band"] in PRESSING_BANDS),
                      key=lambda r: (PRESSING_BANDS.index(r["band"]), r["order"]))
    cap = s["max_pressing_needs"]
    chosen = pressing[:cap]
    for rank, r in enumerate(pressing, 1):
        if rank <= cap:
            r["selected"] = True
            r["reason"] = (f"{r['band']} band: pressing, #{rank} of {len(pressing)} "
                           f"(lowest band first, ties by config order)")
        else:
            r["reason"] = f"{r['band']} band: pressing, but only {cap} are shown"
    if not pressing:
        full = next((r for r in rows if r["band"] == "full" and r["expression"]), None)
        if full:
            full["selected"] = True
            full["reason"] = "nothing is pressing: a full-band expression is shown instead"
            chosen = [full]
    return {"max": cap, "none_pressing": not pressing,
            "selected": [r["need"] for r in chosen], "rows": rows}


def pulling_section(cfg: Config, needs: dict[str, float], stale: bool) -> list[str]:
    """The prompt lines under "What's pulling at you right now"."""
    R = lambda key, **kw: P.render(cfg, "brain.situation." + key, **kw)
    sel = select_needs(cfg, needs)
    by_need = {r["need"]: r for r in sel["rows"]}
    lines = [by_need[n]["expression"] for n in sel["selected"]]               # most pressing first
    if sel["none_pressing"]:
        lines = [R("none_pulling")] + lines
    if stale and cfg.character.get("stale_here"):
        lines.append(cfg.character["stale_here"].strip())
    return [R("heading_pulling")] + lines


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
    R = lambda key, **kw: P.render(cfg, "brain.system." + key, **kw)
    parts = [R("intro", name=c["name"])]
    if c.get("backstory"):
        parts.append(R("backstory", backstory=c["backstory"]))
    parts.append(R("identity", name=c["name"]))
    parts.append(R("core_traits", core_traits=", ".join(c["core_traits"])))
    if c.get("voice"):
        parts.append(R("voice", voice=c["voice"]))
    parts.append(R("output", pronoun=c.get("pronoun", "they")))
    return "\n\n".join(parts)


def situation_prompt(cfg: Config, ctx: BrainContext) -> str:
    dt = clock.parse(ctx.sim_time)
    area = cfg.world["areas"][ctx.area_id]
    R = lambda key, **kw: P.render(cfg, "brain.situation." + key, **kw)
    L: list[str] = []

    L.append(R("heading_who"))
    if ctx.self_image:
        L.append(R("self_image", self_image=" ".join(ctx.self_image)))
    else:
        L.append(R("self_image_empty"))
    if ctx.tendencies:
        L.append(R("tendencies", tendencies="; ".join(
            tendency_phrase(cfg, k, v) for k, v in ctx.tendencies.items())))
    loves = [p for p in ctx.preferences if p["valence"] > 0]
    dislikes = [p for p in ctx.preferences if p["valence"] < 0]
    if loves:
        L.append(R("likes", items="; ".join(p["subject"] for p in loves)))
    if dislikes:
        L.append(R("dislikes", items="; ".join(p["subject"] for p in dislikes)))

    L.append("\n" + R("heading_now"))
    L.append(R("time", time=clock.time_label(dt), part=clock.part_of_day(dt), weather=ctx.weather))
    L.append(R("place", area=area["name"], description=area["description"]))
    L.append(R("feeling", mood=ctx.mood["description"], energy=character.energy_word(ctx.energy, cfg)))

    L.append("\n" + "\n".join(pulling_section(cfg, ctx.needs, ctx.stale_here)))

    if ctx.projects:
        L.append("\n" + R("heading_project"))
        for pr in ctx.projects:
            L.append(R("project", title=pr["title"],
                       why=R("project_why", why=pr["why"]) if pr["why"] else "", stage=pr["stage"],
                       note=R("project_note", note=pr["note"]) if pr["note"] else "",
                       idle=(R("project_new") if pr.get("untouched") and not pr["idle"] else
                             R("project_idle", ago=pr["idle"]) if pr["idle"] else "")))

    L.append("\n" + R("heading_around"))
    L.append(R("here", items="; ".join(_obj(o) for o in ctx.objects_here) or R("here_nothing")))
    L.append(R("carrying", items="; ".join(_obj(o) for o in ctx.inventory) or R("carrying_nothing")))
    exits = [cfg.world["areas"][e]["name"] for e in area["exits"]]
    L.append(R("exits", exits=", ".join(exits)))

    L.append("\n" + R("heading_happened"))
    if ctx.events:
        L.append(R("events", events=" ".join(ctx.events)))
    if ctx.just_woke_from_doze:
        L.append(R("woke_from_doze"))
    else:
        L.append(ctx.last_outcome or R("no_outcome"))
    if ctx.recent_actions:
        L.append(R("recent", actions="; then ".join(ctx.recent_actions)))

    if ctx.long_memories or ctx.short_memories:
        L.append("\n" + R("heading_memories"))
        if ctx.long_memories:
            L.append(R("remember", memories=" ".join(ctx.long_memories)))
        if ctx.short_memories:
            L.append(R("earlier_today", memories=" ".join(ctx.short_memories)))

    L.append("\n" + R("closing"))
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

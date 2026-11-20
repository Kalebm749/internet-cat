"""The world director: an LLM role that proposes the occasional small happening in the entity's area.

Hand-written events in world.yaml stay as seeds and as the fallback. The director adds novelty: a sound,
a change of light, a visitor, a found thing. It sees the place, the time, the weather, recent happenings and
what the entity just did, but NEVER its needs or mood, so it cannot rescue or punish it. Code validates every
proposal (length, repeats, caps, known weather, allowed tags, need ids) and applies it through the same
shape as an event, so it is traced and audited like one. Nothing here picks or ranks the entity's actions."""
from __future__ import annotations

import re
import logging
import math
from typing import Any

from cat import clock
from cat import prompts as P
from cat.config import Config
from cat.llm import BackendError, LLMBackend, call_json

log = logging.getLogger("cat.director")

SCHEMA = {
    "type": "object",
    "properties": {"happening": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "mood": {"type": "number"},
            "weather": {"type": "string"},
            "spawn": {"type": "object", "properties": {"name": {"type": "string"},
                                                       "tags": {"type": "array", "items": {"type": "string"}}}},
            "drains": {"type": "object", "additionalProperties": {"type": "number"}},
        }}},
    "required": ["happening"],
}


def due(cfg: Config, gap: int, stale: bool) -> tuple[bool, str]:
    """Is the director due? `gap` is the number of action ticks since it last ran."""
    d = cfg.settings["director"]
    if not d["enabled"]:
        return False, "disabled"
    if stale and gap >= d["stale_every_ticks"]:
        return True, "stale"
    if gap >= d["every_ticks"]:
        return True, "interval"
    return False, "not due"


def messages(cfg: Config, *, area_id: str, sim_time: str, weather: str, objects: list[str],
             recent_events: list[str], recent_actions: list[str], stale: bool) -> list[dict]:
    area = cfg.world["areas"][area_id]
    dt = clock.parse(sim_time)
    d = cfg.settings["director"]
    R = lambda key, **kw: P.render(cfg, "director." + key, **kw)
    user = R("user", area=area["name"], description=area["description"],
             time=clock.time_label(dt), part=clock.part_of_day(dt), weather=weather,
             weather_options=", ".join(cfg.world.get("weather") or ["clear"]),
             objects=", ".join(objects) or R("none_objects"),
             recent_events="\n".join(f"- {t}" for t in recent_events) or R("none_events"),
             recent_actions="\n".join(f"- {t}" for t in recent_actions) or R("none_actions"),
             stale=R("stale_note") if stale else "")
    system = R("system", allowed_tags=", ".join(cfg.settings["allowed_tags"]),
               need_ids=", ".join(cfg.character["needs"]), max_drain_points=d["max_drain_points"],
               text_max_length=d["text_max_length"])
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate(cfg: Config, raw: Any, recent_events: list[str], here_count: int) -> dict:
    """The director's raw answer -> an outcome. Always has `action`: none | rejected | happened.
    A `happened` outcome carries the cleaned fields; notes lists anything ignored."""
    d = cfg.settings["director"]
    h = raw if isinstance(raw, dict) else None
    if not h:
        return {"action": "none", "reason": "nothing happens"}
    text = " ".join(str(h.get("text") or "").split())[:d["text_max_length"]].strip()
    if not text:
        return {"action": "none", "reason": "nothing happens"}
    if text.lower() in {t.strip().lower() for t in recent_events}:
        return {"action": "rejected", "text": text, "reason": "repeats a recent happening"}
    notes: list[str] = []
    mood = 0.0
    m = h.get("mood")
    if isinstance(m, (int, float)) and not isinstance(m, bool) and math.isfinite(m):
        mood = max(-d["max_mood"], min(d["max_mood"], float(m)))
        if mood != float(m):
            notes.append(f"mood clamped to {mood:+.2f}")
    weather = None
    w = h.get("weather")
    if isinstance(w, str) and w.strip():
        if w in (cfg.world.get("weather") or ["clear"]):
            weather = w
        else:
            notes.append(f"unknown weather '{w}' ignored")
    spawn = None
    sp = h.get("spawn")
    if sp and not d.get("allow_spawn", True):                 # the world's things are defined in world.yaml
        notes.append("spawn ignored: the director makes happenings, not objects")
        sp = None
    if isinstance(sp, dict) and str(sp.get("name") or "").strip():
        name = " ".join(str(sp["name"]).split())[:cfg.settings["name_max_length"]].strip()
        if not re.match(r"(?i)(a|an|the|some|two|three|several|many|one)\b", name):
            name = ("an " if name[:1].lower() in "aeiou" else "a ") + name      # "a butterfly", never a bare noun
        tags = [t for t in (sp.get("tags") or []) if isinstance(t, str) and t in cfg.settings["allowed_tags"]]
        if here_count >= cfg.settings["max_objects_per_area"]:
            notes.append("spawn skipped: the area is full")
        else:
            spawn = {"name": name, "tags": tags}
    drains: dict[str, float] = {}
    dr = h.get("drains")
    if isinstance(dr, dict):
        for nid, pts in dr.items():
            if nid not in cfg.character["needs"]:
                notes.append(f"drain on unknown need '{nid}' ignored")
            elif isinstance(pts, (int, float)) and not isinstance(pts, bool) and math.isfinite(pts) and pts > 0:
                drains[nid] = min(float(pts), float(d["max_drain_points"]))
        if len(drains) > d["max_drain_needs"]:
            keep = sorted(drains, key=lambda k: -drains[k])[:d["max_drain_needs"]]
            notes.append(f"drains limited to {d['max_drain_needs']} needs")
            drains = {k: drains[k] for k in keep}
    return {"action": "happened", "text": text, "mood": round(mood, 3), "weather": weather, "spawn": spawn,
            "drains": drains, "notes": notes}


def propose(backend: LLMBackend, cfg: Config, msgs: list[dict]) -> Any:
    """One call. Returns the raw `happening` (maybe None). Raises BackendError on failure."""
    return call_json(backend, "director", cfg.settings["narrator_model"], msgs, SCHEMA,
                     lambda r: r.get("happening") if isinstance(r, dict) else None,
                     cfg.settings["llm_retries"])

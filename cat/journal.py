"""The journal: the entity's own entry for a moment, written AFTER code has decided what its move did.

The brain picks a move before anything happens, so it can only say what it means to do. The journal call comes
afterwards and is given facts only: what it chose and why, code's plain account of what happened (including
anything looking revealed), what happened around it, which needs crossed a band (as their own expressions, never
names or numbers), how it is now, and any memories that came to mind. It may not add details the facts do not
contain. It decides nothing and changes nothing; if the call fails, the moment simply has no entry (the diary then
shows the reason it gave). This call replaces the old narrator, so a decision still costs two chat calls."""
from __future__ import annotations

import logging
from typing import Any

from cat import brain, character, clock
from cat import prompts as P
from cat.config import Config
from cat.llm import BackendError, LLMBackend, call_json

log = logging.getLogger("cat.journal")

SCHEMA = {"type": "object", "properties": {"entry": {"type": "string"}, "title": {"type": "string"}},
          "required": ["entry"]}
TITLE_MAX = 60


def system_prompt(cfg: Config) -> str:
    """Who is writing (the same identity lines the brain gets, without its move-picking instructions)."""
    c = cfg.character
    R = lambda key, **kw: P.render(cfg, "brain.system." + key, **kw)
    parts = [R("intro", name=c["name"])]
    if c.get("backstory"):
        parts.append(R("backstory", backstory=c["backstory"]))
    parts.append(R("identity", name=c["name"]))
    if c.get("core_traits"):
        parts.append(R("core_traits", core_traits=", ".join(c["core_traits"])))
    parts.append(P.render(cfg, "journal.system", pronoun=c.get("pronoun", "they")))
    return "\n\n".join(parts)


def need_changes(cfg: Config, before: dict[str, float], after: dict[str, float]) -> list[str]:
    """What it can notice about its needs this moment: for each need that crossed into another band, that band's
    own expression (worse or better), or a plain "eased" line when it climbed out of low/empty into a band with no
    wording. Never a need's name or a number."""
    order = character.BANDS
    out: list[str] = []
    for nid, spec in cfg.character["needs"].items():
        if nid not in before or nid not in after:
            continue
        b0, b1 = character.need_band(float(before[nid]), cfg), character.need_band(float(after[nid]), cfg)
        if b0 == b1:
            continue
        expr = (spec.get("expressions") or {}).get(b1)
        if expr:
            out.append(expr.strip())
        elif order.index(b1) > order.index(b0) and b0 in ("empty", "low"):
            out.append(P.render(cfg, "journal.need_eased"))
    return out


def user_prompt(cfg: Config, *, sim_time: str, area_id: str, weather: str, action: str, why: str,
                impulse: bool, result: str, events: list[str], before: dict[str, float],
                after: dict[str, float], mood: dict, energy: float, self_image: list[str],
                tendencies: dict[str, float], preferences: list[dict], projects: list[dict],
                memories: list[str], finishing: str | None = None) -> str:
    S = lambda key, **kw: P.render(cfg, "brain.situation." + key, **kw)
    J = lambda key, **kw: P.render(cfg, "journal." + key, **kw)
    dt = clock.parse(sim_time)
    area = cfg.world["areas"][area_id]
    L: list[str] = [S("heading_who")]
    L.append(S("self_image", self_image=" ".join(self_image)) if self_image else S("self_image_empty"))
    if tendencies:
        L.append(S("tendencies", tendencies="; ".join(brain.tendency_phrase(cfg, k, v) for k, v in tendencies.items())))
    loves = [p["subject"] for p in preferences if p["valence"] > 0]
    hates = [p["subject"] for p in preferences if p["valence"] < 0]
    if loves:
        L.append(S("likes", items="; ".join(loves)))
    if hates:
        L.append(S("dislikes", items="; ".join(hates)))
    for pr in projects:
        L.append(S("project", title=pr["title"], why=S("project_why", why=pr["why"]) if pr.get("why") else "",
                   stage=pr["stage"], note="", idle=""))

    L.append("\n" + J("heading_happened"))
    L.append(S("time", time=clock.time_label(dt), part=clock.part_of_day(dt), weather=weather))
    L.append(S("place", area=area["name"], description=area["description"]))
    L.append(J("did", action=action))
    if impulse:
        L.append(J("impulse"))
    elif why:
        L.append(J("why", why=why))
    L.append(J("result", result=result))
    if events:
        L.append(J("events", events=" ".join(events)))
    if finishing:
        L.append(J("finishing", name=finishing))

    L.append("\n" + J("heading_now"))
    L.extend(need_changes(cfg, before, after))
    L.extend(brain.state_lines(cfg, mood, energy))

    if memories:
        L.append("\n" + J("heading_memories"))
        L.append(S("remember", memories=" ".join(memories)))

    L.append("\n" + J("closing"))
    return "\n".join(L)


def parse(cfg: Config, raw: dict) -> str:
    text = " ".join(str(raw.get("entry") or "").split())[:cfg.settings["journal"]["max_length"]].strip()
    if not text:
        raise BackendError("journal returned no entry")
    return text


def write(backend: LLMBackend, cfg: Config, **facts: Any) -> str | None:
    """The entry, or None if journaling is off or the call failed (a missing entry is honest; a guessed one is not)."""
    return write_full(backend, cfg, **facts)["entry"]


def write_full(backend: LLMBackend, cfg: Config, **facts: Any) -> dict:
    """{"entry": str | None, "title": str | None}. A title is kept only when it has just finished a drawing
    (`finishing`): its own name for the thing it made."""
    if not cfg.settings["journal"]["enabled"]:
        return {"entry": None, "title": None}
    msgs = [{"role": "system", "content": system_prompt(cfg)},
            {"role": "user", "content": user_prompt(cfg, **facts)}]
    try:
        raw = call_json(backend, "journal", cfg.settings["brain_model"], msgs, SCHEMA,
                        lambda r: (parse(cfg, r), r), cfg.settings["llm_retries"])
    except BackendError as e:
        log.warning("journal entry unavailable (%s): the moment has no entry", e)
        return {"entry": None, "title": None}
    entry, r = raw
    title = " ".join(str(r.get("title") or "").split())[:TITLE_MAX].strip().strip('"') if facts.get("finishing") else ""
    return {"entry": entry, "title": title or None}

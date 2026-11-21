"""The Mind: what the creature has in front of it at a decision (docs/rewrite-contracts.md §3.3).

`build` is pure: it reads the life, the world and what the caller passes in (perception, the running activity,
recent activities, memories) and returns a `Mind`. The options are the actions the creature knows at this
location (plus the universal ones) whose conditions hold now, with their inputs resolved to concrete choices.
They are shuffled with a seed and never ranked. Nothing here picks an action.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from cat import clock, conditions, templating
from cat.content import ActionSpec, Content
from cat.life import Life

TRAIT_WORDS = ((0.7, "very {}"), (0.4, "{}"), (0.0, "a little {}"))


@dataclass
class Option:
    id: str
    name: str
    description: str
    inputs: dict[str, dict] = field(default_factory=dict)    # name -> {type, prompt?, max_chars? | choices? | min,max}
    notes: list[str] = field(default_factory=list)          # e.g. preference and repetition notes (steps 5-6)


@dataclass
class Mind:
    now: dict
    location: dict
    perception: list[str]
    body: dict
    activity: dict | None
    recent: list[dict]
    memories: list[str]
    letters: list[dict]
    identity: dict
    options: list[Option]
    learned: list[str] = field(default_factory=list)     # things it chose to keep from reading, in its words (§6.6)
    dwelling: list[str] = field(default_factory=list)    # its own words for a subject it keeps returning to (§6.6)
    inner: list[str] = field(default_factory=list)       # what is strong inside, in words (needs and affect)
    carry: dict | None = None                            # what it was doing, offered as "carry on" (inputs reused)
    menu_info: dict = field(default_factory=dict)        # how the menu was drawn (for the dashboard)
    board: list[str] = field(default_factory=list)       # senders of the letters pinned on its board
    carrying: list[str] = field(default_factory=list)    # what it has in its pocket
    projects: list[str] = field(default_factory=list)    # what it is making, and how it feels about it
    people: list[str] = field(default_factory=list)      # who it knows and how it feels about them
    beliefs: list[str] = field(default_factory=list)     # what it has come to think about where it is and the weather
    mulling: bool = False                                # its recent thoughts keep going the same way (no words quoted)
    loops: list[str] = field(default_factory=list)       # open loops: wants and questions not settled yet (§5.8)

    def option(self, action_id: str) -> Option | None:
        return next((o for o in self.options if o.id == action_id), None)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- choice inputs

def _item_label(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    if "label" in item:
        return str(item["label"])
    if "title" in item:
        return str(item["title"])
    text = str(item.get("text", ""))
    short = text if len(text) <= 60 else text[:57].rstrip() + "..."
    who = item.get("sender") or "someone"
    status = f" ({item['status']})" if item.get("status") in ("unread",) else ""
    return f'{who}: "{short}"{status}' if text else str(item.get("id", "?"))


def resolve_choices(options: Any, life: Life) -> list[dict]:
    """Turn a choice input's `options` into [{value, label}].
    Supported: a literal list, `{exits}`, and `{fact:<loc>.<fact>[.<status>].labels}` for list facts
    (items with an `id`; unread ones first, then newest first)."""
    if isinstance(options, list):
        return [{"value": v, "label": str(v)} for v in options]
    name = templating.placeholders(options)[0]
    if name == "exits":
        locs = life.content.locations
        notes = getattr(life, "exit_notes", {}) or {}
        return [{"value": x, "label": locs[x].name + (f" ({notes[x]})" if notes.get(x) else "")}
                for x in locs[life.location].exits if x in locs]
    if name.startswith("fact:"):
        parts = name[5:].split(".")
        key, rest = ".".join(parts[:2]), parts[2:]
        if not rest or rest[-1] != "labels":
            raise ValueError(f"{{{name}}}: choices from a fact end in '.labels'")
        status = rest[0] if len(rest) == 2 else None
        items = [it for it in life.world.get(key) if isinstance(it, dict) and "id" in it
                 and (status is None or it.get("status") == status)]
        items = list(reversed(items))
        items.sort(key=lambda it: it.get("status") != "unread")
        return [{"value": str(it["id"]), "label": _item_label(it)} for it in items]
    raise ValueError(f"{{{name}}} cannot give choices")


# ---------------------------------------------------------------- options

def ctx_for(life: Life, known: set[str], last_done: dict[str, float], rng: random.Random,
            count_done: Callable[[str, float], int] | None = None, time_scale: float = 24.0) -> conditions.Ctx:
    return conditions.Ctx(creature=life.bodystate.as_dict(), facts=life.world.values, count_done=count_done,
                          time_scale=time_scale,
                          hour=clock.hour_float(clock.parse(life.sim_time)), known=known,
                          last_done=last_done, rng=rng)


def available(life: Life, known: set[str], last_done: dict[str, float], rng: random.Random,
              capability_ok: Callable[[str], bool] | None = None,
              count_done: Callable[[str, float], int] | None = None,
              time_scale: float = 24.0) -> list[tuple[ActionSpec, dict[str, dict]]]:
    """Actions the creature could take right now, with their resolved inputs, in content order."""
    c: Content = life.content
    ctx = ctx_for(life, known, last_done, rng, count_done, time_scale)
    prohibited = set(c.character.get("core", {}).get("prohibited_actions") or [])
    out = []
    for a in c.actions_at(life.location):
        if a.id not in known or a.id in prohibited:
            continue
        if a.capability is not None and not (c.capability_enabled(a.capability)
                                             and (capability_ok is None or capability_ok(a.capability))):
            continue
        if not conditions.all_hold(a.requires, ctx):
            continue
        inputs: dict[str, dict] = {}
        ok = True
        for name, spec in a.inputs.items():
            inp = {k: v for k, v in spec.items() if k != "options"}
            if spec["type"] == "choice":
                inp["choices"] = resolve_choices(spec["options"], life)
                if not inp["choices"]:
                    ok = False
            inputs[name] = inp
        if ok:
            out.append((a, inputs))
    return out


def build_options(life: Life, known: set[str], last_done: dict[str, float], rng: random.Random,
                  notes: Callable[[ActionSpec], list[str]] | None = None,
                  capability_ok: Callable[[str], bool] | None = None,
                  count_done: Callable[[str, float], int] | None = None, time_scale: float = 24.0,
                  menu_size: int = 0, protected: set[str] | None = None, carry: dict | None = None,
                  info: dict | None = None) -> list[Option]:
    """What it can do now. All of what is genuinely available (known, here, its requires hold, its capability on) is
    the pool; with `menu_size` a uniform draw of that many from the pool is offered (chance, not code, decides what
    it meets), plus the `protected` ones that must always be offered (sleep when worn out, food when hungry) and,
    when there is something to carry on with, that. Never ranked; options stay in shuffled order."""
    pool = available(life, known, last_done, rng, capability_ok, count_done, time_scale)
    rng.shuffle(pool)
    chosen = pool
    must = [x for x in pool if x[0].id in (protected or set())]
    if menu_size and len(pool) > menu_size:
        rest = [x for x in pool if x not in must]
        draw = rng.sample(rest, max(0, menu_size - len(must)))
        chosen = [x for x in pool if x in must or x in draw]
    opts = [Option(a.id, a.name, a.description, inputs, list(notes(a)) if notes else []) for a, inputs in chosen]
    if carry and any(a.id == carry["action"] for a, _ in pool):
        spec = next(a for a, _ in pool if a.id == carry["action"])
        opts.append(Option("continue", "carry on", f"Carry on with what you were doing: {spec.name}.", {},
                           list(notes(spec)) if notes else []))
    if info is not None:
        info.update(available=len(pool), drawn=[a.id for a, _ in chosen], guaranteed=[a.id for a, _ in must],
                    carry=carry["action"] if carry else None)
    return opts


# ---------------------------------------------------------------- the rest of the Mind

def _visible(life: Life) -> list[str]:
    """Facts the creature can see here, as plain short lines."""
    lines = []
    for key, v in life.world.visible(life.location).items():
        if key.startswith("world."):
            continue                       # weather and other world facts are in the time line
        spec = life.content.facts[key]
        name = key.split(".", 1)[1].replace("_", " ")
        if spec.visible == "summary" and spec.type == "list":
            items = life.world.get(key)
            by = {}
            for it in items:
                if isinstance(it, dict) and "status" in it:
                    by[it["status"]] = by.get(it["status"], 0) + 1
            extra = (" (" + ", ".join(f"{n} {s}" for s, n in sorted(by.items())) + ")") if by else ""
            lines.append(f"{name}: {len(items)}{extra}")
        elif isinstance(v, (list, dict)):
            lines.append(f"{name}: {len(v)}")
        else:
            lines.append(f"{name}: {v}")
    return lines


def board_now(life: Life) -> list[str]:
    """Who the letters pinned on its board are from (world.yaml `pinned`)."""
    key = life.content.world.get("pinned")
    if not key or life.location != key.split(".", 1)[0]:
        return []
    return [str(i.get("sender") or "someone") for i in life.world.get(key) if isinstance(i, dict) and i.get("status") == "answered"]


def carrying_now(life: Life) -> list[str]:
    """The names of what it carries (world.yaml `carrying` names the list fact)."""
    key = life.content.world.get("carrying")
    return [str(i.get("name", "something")) for i in life.world.get(key) if isinstance(i, dict)] if key else []


def letters_on_mind(life: Life, tuning: dict) -> list[dict]:
    key = life.content.world.get("mailbox")
    if not key:
        return []
    m = tuning["mail"]
    now = clock.parse(life.sim_time)
    out = []
    for it in reversed(life.world.get(key)):
        if not isinstance(it, dict) or it.get("status") != "read":
            continue
        read_at = it.get("read_at")
        if read_at and (now - clock.parse(read_at)).total_seconds() / 86400 > m["on_mind_days"]:
            continue
        text = str(it.get("text", ""))
        if len(text) > m["on_mind_chars"]:
            text = text[: m["on_mind_chars"] - 3].rstrip() + "..."
        out.append({"id": str(it.get("id")), "sender": it.get("sender") or "someone", "text": text,
                    "read_at": read_at})
        if len(out) >= m["on_mind"]:
            break
    return out


def learned_on_mind(life: Life, tuning: dict, seed: Any = 0) -> list[str]:
    """What it chose to keep from its reading, in its own words: the newest, and one older one that comes back to
    mind now and then (so learning seeds new thoughts instead of disappearing into a list)."""
    key = life.content.world.get("learned")
    m = tuning["memory"]
    if not key or m["learned_k"] == 0:
        return []
    now = clock.parse(life.sim_time)
    kept = []
    for it in life.world.get(key):
        if not isinstance(it, dict) or it.get("status") != "kept":
            continue
        when = it.get("reviewed_at") or it.get("read_at")
        if when and (now - clock.parse(when)).total_seconds() / 86400 > m["learned_days"]:
            continue
        words = (it.get("summary") or "").strip()
        meaning = (it.get("meaning") or "").strip()
        text = f"{it.get('title', '')}: {words}" + (f" (what it means to you: {meaning})" if meaning else "")
        if len(text) > m["learned_chars"]:
            text = text[: m["learned_chars"] - 3].rstrip() + "..."
        kept.append(text)
    if not kept:
        return []
    out = [kept[-1]]
    older = kept[:-1]
    if older and m["learned_k"] > 1:
        hour = life.sim_time[:13]
        out.append(older[random.Random(f"{seed}|{hour}").randrange(len(older))])
    return out[: m["learned_k"]]


def _trait_words(traits: dict[str, float]) -> list[str]:
    out = []
    for name, v in sorted(traits.items(), key=lambda kv: -kv[1]):
        fmt = next(f for lo, f in TRAIT_WORDS if v >= lo)
        out.append(fmt.format(name))
    return out


def identity_block(life: Life, core_memories: list[str] | None = None) -> dict:
    s = life.character.sheet
    core = s.get("core") or {}
    return {"name": s.get("name"), "pronoun": s.get("pronoun"), "nature": core.get("nature", ""),
            "senses": core.get("senses") or [], "abilities": core.get("abilities") or [],
            "boundaries": core.get("boundaries") or [], "traits": _trait_words(s.get("traits") or {}),
            "tendencies": [t for t, _ in sorted((s.get("tendencies") or {}).items(), key=lambda kv: -kv[1])]
            + [i["text"] for k in ("habits", "fears", "interests") for i in (s.get("acquired") or {}).get(k, [])]
            ,                                     # what it holds to acts on what reaches it, not on what it is told (spec §13)
            "background": s.get("background", ""), "core_memories": list(core_memories or [])}


def build(life: Life, tuning: dict, *, known: set[str], last_done: dict[str, float] | None = None,
          perception: list[str] | None = None, activity: dict | None = None, recent: list[dict] | None = None,
          memories: list[str] | None = None, repetition: float = 0.0, seed: Any = 0,
          notes: Callable[[ActionSpec], list[str]] | None = None,
          capability_ok: Callable[[str], bool] | None = None, core_memories: list[str] | None = None,
          count_done: Callable[[str, float], int] | None = None, dwelling: list[str] | None = None,
          loops: list[str] | None = None, mulling: bool = False,
          inner: list[str] | None = None, temperament_words: list[str] | None = None,
          menu_size: int = 0, protected: set[str] | None = None, carry: dict | None = None,
          beliefs: list[str] | None = None, projects: list[str] | None = None, people: list[str] | None = None,
          carrying: list[str] | None = None, board: list[str] | None = None) -> Mind:
    rng = random.Random(f"{seed}|{life.sim_time}")
    now = clock.parse(life.sim_time)
    loc = life.content.locations[life.location]
    desc = life.body.describe(life.bodystate, repetition)
    weather = life.world.values.get("world.weather")
    info: dict = {}
    options = build_options(life, known, last_done or {}, rng, notes, capability_ok, count_done,
                            float(tuning.get("time_scale", 24)), menu_size, protected, carry, info)
    return Mind(
        now={"sim_time": life.sim_time, "part_of_day": clock.part_of_day(now), "weather": weather},
        location={"id": loc.id, "name": loc.name, "description": loc.description.strip(),
                  "sheltered": loc.sheltered, "visible_facts": _visible(life),
                  "stay_minutes": round(getattr(life, "stay_minutes", 0.0))},
        perception=list(perception or []),
        body={**{k: round(v, 3) for k, v in life.bodystate.as_dict().items()}, "words": desc.words,
              "derived": desc.derived, "derived_words": desc.derived_words},
        activity=activity,
        recent=list(recent or [])[-tuning["decisions"]["recent_activities"]:] if tuning["decisions"]["recent_activities"] else [],
        memories=list(memories or []),
        letters=letters_on_mind(life, tuning),
        identity={**identity_block(life, core_memories), "temperament": list(temperament_words or [])},
        options=options, carry=carry, menu_info=info,
        learned=learned_on_mind(life, tuning, seed),
        dwelling=list(dwelling or []),
        loops=list(loops or []),
        mulling=bool(mulling),
        inner=list(inner or []),
        beliefs=list(beliefs or []),
        projects=list(projects or []),
        carrying=list(carrying or []),
        board=list(board or []),
        people=list(people or []),
    )

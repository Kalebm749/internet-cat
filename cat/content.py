"""Content packs (docs/rewrite-contracts.md §1, §2.1, §2.3, §4): load and validate `content/`.

    content/character.yaml          the starting character sheet
    content/world.yaml              senses, global facts (weather), global events, effort, body words, derived conditions
    content/locations/*.yaml        one location each, with its facts, events and actions
    content/actions/universal.yaml  actions available everywhere (move, inspect, sleep)
    content/capabilities.yaml       code capabilities, enabled or not, with limits

Loading happens in two passes: each file's structure, then the references between files (exits, facts,
conditions, world ops, placeholders, discoveries, capabilities). Every problem is collected with its
file and line, and `ContentError` lists them all at once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from cat import conditions, ops, templating

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content"

FACT_TYPES = ("int", "float", "text", "bool", "list", "map")
TYPE_DEFAULTS = {"int": 0, "float": 0.0, "text": "", "bool": False, "list": [], "map": {}}
VISIBILITY = ("none", "summary", "full")
DISCOVERY = ("innate", "known_on_arrival", "hidden")
BUILTINS = ("move", "inspect", "sleep")
INPUT_TYPES = ("text", "choice", "int")
CREATURE_VARS = ("mood", "energy", "hunger")
BODY_VARS = ("energy", "hunger", "mood")

LOCATION_KEYS = {"id", "name", "description", "exits", "sheltered", "facts", "events", "actions"}
FACT_KEYS = {"type", "default", "visible", "min", "max", "max_len", "choices", "regrow"}
EVENT_KEYS = {"id", "text", "sense", "chance_per_hour", "salience", "hours", "weather_in", "world"}
ACTION_KEYS = {"id", "name", "description", "family", "discovery", "inspect_hint", "requires", "inputs",
               "duration", "continuous", "per_minute", "on_finish", "outcomes", "on_interrupt", "capability",
               "revisit_key", "effort", "builtin"}
EFFECT_KEYS = {"world", "creature", "discover", "remember", "reflection"}
OUTCOME_KEYS = EFFECT_KEYS | {"when", "text"}
WORLD_KEYS = {"name", "sim_start", "senses", "facts", "events", "effort", "body_words", "derived_conditions"}
CHARACTER_KEYS = {"name", "pronoun", "core", "traits", "tendencies", "background", "likes", "dislikes",
                  "innate_knowledge", "start_location"}
CORE_KEYS = {"nature", "senses", "abilities", "boundaries", "prohibited_actions"}
CAPABILITY_KEYS = {"enabled", "handler", "permissions", "limits"}


class ContentError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(f"content is invalid ({len(problems)} problem(s)):\n  " + "\n  ".join(problems))


# ---------------------------------------------------------------- YAML with line numbers

class LineDict(dict):
    """A mapping that remembers its own line and the line of each key."""
    line: int = 1
    lines: dict


class _Loader(yaml.SafeLoader):
    pass


def _construct_map(loader: _Loader, node: yaml.MappingNode) -> LineDict:
    loader.flatten_mapping(node)
    d = LineDict()
    d.line = node.start_mark.line + 1
    d.lines = {}
    for k_node, v_node in node.value:
        k = loader.construct_object(k_node, deep=True)
        if k in d:
            loader.duplicates.append((k_node.start_mark.line + 1, k))
        d[k] = loader.construct_object(v_node, deep=True)
        d.lines[k] = k_node.start_mark.line + 1
    return d


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_map)


def line_of(d: Any, key: Any = None) -> int:
    if isinstance(d, LineDict):
        return d.lines.get(key, d.line) if key is not None else d.line
    return 1


# ---------------------------------------------------------------- specs

@dataclass
class FactSpec:
    key: str                      # 'location.fact' or 'world.fact'
    type: str
    default: Any
    visible: str = "none"
    min: float | None = None
    max: float | None = None
    max_len: int | None = None
    choices: list | None = None
    regrow: dict | None = None    # {amount, at_hours: [..]} or {amount, every_hours: N}
    source: str = ""


@dataclass
class EventSpec:
    id: str
    location: str | None          # None: global (world.yaml)
    text: str
    sense: str
    chance_per_hour: float = 0.0
    salience: float = 0.5
    hours: list | None = None
    weather_in: list | None = None
    world: list = field(default_factory=list)
    source: str = ""


@dataclass
class ActionSpec:
    id: str
    location: str | None          # None: universal
    name: str
    description: str
    family: str
    discovery: str
    duration: tuple[float, float]
    inspect_hint: str | None = None
    requires: list = field(default_factory=list)
    inputs: dict = field(default_factory=dict)
    continuous: bool = False
    per_minute: dict = field(default_factory=dict)
    on_finish: dict | None = None
    outcomes: list | None = None
    on_interrupt: dict | None = None
    capability: str | None = None
    revisit_key: list = field(default_factory=list)
    effort: Any = None
    builtin: str | None = None
    source: str = ""


@dataclass
class Location:
    id: str
    name: str
    description: str
    exits: list[str]
    sheltered: bool = False
    facts: list[str] = field(default_factory=list)      # fact keys owned here
    events: list[str] = field(default_factory=list)     # event ids
    actions: list[str] = field(default_factory=list)    # action ids
    source: str = ""


@dataclass
class Content:
    dir: Path
    character: dict
    world: dict
    locations: dict[str, Location]
    actions: dict[str, ActionSpec]
    facts: dict[str, FactSpec]
    events: dict[str, EventSpec]
    capabilities: dict[str, dict]

    # the Ref protocol used by conditions and ops
    def fact(self, key: str) -> FactSpec | None:
        return self.facts.get(key)

    @property
    def senses(self) -> list[str]:
        return list(self.world.get("senses") or [])

    def actions_at(self, location_id: str) -> list[ActionSpec]:
        """The location's own actions followed by the universal ones."""
        loc = self.locations[location_id]
        return [self.actions[a] for a in loc.actions] + [a for a in self.actions.values() if a.location is None]

    def capability_enabled(self, name: str) -> bool:
        return bool(self.capabilities.get(name, {}).get("enabled"))


# ---------------------------------------------------------------- loading

class _Problems:
    def __init__(self, base: Path):
        self.base = base
        self.items: list[str] = []

    def add(self, path: Path, line: int, msg: str) -> None:
        try:
            rel = path.relative_to(self.base)
        except ValueError:
            rel = path
        self.items.append(f"{rel}:{line}: {msg}")


def _read(path: Path, probs: _Problems, required: bool = True) -> LineDict | None:
    if not path.exists():
        if required:
            probs.add(path, 1, "missing file")
        return None
    loader = _Loader(path.read_text())
    loader.duplicates = []
    try:
        data = loader.get_single_data()
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        probs.add(path, mark.line + 1 if mark else 1, f"invalid YAML: {getattr(e, 'problem', e)}")
        return None
    finally:
        loader.dispose()
    for line, key in loader.duplicates:
        probs.add(path, line, f"duplicate key '{key}'")
    if data is None:
        data = LineDict()
    if not isinstance(data, dict):
        probs.add(path, 1, "the top level must be a mapping")
        return None
    return data


def _unknown(d: Mapping, allowed: set[str], path: Path, probs: _Problems, what: str) -> None:
    for k in d:
        if k not in allowed:
            probs.add(path, line_of(d, k), f"{what}: unknown field '{k}' (allowed: {', '.join(sorted(allowed))})")


def _missing(d: Mapping, required: tuple[str, ...], path: Path, probs: _Problems, what: str) -> bool:
    gone = [k for k in required if k not in d]
    for k in gone:
        probs.add(path, line_of(d), f"{what}: missing '{k}'")
    return not gone


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _ident(x: Any) -> bool:
    return isinstance(x, str) and x.replace("_", "").isalnum() and x[:1].isalpha() and x == x.lower()


def _parse_fact(owner: str, name: Any, d: Any, path: Path, probs: _Problems, line: int) -> FactSpec | None:
    what = f"fact '{owner}.{name}'"
    if not _ident(name):
        probs.add(path, line, f"{what}: names are lowercase letters, digits and _")
        return None
    if not isinstance(d, dict):
        probs.add(path, line, f"{what}: must be a mapping with a 'type'")
        return None
    _unknown(d, FACT_KEYS, path, probs, what)
    t = d.get("type")
    if t not in FACT_TYPES:
        probs.add(path, line_of(d, "type"), f"{what}: type must be one of {', '.join(FACT_TYPES)}")
        return None
    spec = FactSpec(key=f"{owner}.{name}", type=t, default=d.get("default", TYPE_DEFAULTS[t]),
                    visible=d.get("visible", "none"), min=d.get("min"), max=d.get("max"),
                    max_len=d.get("max_len"), choices=d.get("choices"), regrow=d.get("regrow"),
                    source=f"{path.name}:{line}")
    if spec.visible not in VISIBILITY:
        probs.add(path, line_of(d, "visible"), f"{what}: visible must be one of {', '.join(VISIBILITY)}")
    numeric = t in ("int", "float")
    for k in ("min", "max"):
        v = getattr(spec, k)
        if v is not None and (not numeric or not _is_num(v)):
            probs.add(path, line_of(d, k), f"{what}: {k} needs a number fact and a number")
    if spec.min is not None and spec.max is not None and _is_num(spec.min) and _is_num(spec.max) \
            and spec.min > spec.max:
        probs.add(path, line_of(d, "min"), f"{what}: min is above max")
    if spec.max_len is not None and (t != "list" or not isinstance(spec.max_len, int) or spec.max_len < 1):
        probs.add(path, line_of(d, "max_len"), f"{what}: max_len needs a list fact and a whole number ≥ 1")
    if spec.choices is not None and (t != "text" or not isinstance(spec.choices, list) or not spec.choices):
        probs.add(path, line_of(d, "choices"), f"{what}: choices needs a text fact and a non-empty list")
    if not ops.type_ok(spec, spec.default):
        probs.add(path, line_of(d, "default"), f"{what}: default {spec.default!r} does not fit type {t}"
                  + (f" and choices {spec.choices}" if spec.choices else ""))
    elif numeric and ((spec.min is not None and _is_num(spec.min) and spec.default < spec.min)
                      or (spec.max is not None and _is_num(spec.max) and spec.default > spec.max)):
        probs.add(path, line_of(d, "default"), f"{what}: default is outside min/max")
    if spec.regrow is not None:
        r = spec.regrow
        ok = (numeric and isinstance(r, dict) and _is_num(r.get("amount"))
              and (("at_hours" in r) != ("every_hours" in r))
              and set(r) <= {"amount", "at_hours", "every_hours"})
        if ok and "at_hours" in r:
            ok = isinstance(r["at_hours"], list) and bool(r["at_hours"]) and all(
                _is_num(h) and 0 <= h < 24 for h in r["at_hours"])
        if ok and "every_hours" in r:
            ok = _is_num(r["every_hours"]) and r["every_hours"] > 0
        if not ok:
            probs.add(path, line_of(d, "regrow"),
                      f"{what}: regrow needs a number fact and {{amount, at_hours: [0..23]}} or {{amount, every_hours: N}}")
    return spec


def _parse_event(owner: str | None, d: Any, path: Path, probs: _Problems, line: int) -> EventSpec | None:
    if not isinstance(d, dict):
        probs.add(path, line, "an event must be a mapping")
        return None
    what = f"event '{d.get('id', '?')}'"
    _unknown(d, EVENT_KEYS, path, probs, what)
    if not _missing(d, ("id", "text", "sense"), path, probs, what):
        return None
    if not _ident(d["id"]):
        probs.add(path, line_of(d, "id"), f"{what}: ids are lowercase letters, digits and _")
    for k in ("chance_per_hour", "salience"):
        if k in d and (not _is_num(d[k]) or d[k] < 0 or (k == "salience" and d[k] > 1)):
            probs.add(path, line_of(d, k), f"{what}: {k} must be a number ≥ 0" + (" and ≤ 1" if k == "salience" else ""))
    return EventSpec(id=d["id"], location=owner, text=d["text"], sense=d["sense"],
                     chance_per_hour=d.get("chance_per_hour", 0.0), salience=d.get("salience", 0.5),
                     hours=d.get("hours"), weather_in=d.get("weather_in"), world=d.get("world") or [],
                     source=f"{path.name}:{line}")


def _parse_action(owner: str | None, d: Any, path: Path, probs: _Problems, line: int) -> ActionSpec | None:
    if not isinstance(d, dict):
        probs.add(path, line, "an action must be a mapping")
        return None
    what = f"action '{d.get('id', '?')}'"
    _unknown(d, ACTION_KEYS, path, probs, what)
    if not _missing(d, ("id", "name", "description", "family", "discovery", "duration"), path, probs, what):
        return None
    if not _ident(d["id"]):
        probs.add(path, line_of(d, "id"), f"{what}: ids are lowercase letters, digits and _")
    if d["discovery"] not in DISCOVERY:
        probs.add(path, line_of(d, "discovery"), f"{what}: discovery must be one of {', '.join(DISCOVERY)}")
    dur = d["duration"]
    mins = dur.get("minutes") if isinstance(dur, dict) else None
    if not (isinstance(mins, list) and len(mins) == 2 and all(_is_num(m) and m >= 0 for m in mins)
            and mins[0] <= mins[1] and set(dur) == {"minutes"}):
        probs.add(path, line_of(d, "duration"), f"{what}: duration must be {{minutes: [min, max]}} with 0 ≤ min ≤ max")
        mins = [0, 0]
    builtin = d.get("builtin")
    if builtin is not None:
        if owner is not None:
            probs.add(path, line_of(d, "builtin"), f"{what}: builtin actions belong in actions/universal.yaml")
        elif builtin not in BUILTINS:
            probs.add(path, line_of(d, "builtin"), f"{what}: builtin must be one of {', '.join(BUILTINS)}")
    a = ActionSpec(id=d["id"], location=owner, name=d["name"], description=d["description"],
                   family=d["family"], discovery=d["discovery"], duration=(mins[0], mins[1]),
                   inspect_hint=d.get("inspect_hint"), requires=d.get("requires") or [],
                   inputs=d.get("inputs") or {}, continuous=bool(d.get("continuous", False)),
                   per_minute=d.get("per_minute") or {}, on_finish=d.get("on_finish"),
                   outcomes=d.get("outcomes"), on_interrupt=d.get("on_interrupt"),
                   capability=d.get("capability"), revisit_key=d.get("revisit_key") or [],
                   effort=d.get("effort"), builtin=builtin, source=f"{path.name}:{line}")
    if a.per_minute and not a.continuous:
        probs.add(path, line_of(d, "per_minute"), f"{what}: per_minute needs continuous: true")
    if a.on_finish is not None and a.outcomes is not None:
        probs.add(path, line_of(d, "outcomes"), f"{what}: use on_finish or outcomes, not both")
    if a.on_finish is None and a.outcomes is None and a.builtin is None:
        probs.add(path, line_of(d), f"{what}: does nothing (needs on_finish or outcomes)")
    if a.discovery == "hidden" and a.inspect_hint is not None and not isinstance(a.inspect_hint, str):
        probs.add(path, line_of(d, "inspect_hint"), f"{what}: inspect_hint must be text")
    if a.inspect_hint is not None and a.discovery != "hidden":
        probs.add(path, line_of(d, "inspect_hint"), f"{what}: inspect_hint only makes sense for hidden actions")
    if not isinstance(a.requires, list):
        probs.add(path, line_of(d, "requires"), f"{what}: requires must be a list of conditions")
        a.requires = []
    if not isinstance(a.inputs, dict):
        probs.add(path, line_of(d, "inputs"), f"{what}: inputs must be a mapping of name -> input")
        a.inputs = {}
    for name, inp in a.inputs.items():
        _check_input(what, name, inp, path, probs, line_of(a.inputs, name))
    if a.outcomes is not None:
        if not isinstance(a.outcomes, list) or not a.outcomes:
            probs.add(path, line_of(d, "outcomes"), f"{what}: outcomes must be a non-empty list")
            a.outcomes = []
        elif "when" in (a.outcomes[-1] or {}):
            probs.add(path, line_of(a.outcomes[-1]), f"{what}: the last outcome must have no 'when' (it is the fallback)")
    if a.effort is not None and not (_is_num(a.effort) and a.effort >= 0) and not isinstance(a.effort, str):
        probs.add(path, line_of(d, "effort"), f"{what}: effort must be a number ≥ 0 or a level from world.yaml effort")
    return a


def _check_input(what: str, name: Any, inp: Any, path: Path, probs: _Problems, line: int) -> None:
    w = f"{what} input '{name}'"
    if not _ident(name):
        probs.add(path, line, f"{w}: names are lowercase letters, digits and _")
    if not isinstance(inp, dict) or inp.get("type") not in INPUT_TYPES:
        probs.add(path, line, f"{w}: needs type {' | '.join(INPUT_TYPES)}")
        return
    allowed = {"text": {"type", "max_chars", "prompt"}, "choice": {"type", "options", "prompt"},
               "int": {"type", "min", "max", "prompt"}}[inp["type"]]
    _unknown(inp, allowed, path, probs, w)
    if inp["type"] == "text" and not (isinstance(inp.get("max_chars"), int) and inp["max_chars"] > 0):
        probs.add(path, line, f"{w}: text inputs need max_chars > 0")
    if inp["type"] == "choice":
        o = inp.get("options")
        if not ((isinstance(o, list) and o) or (isinstance(o, str) and templating._FULL.fullmatch(o))):
            probs.add(path, line, f"{w}: options must be a non-empty list or one placeholder like '{{exits}}'")
    if inp["type"] == "int" and not (_is_num(inp.get("min")) and _is_num(inp.get("max")) and inp["min"] <= inp["max"]):
        probs.add(path, line, f"{w}: int inputs need min ≤ max")


def load(content_dir: Path | str = CONTENT_DIR) -> Content:
    """Load and validate a content pack. Raises ContentError listing every problem."""
    base = Path(content_dir)
    probs = _Problems(base)
    facts: dict[str, FactSpec] = {}
    events: dict[str, EventSpec] = {}
    actions: dict[str, ActionSpec] = {}
    locations: dict[str, Location] = {}
    where: dict[str, tuple[Path, int]] = {}          # id -> where it was defined (for cross-reference errors)

    def add_event(ev: EventSpec | None, path: Path, line: int) -> str | None:
        if ev is None:
            return None
        if ev.id in events:
            probs.add(path, line, f"event '{ev.id}' is already defined in {events[ev.id].source}")
            return None
        events[ev.id] = ev
        where[f"event:{ev.id}"] = (path, line)
        return ev.id

    def add_action(a: ActionSpec | None, path: Path, line: int) -> str | None:
        if a is None:
            return None
        if a.id in actions:
            probs.add(path, line, f"action '{a.id}' is already defined in {actions[a.id].source}")
            return None
        actions[a.id] = a
        where[f"action:{a.id}"] = (path, line)
        return a.id

    # ---- world.yaml
    wpath = base / "world.yaml"
    world = _read(wpath, probs) or LineDict()
    _unknown(world, WORLD_KEYS, wpath, probs, "world")
    senses = world.get("senses")
    if not (isinstance(senses, list) and senses and all(isinstance(s, str) for s in senses)):
        probs.add(wpath, line_of(world, "senses"), "world: senses must be a non-empty list of names")
        world["senses"] = []
    for name, d in (world.get("facts") or {}).items():
        spec = _parse_fact("world", name, d, wpath, probs, line_of(world["facts"], name))
        if spec:
            facts[spec.key] = spec
    for ev in world.get("events") or []:
        add_event(_parse_event(None, ev, wpath, probs, line_of(ev)), wpath, line_of(ev))
    _check_world_tables(world, wpath, probs)

    # ---- locations
    ldir = base / "locations"
    files = sorted(ldir.glob("*.yaml")) if ldir.is_dir() else []
    if not files:
        probs.add(ldir, 1, "no location files")
    for path in files:
        d = _read(path, probs)
        if d is None:
            continue
        _unknown(d, LOCATION_KEYS, path, probs, "location")
        if not _missing(d, ("id", "name", "description", "exits"), path, probs, "location"):
            continue
        lid = d["id"]
        if not _ident(lid) or lid == "world":
            probs.add(path, line_of(d, "id"), f"location id '{lid}': lowercase letters, digits and _, not 'world'")
            continue
        if lid in locations:
            probs.add(path, line_of(d, "id"), f"location '{lid}' is already defined in {locations[lid].source}")
            continue
        if path.stem != lid:
            probs.add(path, line_of(d, "id"), f"location '{lid}' should be in {lid}.yaml")
        exits = d["exits"] if isinstance(d["exits"], list) else []
        loc = Location(id=lid, name=d["name"], description=d["description"], exits=exits,
                       sheltered=bool(d.get("sheltered", False)), source=f"{path.name}:{line_of(d)}")
        for name, fd in (d.get("facts") or {}).items():
            spec = _parse_fact(lid, name, fd, path, probs, line_of(d["facts"], name))
            if spec:
                facts[spec.key] = spec
                loc.facts.append(spec.key)
        for ev in d.get("events") or []:
            eid = add_event(_parse_event(lid, ev, path, probs, line_of(ev)), path, line_of(ev))
            if eid:
                loc.events.append(eid)
        for ad in d.get("actions") or []:
            aid = add_action(_parse_action(lid, ad, path, probs, line_of(ad)), path, line_of(ad))
            if aid:
                loc.actions.append(aid)
        locations[lid] = loc
        where[f"location:{lid}"] = (path, line_of(d, "exits"))

    # ---- universal actions
    upath = base / "actions" / "universal.yaml"
    if upath.exists():
        loader_data = _read_list(upath, probs)
        for ad in loader_data:
            add_action(_parse_action(None, ad, upath, probs, line_of(ad)), upath, line_of(ad))
    for b in BUILTINS:
        if not any(a.builtin == b for a in actions.values()):
            probs.add(upath, 1, f"the universal actions must include builtin '{b}'")

    # ---- capabilities
    cpath = base / "capabilities.yaml"
    caps = _read(cpath, probs, required=False) or LineDict()
    for name, cd in caps.items():
        if not isinstance(cd, dict):
            probs.add(cpath, line_of(caps, name), f"capability '{name}': must be a mapping")
            continue
        _unknown(cd, CAPABILITY_KEYS, cpath, probs, f"capability '{name}'")
        if not isinstance(cd.get("handler"), str) or "." not in cd["handler"]:
            probs.add(cpath, line_of(caps, name), f"capability '{name}': handler must be a dotted module path")

    # ---- character.yaml
    chpath = base / "character.yaml"
    character = _read(chpath, probs) or LineDict()

    content = Content(dir=base, character=character, world=world, locations=locations, actions=actions,
                      facts=facts, events=events, capabilities=dict(caps))

    # ---- second pass: references
    _check_character(content, chpath, probs)
    for lid, loc in locations.items():
        path, line = where[f"location:{lid}"]
        for x in loc.exits:
            if x not in locations:
                probs.add(path, line, f"location '{lid}': exit to unknown location '{x}'")
            elif x == lid:
                probs.add(path, line, f"location '{lid}': exit to itself")
    for ev in events.values():
        path, line = where[f"event:{ev.id}"]
        _check_event(content, ev, path, line, probs)
    discovered_by = {t for a in actions.values() for blk in _effect_blocks(a) for t in (blk.get("discover") or [])}
    for a in actions.values():
        path, line = where[f"action:{a.id}"]
        _check_action(content, a, path, line, probs)
        if a.discovery == "hidden" and not a.inspect_hint and a.id not in discovered_by:
            probs.add(path, line, f"action '{a.id}' is hidden but can never be discovered "
                                  "(give it an inspect_hint or name it in another action's discover)")

    if probs.items:
        raise ContentError(probs.items)
    return content


def _read_list(path: Path, probs: _Problems) -> list:
    loader = _Loader(path.read_text())
    loader.duplicates = []
    try:
        data = loader.get_single_data()
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        probs.add(path, mark.line + 1 if mark else 1, f"invalid YAML: {getattr(e, 'problem', e)}")
        return []
    finally:
        loader.dispose()
    if not isinstance(data, list):
        probs.add(path, 1, "the top level must be a list of actions")
        return []
    return data


def _check_world_tables(world: Mapping, path: Path, probs: _Problems) -> None:
    eff = world.get("effort") or {}
    if not isinstance(eff, dict) or not all(_is_num(v) and v >= 0 for v in eff.values()):
        probs.add(path, line_of(world, "effort"), "world: effort must map level names to energy per sim minute (≥ 0)")
    bw = world.get("body_words") or {}
    if not isinstance(bw, dict):
        probs.add(path, line_of(world, "body_words"), "world: body_words must be a mapping")
        bw = {}
    for var, bands in bw.items():
        ln = line_of(bw, var)
        if var not in BODY_VARS:
            probs.add(path, ln, f"body_words: unknown variable '{var}' (known: {', '.join(BODY_VARS)})")
            continue
        froms = [b.get("from") for b in bands] if isinstance(bands, list) and all(isinstance(b, dict) for b in bands) else None
        if not froms or not all(_is_num(f) for f in froms) or froms != sorted(froms) \
                or not all(isinstance(b.get("words"), str) and set(b) == {"from", "words"} for b in bands):
            probs.add(path, ln, f"body_words.{var}: a list of {{from, words}} sorted by 'from'")
    for dc in world.get("derived_conditions") or []:
        ln = line_of(dc)
        if not isinstance(dc, dict) or not isinstance(dc.get("name"), str) or not isinstance(dc.get("words"), str):
            probs.add(path, ln, "derived_conditions: each needs a name and words")
            continue
        for k in set(dc) - {"name", "words"}:
            r = dc[k]
            if k not in ("mood", "energy", "repetition"):
                probs.add(path, ln, f"derived condition '{dc['name']}': unknown range '{k}' (mood, energy, repetition)")
            elif not (isinstance(r, list) and len(r) == 2 and all(_is_num(x) for x in r) and r[0] <= r[1]):
                probs.add(path, ln, f"derived condition '{dc['name']}': {k} must be [low, high]")
    if "sim_start" in world:
        from cat import clock
        try:
            clock.parse(str(world["sim_start"]))
        except ValueError:
            probs.add(path, line_of(world, "sim_start"), "world: sim_start must look like 2026-11-21T07:00")


def _check_character(c: Content, path: Path, probs: _Problems) -> None:
    ch = c.character
    _unknown(ch, CHARACTER_KEYS, path, probs, "character")
    if not _missing(ch, ("name", "pronoun", "core", "traits", "innate_knowledge", "start_location"),
                    path, probs, "character"):
        return
    core = ch["core"]
    if not isinstance(core, dict):
        probs.add(path, line_of(ch, "core"), "character: core must be a mapping")
    else:
        _unknown(core, CORE_KEYS, path, probs, "core")
        _missing(core, ("nature", "senses", "boundaries"), path, probs, "core")
        for s in core.get("senses") or []:
            if s not in c.senses:
                probs.add(path, line_of(core, "senses"), f"core: unknown sense '{s}' (world.yaml senses: {c.senses})")
        if not isinstance(core.get("boundaries", []), list):
            probs.add(path, line_of(core, "boundaries"), "core: boundaries must be a list of sentences")
        for aid in core.get("prohibited_actions") or []:
            if aid not in c.actions:
                probs.add(path, line_of(core, "prohibited_actions"), f"core: prohibited action '{aid}' does not exist")
    traits = ch["traits"]
    if not isinstance(traits, dict) or not all(_is_num(v) and 0 <= v <= 1 for v in traits.values()):
        probs.add(path, line_of(ch, "traits"), "character: traits must map names to strengths 0..1")
    for k in ("tendencies",):
        v = ch.get(k) or {}
        if not isinstance(v, dict) or not all(_is_num(x) and 0 <= x <= 1 for x in v.values()):
            probs.add(path, line_of(ch, k), f"character: {k} must map words to strengths 0..1")
    for k in ("likes", "dislikes"):
        v = ch.get(k) or {}
        if not isinstance(v, dict) or not all(_is_num(x) and 0 <= x <= 1 for x in v.values()):
            probs.add(path, line_of(ch, k), f"character: {k} must map subjects to strengths 0..1")
    for aid in ch["innate_knowledge"] or []:
        a = c.actions.get(aid)
        if a is None:
            probs.add(path, line_of(ch, "innate_knowledge"), f"character: innate action '{aid}' does not exist")
    for a in c.actions.values():
        if a.discovery == "innate" and a.id not in (ch["innate_knowledge"] or []):
            probs.add(path, line_of(ch, "innate_knowledge"),
                      f"character: action '{a.id}' is innate but not in innate_knowledge")
    if ch["start_location"] not in c.locations:
        probs.add(path, line_of(ch, "start_location"), f"character: unknown start_location '{ch['start_location']}'")


def _check_event(c: Content, ev: EventSpec, path: Path, line: int, probs: _Problems) -> None:
    w = f"event '{ev.id}'"
    if ev.sense not in c.senses:
        probs.add(path, line, f"{w}: unknown sense '{ev.sense}' (world.yaml senses: {c.senses})")
    for name, arg in (("hours", ev.hours), ("weather_in", ev.weather_in)):
        if arg is not None:
            for p in conditions.validate({name: arg}, c):
                probs.add(path, line, f"{w}: {p}")
    scope = dict(inputs=set(), has_capability=False, fact_exists=lambda k: k in c.facts)
    for op in ev.world:
        for p in ops.validate(op, fact=c.fact, event_exists=lambda e: e in c.events,
                              check_template=lambda ph: templating.check(ph, **scope)):
            probs.add(path, line_of(op), f"{w}: {p}")


def _effect_blocks(a: ActionSpec) -> list[dict]:
    blocks = [b for b in (a.on_finish, a.on_interrupt) if isinstance(b, dict)]
    return blocks + [o for o in (a.outcomes or []) if isinstance(o, dict)]


def _check_action(c: Content, a: ActionSpec, path: Path, line: int, probs: _Problems) -> None:
    w = f"action '{a.id}'"
    scope = dict(inputs=set(a.inputs), has_capability=a.capability is not None,
                 fact_exists=lambda k: k in c.facts)

    def tmpl(ph: str) -> str | None:
        return templating.check(ph, **scope)

    for cond in a.requires:
        for p in conditions.validate(cond, c, "requires"):
            probs.add(path, line_of(cond), f"{w} requires: {p}")
    for name, inp in a.inputs.items():
        o = inp.get("options") if isinstance(inp, dict) else None
        if isinstance(o, str):
            for ph in templating.placeholders(o):
                p = tmpl(ph)
                if p:
                    probs.add(path, line_of(a.inputs, name), f"{w} input '{name}': {p}")
    if a.capability is not None and a.capability not in c.capabilities:
        probs.add(path, line, f"{w}: unknown capability '{a.capability}' (declare it in capabilities.yaml)")
    for k in a.revisit_key:
        if k not in c.facts:
            probs.add(path, line, f"{w}: revisit_key names unknown fact '{k}'")
    if isinstance(a.effort, str) and a.effort not in (c.world.get("effort") or {}):
        probs.add(path, line, f"{w}: unknown effort level '{a.effort}'")
    _check_creature(a.per_minute, f"{w} per_minute", path, line, probs)
    for blk, is_outcome in [(b, False) for b in (a.on_finish, a.on_interrupt) if b is not None] + \
                           [(o, True) for o in (a.outcomes or [])]:
        bl = line_of(blk)
        if not isinstance(blk, dict):
            probs.add(path, line, f"{w}: effects must be a mapping")
            continue
        _unknown(blk, OUTCOME_KEYS if is_outcome else EFFECT_KEYS, path, probs, w)
        if is_outcome:
            if not isinstance(blk.get("text"), str):
                probs.add(path, bl, f"{w}: every outcome needs a text")
            when = blk.get("when")
            if when is not None:
                for cond in (when if isinstance(when, list) else [when]):
                    for p in conditions.validate(cond, c, "outcome"):
                        probs.add(path, bl, f"{w} when: {p}")
        for k in ("text", "remember"):
            for s in templating.walk_strings(blk.get(k)):
                for ph in templating.placeholders(s):
                    p = tmpl(ph)
                    if p:
                        probs.add(path, line_of(blk, k), f"{w}: {p}")
        world_ops = blk.get("world") or []
        if not isinstance(world_ops, list):
            probs.add(path, line_of(blk, "world"), f"{w}: world must be a list of ops")
            world_ops = []
        for op in world_ops:
            for p in ops.validate(op, fact=c.fact, event_exists=lambda e: e in c.events, check_template=tmpl):
                probs.add(path, line_of(op) if isinstance(op, LineDict) else line_of(blk, "world"), f"{w}: {p}")
        _check_creature(blk.get("creature") or {}, w, path, line_of(blk, "creature"), probs)
        for t in blk.get("discover") or []:
            if t not in c.actions:
                probs.add(path, line_of(blk, "discover"), f"{w}: discovers unknown action '{t}'")
            elif c.actions[t].discovery != "hidden":
                probs.add(path, line_of(blk, "discover"), f"{w}: discovers '{t}', which is not hidden")
        rem = blk.get("remember")
        if rem is not None and not (isinstance(rem, dict) and set(rem) <= {"importance", "text"}
                                    and _is_num(rem.get("importance", 0.5)) and 0 <= rem.get("importance", 0.5) <= 1):
            probs.add(path, line_of(blk, "remember"), f"{w}: remember must be {{importance: 0..1, text?}}")


def _check_creature(eff: Any, w: str, path: Path, line: int, probs: _Problems) -> None:
    if not isinstance(eff, dict):
        probs.add(path, line, f"{w}: creature effects must map mood/energy/hunger to numbers")
        return
    for k, v in eff.items():
        if k not in CREATURE_VARS:
            probs.add(path, line_of(eff, k) if isinstance(eff, LineDict) else line,
                      f"{w}: unknown creature variable '{k}' (known: {', '.join(CREATURE_VARS)})")
        elif not _is_num(v) or abs(v) > 1:
            probs.add(path, line_of(eff, k) if isinstance(eff, LineDict) else line,
                      f"{w}: {k} change must be a number between -1 and 1")

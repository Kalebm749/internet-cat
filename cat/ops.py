"""World effect operations (docs/rewrite-contracts.md §4.4): the only ways content can change world facts.

    set           {op: set, fact, value}
    add           {op: add, fact, value: number}                 clamped to the fact's min/max
    append        {op: append, fact, value}                      bounded by max_len (oldest dropped)
    update_where  {op: update_where, fact, match, set, pick?}
    remove_where  {op: remove_where, fact, match, pick?}
    move_where    {op: move_where, from, to, match, set?, pick?}
    emit_event    {op: emit_event, event, delay_minutes?}        makes an event happen (no fact changes)

`pick` is all (default) | oldest | newest | random_one. List items that are mappings get an `id` when
appended without one. `validate` runs at load time; `apply_all` applies a list of ops to a staged copy of
the facts and returns it only if every op succeeded, so a failing op changes nothing.
Adding an operation means adding it here (code) and to the spec.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from cat import templating
from cat.conditions import matches

PICKS = ("all", "oldest", "newest", "random_one")
FIELDS = {
    "set": ({"fact", "value"}, set()),
    "add": ({"fact", "value"}, set()),
    "append": ({"fact", "value"}, set()),
    "update_where": ({"fact", "match", "set"}, {"pick"}),
    "remove_where": ({"fact", "match"}, {"pick"}),
    "move_where": ({"from", "to", "match"}, {"set", "pick"}),
    "emit_event": ({"event"}, {"delay_minutes"}),
}
LIST_OPS = ("append", "update_where", "remove_where")


class OpError(ValueError):
    pass


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def type_ok(spec: Any, value: Any) -> bool:
    """Does a value fit a fact spec's type (and choices)?"""
    t = spec.type
    if t == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t == "float":
        ok = _num(value)
    elif t == "text":
        ok = isinstance(value, str) and (not spec.choices or value in spec.choices)
    elif t == "bool":
        ok = isinstance(value, bool)
    elif t == "list":
        ok = isinstance(value, list)
    elif t == "map":
        ok = isinstance(value, dict)
    else:
        ok = False
    return ok


# ---------------------------------------------------------------- validation (load time)

def validate(op: Any, *, fact: Callable[[str], Any], event_exists: Callable[[str], bool],
             check_template: Callable[[str], str | None]) -> list[str]:
    """Problems with one op. `fact(key)` returns a fact spec or None."""
    if not isinstance(op, Mapping) or "op" not in op:
        return [f"a world change must be a mapping with an 'op', got {op!r}"]
    name = op["op"]
    if name not in FIELDS:
        return [f"unknown op '{name}' (known: {', '.join(FIELDS)})"]
    need, optional = FIELDS[name]
    probs = [f"{name}: missing '{k}'" for k in sorted(need - set(op))]
    probs += [f"{name}: unknown field '{k}'" for k in sorted(set(op) - need - optional - {"op"})]
    if probs:
        return probs

    def fact_of(key: Any, types: tuple[str, ...] | None = None) -> Any:
        spec = fact(key) if isinstance(key, str) else None
        if spec is None:
            probs.append(f"{name}: no fact '{key}'")
        elif types and spec.type not in types:
            probs.append(f"{name}: fact '{key}' is a {spec.type}, needs {' or '.join(types)}")
            return None
        return spec

    if name == "emit_event":
        if not event_exists(op["event"]):
            probs.append(f"emit_event: no event '{op['event']}'")
        if "delay_minutes" in op and (not _num(op["delay_minutes"]) or op["delay_minutes"] < 0):
            probs.append("emit_event: delay_minutes must be a number ≥ 0")
    elif name == "set":
        spec = fact_of(op["fact"])
        v = op["value"]
        if spec and not (isinstance(v, str) and templating.placeholders(v)) and not type_ok(spec, v):
            probs.append(f"set: value {v!r} does not fit fact '{op['fact']}' ({spec.type})")
    elif name == "add":
        fact_of(op["fact"], ("int", "float"))
        if not _num(op["value"]):
            probs.append("add: value must be a number")
    elif name in LIST_OPS:
        fact_of(op["fact"], ("list",))
    elif name == "move_where":
        fact_of(op["from"], ("list",))
        fact_of(op["to"], ("list",))
    if name in ("update_where", "remove_where", "move_where"):
        if not isinstance(op["match"], Mapping) or not op["match"]:
            probs.append(f"{name}: match must be a non-empty mapping")
        if "set" in op and not isinstance(op["set"], Mapping):
            probs.append(f"{name}: set must be a mapping")
        if op.get("pick", "all") not in PICKS:
            probs.append(f"{name}: pick must be one of {', '.join(PICKS)}")
    for s in templating.walk_strings({k: v for k, v in op.items() if k != "op"}):
        for ph in templating.placeholders(s):
            p = check_template(ph)
            if p:
                probs.append(f"{name}: {p}")
    return probs


# ---------------------------------------------------------------- application (run time)

@dataclass
class Applied:
    facts: dict[str, Any]                                   # the staged facts after all ops
    seq: dict[str, int]                                     # id counters after all ops
    changed: list[str] = field(default_factory=list)        # fact keys that changed, in order
    log: list[dict] = field(default_factory=list)           # one entry per op, for the trace
    events: list[dict] = field(default_factory=list)        # emit_event requests


def _picked(items: list, match: Mapping, pick: str, rng: random.Random) -> list[int]:
    idx = [i for i, it in enumerate(items) if matches(it, match)]
    if not idx or pick == "all":
        return idx
    if pick == "oldest":
        return idx[:1]
    if pick == "newest":
        return idx[-1:]
    return [rng.choice(idx)]


def _bounded(spec: Any, items: list) -> list:
    if spec.max_len is not None and len(items) > spec.max_len:
        return items[len(items) - spec.max_len:]
    return items


def _with_id(item: Any, key: str, seq: dict[str, int]) -> Any:
    if isinstance(item, dict) and "id" not in item:
        seq[key] = seq.get(key, 0) + 1
        item = {"id": str(seq[key]), **item}
    return item


def apply_all(ops: list[Mapping], facts: Mapping[str, Any], specs: Mapping[str, Any],
              vars: dict[str, Any], rng: random.Random, seq: Mapping[str, int] | None = None) -> Applied:
    """Apply ops in order to a copy of `facts`. Raises OpError (and changes nothing) if any op fails."""
    staged = copy.deepcopy(dict(facts))
    out = Applied(facts=staged, seq=dict(seq or {}))
    for op in ops:
        name = op["op"]
        env = {**vars, "facts": staged}
        try:
            r = templating.render({k: v for k, v in op.items() if k != "op"}, env)
        except templating.TemplateError as e:
            raise OpError(f"{name}: {e}") from None
        entry: dict[str, Any] = {"op": name}
        if name == "emit_event":
            out.events.append({"event": r["event"], "delay_minutes": r.get("delay_minutes", 0)})
            entry["event"] = r["event"]
            out.log.append(entry)
            continue
        keys = [r["from"], r["to"]] if name == "move_where" else [r["fact"]]
        for k in keys:
            if k not in specs:
                raise OpError(f"{name}: no fact '{k}'")
        spec = specs[keys[0]]
        before = copy.deepcopy(staged[keys[0]])
        if name == "set":
            if not type_ok(spec, r["value"]):
                raise OpError(f"set: value {r['value']!r} does not fit fact '{keys[0]}' ({spec.type})")
            staged[keys[0]] = r["value"]
        elif name == "add":
            v = staged[keys[0]] + r["value"]
            if spec.min is not None:
                v = max(spec.min, v)
            if spec.max is not None:
                v = min(spec.max, v)
            staged[keys[0]] = int(round(v)) if spec.type == "int" else float(v)
        elif name == "append":
            item = _with_id(copy.deepcopy(r["value"]), keys[0], out.seq)
            staged[keys[0]] = _bounded(spec, staged[keys[0]] + [item])
            entry["item"] = item
        elif name == "update_where":
            items = staged[keys[0]]
            hit = _picked(items, r["match"], r.get("pick", "all"), rng)
            for i in hit:
                if isinstance(items[i], dict):
                    items[i].update(r["set"])
            entry["count"] = len(hit)
        elif name == "remove_where":
            items = staged[keys[0]]
            hit = set(_picked(items, r["match"], r.get("pick", "all"), rng))
            staged[keys[0]] = [it for i, it in enumerate(items) if i not in hit]
            entry["count"] = len(hit)
        elif name == "move_where":
            src, dst = keys
            items = staged[src]
            hit = _picked(items, r["match"], r.get("pick", "all"), rng)
            moved = [{**items[i], **r.get("set", {})} if isinstance(items[i], dict) else items[i] for i in hit]
            staged[src] = [it for i, it in enumerate(items) if i not in set(hit)]
            staged[dst] = _bounded(specs[dst], staged[dst] + moved)
            entry["count"] = len(moved)
            if moved:
                out.changed += [k for k in (src, dst) if k not in out.changed]
        entry["fact"] = keys[0] if name != "move_where" else f"{keys[0]} -> {keys[1]}"
        if name != "move_where" and staged[keys[0]] != before and keys[0] not in out.changed:
            out.changed.append(keys[0])
        out.log.append(entry)
    return out

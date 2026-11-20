"""Conditions (docs/rewrite-contracts.md §4.3): a closed set of small checks used in `requires` and in
outcome `when`.

A condition is a one-key mapping, e.g. `{energy_gte: 0.1}` or `{fact_count_where: [market.mailbox, {status: unread}, 1]}`.
`validate` checks one against the loaded content and returns problems; `evaluate` checks it against a
`Ctx` at run time. Adding a condition type means adding a validator and an evaluator here (code), and a
line in the spec.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

RANGES = {"mood": (-1.0, 1.0), "energy": (0.0, 1.0), "hunger": (0.0, 1.0)}


class Ref(Protocol):
    """What validation needs from the content (implemented by cat.content.Content)."""
    actions: Mapping[str, Any]

    def fact(self, key: str) -> Any: ...


@dataclass
class Ctx:
    """What evaluation needs at run time."""
    creature: Mapping[str, float]                       # mood, energy, hunger
    facts: Mapping[str, Any]                            # 'location.fact' -> value
    hour: float = 12.0                                  # sim hour of day, 0..24
    known: set[str] = field(default_factory=set)        # action ids the creature knows
    last_done: Mapping[str, float] = field(default_factory=dict)   # action id -> sim minutes since it was last done
    rng: random.Random = field(default_factory=lambda: random.Random(0))
    count_done: Callable[[str, float], int] | None = None  # (action id, sim minutes back) -> times started, not rejected
    time_scale: float = 24.0                             # sim minutes per real minute (for real_hours windows)


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def matches(item: Any, match: Mapping[str, Any]) -> bool:
    """A list item matches when every field equals the value, or is one of a list of values."""
    if not isinstance(item, Mapping):
        return False
    for k, want in match.items():
        have = item.get(k)
        if isinstance(want, list):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


# ---------------------------------------------------------------- validators (return a problem or None)

def _v_body(var: str):
    lo, hi = RANGES[var]

    def v(arg, ref, where):
        if not _num(arg) or not lo <= arg <= hi:
            return f"needs a number between {lo} and {hi}"
    return v


def _fact_problem(ref: Ref, key: Any, types: tuple[str, ...] | None = None) -> str | None:
    if not isinstance(key, str):
        return "the first item must be a fact key like 'market.offerings'"
    spec = ref.fact(key)
    if spec is None:
        return f"no fact '{key}'"
    if types and spec.type not in types:
        return f"fact '{key}' is a {spec.type}, needs {' or '.join(types)}"
    return None


def _v_fact_cmp(numeric: bool):
    def v(arg, ref, where):
        if not isinstance(arg, list) or len(arg) != 2:
            return "needs [fact, value]"
        p = _fact_problem(ref, arg[0], ("int", "float") if numeric else None)
        if p:
            return p
        if numeric and not _num(arg[1]):
            return "the value must be a number"
    return v


def _v_count(arg, ref, where):
    if not isinstance(arg, list) or len(arg) != 2:
        return "needs [list fact, n]"
    return _fact_problem(ref, arg[0], ("list",)) or (None if isinstance(arg[1], int) and arg[1] >= 0
                                                     else "n must be a whole number ≥ 0")


def _v_count_where(arg, ref, where):
    if not isinstance(arg, list) or len(arg) != 3:
        return "needs [list fact, {field: value}, n]"
    p = _fact_problem(ref, arg[0], ("list",))
    if p:
        return p
    if not isinstance(arg[1], Mapping) or not arg[1]:
        return "the match must be a non-empty mapping"
    if not isinstance(arg[2], int) or arg[2] < 0:
        return "n must be a whole number ≥ 0"


def _v_hours(arg, ref, where):
    if (not isinstance(arg, list) or len(arg) != 2 or not all(_num(h) and 0 <= h <= 24 for h in arg)):
        return "needs [from, to] hours between 0 and 24 (from > to wraps past midnight)"


def _v_weather(arg, ref, where):
    spec = ref.fact("world.weather")
    if spec is None:
        return "the world has no 'weather' fact"
    if not isinstance(arg, list) or not arg:
        return "needs a list of weathers"
    bad = [w for w in arg if w not in (spec.choices or [])]
    if bad:
        return f"unknown weather {bad} (known: {spec.choices})"


def _v_action(arg, ref, where):
    if arg not in ref.actions:
        return f"no action '{arg}'"


def _v_not_since(arg, ref, where):
    if not isinstance(arg, list) or len(arg) != 2 or not _num(arg[1]):
        return "needs [action, minutes]"
    return _v_action(arg[0], ref, where)


def _v_done_fewer(arg, ref, where):
    if not isinstance(arg, Mapping) or "action" not in arg or "times" not in arg:
        return "needs {action, times, hours | real_hours}"
    extra = set(arg) - {"action", "times", "hours", "real_hours"}
    if extra:
        return f"unknown field(s) {sorted(extra)}"
    if ("hours" in arg) == ("real_hours" in arg):
        return "give exactly one of hours (sim) or real_hours"
    window = arg.get("hours", arg.get("real_hours"))
    if not _num(window) or window <= 0:
        return "the window must be a number of hours > 0"
    if not isinstance(arg["times"], int) or isinstance(arg["times"], bool) or arg["times"] < 1:
        return "times must be a whole number >= 1"
    return _v_action(arg["action"], ref, where)


def _done_fewer(arg, ctx: Ctx) -> bool:
    if ctx.count_done is None:
        return True
    minutes = arg["hours"] * 60 if "hours" in arg else arg["real_hours"] * 60 * ctx.time_scale
    return ctx.count_done(arg["action"], minutes) < arg["times"]


def _v_random(arg, ref, where):
    if where != "outcome":
        return "random_lt is only allowed in an outcome's `when`"
    if not _num(arg) or not 0 <= arg <= 1:
        return "needs a probability between 0 and 1"


def _v_list(arg, ref, where):
    if not isinstance(arg, list) or not arg:
        return "needs a list of conditions"
    probs = [p for c in arg for p in validate(c, ref, where)]
    return "; ".join(probs) or None


def _v_not(arg, ref, where):
    return "; ".join(validate(arg, ref, where)) or None


# ---------------------------------------------------------------- evaluators

def _hours(arg, ctx: Ctx) -> bool:
    lo, hi = arg
    return lo <= ctx.hour < hi if lo <= hi else (ctx.hour >= lo or ctx.hour < hi)


def _count_where(arg, ctx: Ctx) -> bool:
    key, match, n = arg
    return sum(1 for it in ctx.facts.get(key, []) if matches(it, match)) >= n


def _not_since(arg, ctx: Ctx) -> bool:
    ago = ctx.last_done.get(arg[0])
    return ago is None or ago >= arg[1]


Evaluator = Callable[[Any, Ctx], bool]
REGISTRY: dict[str, tuple[Callable[[Any, Ref, str], str | None], Evaluator]] = {
    "energy_gte": (_v_body("energy"), lambda a, c: c.creature["energy"] >= a),
    "energy_lte": (_v_body("energy"), lambda a, c: c.creature["energy"] <= a),
    "hunger_gte": (_v_body("hunger"), lambda a, c: c.creature["hunger"] >= a),
    "hunger_lte": (_v_body("hunger"), lambda a, c: c.creature["hunger"] <= a),
    "mood_gte": (_v_body("mood"), lambda a, c: c.creature["mood"] >= a),
    "mood_lte": (_v_body("mood"), lambda a, c: c.creature["mood"] <= a),
    "fact_gte": (_v_fact_cmp(True), lambda a, c: c.facts[a[0]] >= a[1]),
    "fact_lte": (_v_fact_cmp(True), lambda a, c: c.facts[a[0]] <= a[1]),
    "fact_eq": (_v_fact_cmp(False), lambda a, c: c.facts[a[0]] == a[1]),
    "fact_count_gte": (_v_count, lambda a, c: len(c.facts.get(a[0], [])) >= a[1]),
    "fact_count_where": (_v_count_where, _count_where),
    "hours": (_v_hours, _hours),
    "weather_in": (_v_weather, lambda a, c: c.facts.get("world.weather") in a),
    "knows": (_v_action, lambda a, c: a in c.known),
    "done_before": (_v_action, lambda a, c: a in c.last_done),
    "not_done_since_minutes": (_v_not_since, _not_since),
    "done_fewer_than": (_v_done_fewer, _done_fewer),
    "random_lt": (_v_random, lambda a, c: c.rng.random() < a),
    "any_of": (_v_list, lambda a, c: any(evaluate(x, c) for x in a)),
    "all_of": (_v_list, lambda a, c: all(evaluate(x, c) for x in a)),
    "not": (_v_not, lambda a, c: not evaluate(a, c)),
}


def _unpack(cond: Any) -> tuple[str, Any] | None:
    if isinstance(cond, Mapping) and len(cond) == 1:
        (name, arg), = cond.items()
        return name, arg
    return None


def validate(cond: Any, ref: Ref, where: str = "requires") -> list[str]:
    """Problems with one condition. `where` is 'requires' or 'outcome'."""
    kv = _unpack(cond)
    if kv is None:
        return [f"a condition must be a mapping with one key, got {cond!r}"]
    name, arg = kv
    if name not in REGISTRY:
        return [f"unknown condition '{name}' (known: {', '.join(sorted(REGISTRY))})"]
    p = REGISTRY[name][0](arg, ref, where)
    return [f"{name}: {p}"] if p else []


def evaluate(cond: Mapping[str, Any], ctx: Ctx) -> bool:
    (name, arg), = cond.items()
    return bool(REGISTRY[name][1](arg, ctx))


def all_hold(conds: list[Mapping[str, Any]] | None, ctx: Ctx) -> bool:
    return all(evaluate(c, ctx) for c in conds or [])

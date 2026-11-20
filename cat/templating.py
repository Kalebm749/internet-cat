"""Placeholders in content strings (docs/rewrite-contracts.md §4.4).

A placeholder is `{root}` or `{root.path}` or `{fact:location.fact[.selector]}`. Allowed roots:
  inputs.<name>       an input the action declares
  now                 the sim time
  location.id/.name   where the action happens
  exits               the exits of that location (a list)
  fact:<loc>.<fact>   a world fact (`.count` gives the length of a list)
  capability.<field>  a field of the capability result (only in actions that use a capability)

`check` validates placeholders when content is loaded; `render` fills them when an action runs.
A string that is exactly one placeholder renders to the raw value (a list stays a list).
"""
from __future__ import annotations

import re
from typing import Any, Callable

_FULL = re.compile(r"\{([A-Za-z_]+(?:[:.][A-Za-z0-9_.]+)?)\}")
ROOTS = ("inputs", "now", "location", "exits", "fact", "capability")
LOCATION_FIELDS = ("id", "name")


class TemplateError(ValueError):
    pass


def placeholders(s: str) -> list[str]:
    """Every `{...}` placeholder in a string, without the braces."""
    return _FULL.findall(s)


def _split(name: str) -> tuple[str, str]:
    if name.startswith("fact:"):
        return "fact", name[5:]
    root, _, rest = name.partition(".")
    return root, rest


def check(name: str, *, inputs: set[str], has_capability: bool,
          fact_exists: Callable[[str], bool]) -> str | None:
    """Return a problem with one placeholder, or None when it is fine."""
    root, rest = _split(name)
    if root not in ROOTS:
        return f"unknown placeholder {{{name}}} (allowed: {', '.join(ROOTS)})"
    if root == "inputs":
        if rest not in inputs:
            return f"{{{name}}}: the action declares no input '{rest}'"
    elif root == "now" or root == "exits":
        if rest:
            return f"{{{name}}}: '{root}' has no fields"
    elif root == "location":
        if rest not in LOCATION_FIELDS:
            return f"{{{name}}}: location has only {', '.join(LOCATION_FIELDS)}"
    elif root == "fact":
        parts = rest.split(".")
        if len(parts) < 2 or not fact_exists(".".join(parts[:2])):
            return f"{{{name}}}: no fact '{'.'.join(parts[:2])}'"
    elif root == "capability":
        if not has_capability:
            return f"{{{name}}}: this action uses no capability"
        if not rest:
            return f"{{{name}}}: name a field of the capability result"
    return None


def walk_strings(value: Any):
    """Yield every string inside a nested value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_strings(v)


def _lookup(name: str, vars: dict[str, Any]) -> Any:
    root, rest = _split(name)
    if root == "fact":
        parts = rest.split(".")
        key, selector = ".".join(parts[:2]), parts[2:]
        facts = vars.get("facts") or {}
        if key not in facts:
            raise TemplateError(f"{{{name}}}: no fact '{key}'")
        val = facts[key]
        if selector == ["count"]:
            return len(val)
        if selector:
            raise TemplateError(f"{{{name}}}: selector '{'.'.join(selector)}' is resolved by the Mind builder")
        return val
    if root not in vars:
        raise TemplateError(f"{{{name}}}: '{root}' is not available here")
    val = vars[root]
    for part in [p for p in rest.split(".") if p]:
        if not isinstance(val, dict) or part not in val:
            raise TemplateError(f"{{{name}}}: no '{part}'")
        val = val[part]
    return val


def render(value: Any, vars: dict[str, Any]) -> Any:
    """Fill placeholders in a value (recursively for lists and maps)."""
    if isinstance(value, dict):
        return {k: render(v, vars) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, vars) for v in value]
    if not isinstance(value, str):
        return value
    whole = _FULL.fullmatch(value)
    if whole:
        return _lookup(whole.group(1), vars)
    return _FULL.sub(lambda m: str(_lookup(m.group(1), vars)), value)

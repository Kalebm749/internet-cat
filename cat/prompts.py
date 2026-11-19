"""Prompt text lives in config/prompts.yaml. This module renders it and says what each entry may use.

`SPEC` is the single source of truth: every entry, and the {placeholders} it may contain. The
config validator uses it to refuse an empty, missing or unknown-placeholder entry."""
from __future__ import annotations

from string import Formatter
from typing import Any

SPEC: dict[str, set[str]] = {
    "brain.system.intro": {"name"},
    "brain.system.backstory": {"backstory"},
    "brain.system.identity": {"name"},
    "brain.system.core_traits": {"core_traits"},
    "brain.system.voice": {"voice"},
    "brain.system.output": {"pronoun"},
    "brain.situation.heading_who": set(),
    "brain.situation.self_image": {"self_image"},
    "brain.situation.self_image_empty": set(),
    "brain.situation.tendencies": {"tendencies"},
    "brain.situation.likes": {"items"},
    "brain.situation.dislikes": {"items"},
    "brain.situation.heading_now": set(),
    "brain.situation.time": {"time", "part", "weather"},
    "brain.situation.place": {"area", "description"},
    "brain.situation.feeling": {"mood", "energy"},
    "brain.situation.heading_pulling": set(),
    "brain.situation.none_pulling": set(),
    "brain.situation.heading_around": set(),
    "brain.situation.here": {"items"},
    "brain.situation.here_nothing": set(),
    "brain.situation.carrying": {"items"},
    "brain.situation.carrying_nothing": set(),
    "brain.situation.exits": {"exits"},
    "brain.situation.heading_happened": set(),
    "brain.situation.events": {"events"},
    "brain.situation.woke_from_doze": set(),
    "brain.situation.no_outcome": set(),
    "brain.situation.recent": {"actions"},
    "brain.situation.heading_memories": set(),
    "brain.situation.remember": {"memories"},
    "brain.situation.earlier_today": {"memories"},
    "brain.situation.closing": set(),
    "narrator.system": {"needs_list", "sim_minutes_min", "sim_minutes_max", "allowed_tags"},
    "narrator.needs_heading": set(),
    "narrator.need_line": {"need", "meaning", "band"},
    "narrator.need_consume_note": set(),
    "narrator.user": {"context", "action"},
    "sleep.system": {"name", "core_traits", "job"},
    "sleep.jobs.consolidate": set(),
    "sleep.jobs.tendencies": set(),
    "sleep.jobs.preferences": set(),
    "sleep.jobs.self_image": set(),
    "sleep.jobs.projects": set(),
    "sleep.none_likes": set(),
    "sleep.none_existing_projects": set(),
    "sleep.none_shorts": set(),
    "sleep.none_log": set(),
    "sleep.none_existing_preferences": set(),
    "sleep.none_existing_self_image": set(),
    "sleep.seed_hint": {"seed"},
    "sleep.consolidate": {"shorts", "log", "name", "n"},
    "sleep.tendencies": {"summary", "tendencies"},
    "sleep.preferences": {"summary", "log", "existing", "n"},
    "sleep.self_image": {"summary", "shorts", "log", "seed_hint", "existing"},
    "sleep.projects": {"summary", "log", "likes", "existing", "max_active"},
}


def lookup(prompts: dict, key: str) -> Any:
    cur: Any = prompts
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def check(prompts: Any, extra_spec: dict[str, set[str]] | None = None) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a loaded prompts.yaml."""
    spec = {**SPEC, **(extra_spec or {})}
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(prompts, dict):
        return ["prompts.yaml: top level must be a mapping"], []
    for key, allowed in spec.items():
        v = lookup(prompts, key)
        if v is None:
            errs.append(f"prompts.yaml: missing required entry '{key}'")
            continue
        if not isinstance(v, str) or not v.strip():
            errs.append(f"prompts.yaml '{key}': must be non-empty text")
            continue
        try:
            names = {f for _, f, _, _ in Formatter().parse(v) if f is not None}
        except ValueError as e:
            errs.append(f"prompts.yaml '{key}': bad braces ({e}); write {{{{ and }}}} for a literal brace")
            continue
        for n in sorted(names - allowed):
            errs.append(f"prompts.yaml '{key}': unknown placeholder {{{n}}} "
                        f"(allowed: {', '.join('{' + a + '}' for a in sorted(allowed)) or 'none'})")
    known_top = {k.split(".")[0] for k in spec}
    for k in sorted(set(prompts) - known_top):
        warns.append(f"prompts.yaml: unknown top-level field '{k}' (ignored)")
    return errs, warns


def render(cfg, key: str, **vars: Any) -> str:
    """Fill the entry `key` from cfg.prompts. Unknown placeholders cannot reach here: the
    validator has already refused them."""
    text = lookup(cfg.prompts, key)
    if text is None:
        raise KeyError(f"prompts.yaml: missing entry '{key}'")
    return text.format(**vars)

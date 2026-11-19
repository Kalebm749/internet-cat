"""Closed moves: the world offers a menu, the brain picks one, CODE decides what it does.

Verbs live in config/moves.yaml. Each decision the engine builds the menu from what is possible right now
(the area's exits, the objects here and carried, the active projects, and sleeping when it is allowed). The
menu is in a fixed order (file order, then nearer targets) and is never ranked or marked by need. Resolving a
move gives the same shape the old open-action narrator produced (gains as buckets, effort, duration, world
changes), so everything downstream is unchanged: only code decides, and `cat/effects.py` still validates and
applies every world change. Nothing here picks or suggests a move because of a need."""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from string import Formatter
from typing import Any

from cat.config import Config

EFFORTS = ("low", "medium", "high")
TARGETS = ("none", "area", "object", "project")
REACHES = ("here", "carried", "any")
DOES = ("none", "move_self", "take", "drop", "consume", "work_on", "sleep")
ONLY_WHEN = ("always", "can_sleep")
VERB_FIELDS = {"label", "target", "reach", "any_tags", "effort", "minutes", "effects", "by_tag", "novelty",
               "does", "only_when", "outcome"}


@dataclass
class Move:
    id: str                 # the menu id shown to the brain ("m1", "m2", ...); fixed for one menu
    key: str                # stable identity: "verb" or "verb:target_id" (used for repetition and habits)
    verb: str
    target_id: str | None
    target_name: str
    label: str


# ---------- the menu ----------
def offered(cfg: Config, *, area_id: str, here: list[dict], inventory: list[dict],
            projects: list[dict], can_sleep: bool, inventory_full: bool = False) -> list[Move]:
    """Every move possible right now, in a fixed order. `here` / `inventory` are world objects,
    `projects` the active projects, `can_sleep` whether going to sleep is allowed."""
    out: list[Move] = []
    areas = cfg.world["areas"]
    for verb, v in cfg.moves["verbs"].items():
        if v.get("only_when", "always") == "can_sleep" and not can_sleep:
            continue
        if inventory_full and v.get("does") == "take":
            continue                                    # nothing to pick up with full hands
        kind = v["target"]
        targets: list[tuple[str | None, str]] = []
        if kind == "none":
            targets = [(None, "")]
        elif kind == "area":
            targets = [(a, areas[a]["name"]) for a in areas[area_id]["exits"]]
        elif kind == "project":
            targets = [(str(p["id"]), p["title"]) for p in projects]
        else:
            reach = v.get("reach", "here")
            pool = (here if reach in ("here", "any") else []) + (inventory if reach in ("carried", "any") else [])
            need = set(v.get("any_tags") or [])
            targets = [(o["id"], o["name"]) for o in pool if not need or need & set(o["tags"])]
        for tid, name in targets:
            key = verb if tid is None else f"{verb}:{tid}"
            out.append(Move("", key, verb, tid, name, v["label"].format(target=name)))
    out = out[:cfg.settings["moves"]["max_offered"]]
    for i, m in enumerate(out, 1):
        m.id = f"m{i}"
    return out


def choose(menu: list[Move], raw: Any) -> Move | None:
    """The brain's `move` value -> a Move from this menu, or None. Lenient about "m3", "3" and the key;
    nothing outside the menu is ever accepted."""
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None
    for m in menu:
        if s.lower() in (m.id.lower(), m.key.lower()):
            return m
    if s.isdigit() and 1 <= int(s) <= len(menu):
        return menu[int(s) - 1]
    return None


# ---------- resolving ----------
def _rank(cfg: Config, bucket: str) -> float:
    return float(cfg.settings["gain_values"].get(str(bucket), 0))


def _merge(cfg: Config, into: dict[str, str], more: dict[str, str]) -> None:
    for need, b in (more or {}).items():
        if need not in into or _rank(cfg, b) > _rank(cfg, into[need]):
            into[need] = str(b)


def resolve(cfg: Config, move: Move, *, target_tags: list[str], rng: random.Random, novel: bool = False,
            project_progress_bucket: str | None = None) -> dict:
    """What the move does, decided by code. Returns {"gains": {need: bucket}, "effort", "duration",
    "changes": [world ops], "does", "template": plain-words outcome}. Gains from the verb, the target's tags
    and (for going somewhere new) novelty are merged taking the largest bucket per need, so they never
    stack past one `++`."""
    v = cfg.moves["verbs"][move.verb]
    gains: dict[str, str] = {}
    _merge(cfg, gains, v.get("effects"))
    for tag in target_tags:
        _merge(cfg, gains, (v.get("by_tag") or {}).get(tag))
    if novel:
        _merge(cfg, gains, v.get("novelty"))
    lo, hi = v["minutes"]
    changes: list[dict] = []
    does = v.get("does", "none")
    if does == "move_self":
        changes.append({"op": "move_self", "area": move.target_id})
    elif does in ("take", "drop", "consume"):
        changes.append({"op": does, "object_id": move.target_id})
    return {"gains": gains, "effort": v["effort"], "duration": rng.randint(lo, hi), "changes": changes,
            "does": does, "template": v["outcome"].format(target=move.target_name),
            "project_bucket": project_progress_bucket if does == "work_on" else None}


# ---------- validation of config/moves.yaml ----------
def check(moves: Any, cfg_character: dict, settings: dict) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(moves, dict):
        return ["moves.yaml: top level must be a mapping"], []
    for k in sorted(set(moves) - {"verbs"}):
        warns.append(f"moves.yaml: unknown top-level field '{k}' (ignored)")
    verbs = moves.get("verbs")
    if not isinstance(verbs, dict) or not verbs:
        return errs + ["moves.yaml: 'verbs' must be a non-empty mapping"], warns
    needs = set(cfg_character.get("needs") or {})
    buckets = {str(k) for k in (settings.get("gain_values") or {})}
    sus = settings.get("sustenance_need")
    max_minutes = settings.get("sim_minutes_max")
    efforts = set((settings.get("effort_energy_cost") or {}) or EFFORTS)

    def gains_ok(w: str, g: Any, field: str, is_consume: bool) -> None:
        if not isinstance(g, dict):
            errs.append(f"{w}: {field} must be a mapping of need -> bucket")
            return
        for nid, b in g.items():
            if needs and nid not in needs:
                errs.append(f"{w}: {field} names unknown need '{nid}'")
            if str(b) not in buckets:
                errs.append(f"{w}: {field} bucket '{b}' is not in gain_values")
            if nid == sus and not is_consume:
                errs.append(f"{w}: only a verb with does: consume may feed '{sus}' (found it in {field})")

    for vid, v in verbs.items():
        w = f"moves.yaml verb '{vid}'"
        if not isinstance(v, dict):
            errs.append(f"{w}: must be a mapping")
            continue
        for k in sorted(set(v) - VERB_FIELDS):
            errs.append(f"{w}: unknown field '{k}'")
        label = v.get("label")
        if not isinstance(label, str) or not label.strip():
            errs.append(f"{w}: label must be non-empty text")
        else:
            try:
                bad = {f for _, f, _, _ in Formatter().parse(label) if f is not None} - {"target"}
                if bad:
                    errs.append(f"{w}: label may only use {{target}} (found {sorted(bad)})")
            except ValueError:
                errs.append(f"{w}: label has bad braces")
        out = v.get("outcome")
        if not isinstance(out, str) or not out.strip():
            errs.append(f"{w}: outcome must be non-empty text")
        tgt = v.get("target", "none")
        if tgt not in TARGETS:
            errs.append(f"{w}: target must be one of {', '.join(TARGETS)}")
        if v.get("reach", "here") not in REACHES:
            errs.append(f"{w}: reach must be one of {', '.join(REACHES)}")
        tags = v.get("any_tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            errs.append(f"{w}: any_tags must be a list of tag names")
        elif tags and tgt != "object":
            errs.append(f"{w}: any_tags only makes sense for target: object")
        if v.get("effort") not in efforts:
            errs.append(f"{w}: effort must be one of {', '.join(sorted(efforts))}")
        mins = v.get("minutes")
        if not (isinstance(mins, list) and len(mins) == 2 and all(isinstance(x, int) and not isinstance(x, bool) and x >= 1 for x in mins)
                and mins[0] <= mins[1]):
            errs.append(f"{w}: minutes must be [min, max] whole minutes, 1 <= min <= max")
        elif isinstance(max_minutes, int) and mins[1] > max_minutes:
            errs.append(f"{w}: minutes max {mins[1]} is above settings sim_minutes_max ({max_minutes})")
        does = v.get("does", "none")
        if does not in DOES:
            errs.append(f"{w}: does must be one of {', '.join(DOES)}")
        if v.get("only_when", "always") not in ONLY_WHEN:
            errs.append(f"{w}: only_when must be one of {', '.join(ONLY_WHEN)}")
        is_consume = does == "consume"
        gains_ok(w, v.get("effects", {}), "effects", is_consume)
        for tag, g in (v.get("by_tag") or {}).items() if isinstance(v.get("by_tag", {}), dict) else []:
            gains_ok(w, g, f"by_tag.{tag}", is_consume)
        if "novelty" in v:
            if tgt != "area":
                errs.append(f"{w}: novelty only makes sense for target: area")
            gains_ok(w, v["novelty"], "novelty", is_consume)
        want = {"move_self": "area", "work_on": "project", "take": "object", "drop": "object", "consume": "object"}
        if does in want and tgt != want[does]:
            errs.append(f"{w}: does: {does} needs target: {want[does]}")
        if does == "sleep" and v.get("only_when") != "can_sleep":
            errs.append(f"{w}: does: sleep must set only_when: can_sleep (it is only offered when allowed)")
    if not any(isinstance(v, dict) and v.get("target") == "none" and v.get("does", "none") == "none" for v in verbs.values()):
        errs.append("moves.yaml: needs at least one verb with target: none and does: none, so there is always something to do")
    return errs, warns

"""'Open actions, closed effects': the ONLY way the narrator changes the saved world.

Every change is validated against the real world state. Valid ones are applied; invalid ones
are returned in `rejected` (and logged) and do not happen. Changes are evaluated in order, so
a `move_self` affects how later changes in the same list are checked.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from cat.config import Config
from cat.world import WorldState

log = logging.getLogger("cat.effects")

OPS = ("move_self", "move_object", "take", "drop", "consume", "set_state", "create", "discard")


@dataclass
class EffectsResult:
    area: str
    applied: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    consumed: bool = False
    log: list[dict] = field(default_factory=list)   # every proposed change, in order, accepted or not


class Rejected(Exception):
    pass


def _str(change: dict, key: str) -> str:
    v = change.get(key)
    if not isinstance(v, str) or not v.strip():
        raise Rejected(f"missing or invalid '{key}'")
    return v.strip()


def _find(world: WorldState, ref: str) -> dict:
    """Resolve an object by id; fall back to a unique exact (case-insensitive) name match."""
    if ref in world.objects:
        return world.objects[ref]
    hits = [o for o in world.objects.values() if o["name"].lower() == ref.lower()]
    if len(hits) == 1:
        return hits[0]
    raise Rejected(f"no such object '{ref}'" if not hits else f"ambiguous object '{ref}'")


def _in_reach(world: WorldState, o: dict, area: str) -> bool:
    return o["area"] == area or o["area"] == "inventory"


def apply_changes(changes: list[dict], world: WorldState, area: str, cfg: Config,
                  now: str) -> EffectsResult:
    s = cfg.settings
    areas = cfg.world["areas"]
    res = EffectsResult(area=area)
    creates = 0

    for i, raw in enumerate(changes):
        try:
            if i >= s["max_changes_per_tick"]:
                raise Rejected(f"too many changes (max {s['max_changes_per_tick']} per tick)")
            op = raw.get("op") if isinstance(raw, dict) else None
            if op not in OPS:
                raise Rejected(f"unknown op '{op}'")
            rec: dict[str, Any] = {"op": op}

            if op == "move_self":
                target = _str(raw, "area")
                if target not in areas:
                    raise Rejected(f"unknown area '{target}'")
                if target == res.area:
                    raise Rejected("already in that area")
                if target not in areas[res.area]["exits"]:
                    raise Rejected(f"'{target}' is not connected to '{res.area}'")
                res.area = target
                rec["area"] = target
                rec["label"] = areas[target]["name"]

            elif op == "move_object":
                o = _find(world, _str(raw, "object_id"))
                target = _str(raw, "area")
                if target not in areas:
                    raise Rejected(f"unknown area '{target}'")
                if not _in_reach(world, o, res.area):
                    raise Rejected(f"'{o['id']}' is not within reach")
                if "movable" not in o["tags"]:
                    raise Rejected(f"'{o['id']}' is not movable")
                if target != res.area and target not in areas[res.area]["exits"]:
                    raise Rejected(f"'{target}' is neither here nor adjacent")
                if o["area"] == target:
                    raise Rejected("already there")
                if len(world.here(target)) >= s["max_objects_per_area"]:
                    raise Rejected(f"'{target}' is full")
                o["area"] = target
                world.touch(o["id"])
                rec.update(object_id=o["id"], area=target, label=o["name"])

            elif op == "take":
                o = _find(world, _str(raw, "object_id"))
                if o["area"] != res.area:
                    raise Rejected(f"'{o['id']}' is not here")
                if "movable" not in o["tags"]:
                    raise Rejected(f"'{o['id']}' is not movable")
                if len(world.inventory()) >= s["inventory_max"]:
                    raise Rejected("inventory is full")
                o["area"] = "inventory"
                world.touch(o["id"])
                rec.update(object_id=o["id"], label=o["name"])

            elif op == "drop":
                o = _find(world, _str(raw, "object_id"))
                if o["area"] != "inventory":
                    raise Rejected(f"'{o['id']}' is not in the inventory")
                if len(world.here(res.area)) >= s["max_objects_per_area"]:
                    raise Rejected("this area is full")
                o["area"] = res.area
                world.touch(o["id"])
                rec.update(object_id=o["id"], area=res.area, label=o["name"])

            elif op == "consume":
                o = _find(world, _str(raw, "object_id"))
                if not _in_reach(world, o, res.area):
                    raise Rejected(f"'{o['id']}' is not within reach")
                if "edible" not in o["tags"]:
                    raise Rejected(f"'{o['id']}' is not edible")
                world.remove_object(o["id"])
                res.consumed = True
                rec.update(object_id=o["id"], label=o["name"])

            elif op == "set_state":
                o = _find(world, _str(raw, "object_id"))
                key = _str(raw, "key")
                if not _in_reach(world, o, res.area):
                    raise Rejected(f"'{o['id']}' is not within reach")
                v = raw.get("value")
                if isinstance(v, bool):
                    v = str(v).lower()
                elif isinstance(v, (int, float)):
                    v = str(v)
                if not isinstance(v, str):
                    raise Rejected("invalid 'value'")
                if len(key) > s["name_max_length"] or len(v) > s["state_value_max_length"]:
                    raise Rejected("key or value too long")
                o["state"][key] = v
                world.touch(o["id"])
                rec.update(object_id=o["id"], key=key, value=v, label=o["name"])

            elif op == "create":
                name = _str(raw, "name")
                if len(name) > s["name_max_length"]:
                    raise Rejected("name too long")
                target = raw.get("area") or res.area
                if target != res.area:
                    raise Rejected("can only create things in the current area")
                if creates >= s["create_limit_per_tick"]:
                    raise Rejected(f"create limit reached ({s['create_limit_per_tick']} per tick)")
                if len(world.here(res.area)) >= s["max_objects_per_area"]:
                    raise Rejected("this area is full")
                tags_in = raw.get("tags") or []
                if not isinstance(tags_in, list):
                    raise Rejected("invalid 'tags'")
                tags = [t for t in dict.fromkeys(tags_in) if t in s["allowed_tags"]]
                st_in = raw.get("state") or {}
                if not isinstance(st_in, dict) or not all(
                        isinstance(k, str) and isinstance(v, str) and len(k) <= s["name_max_length"]
                        and len(v) <= s["state_value_max_length"] for k, v in st_in.items()):
                    raise Rejected("invalid 'state'")
                o = world.add_object(name, tags, res.area, "creature", now, st_in)
                creates += 1
                rec.update(object_id=o["id"], area=res.area, name=name, label=name, tags=tags)
                dropped = [t for t in tags_in if t not in s["allowed_tags"]]
                if dropped:
                    rec["dropped_tags"] = dropped

            elif op == "discard":
                o = _find(world, _str(raw, "object_id"))
                if not _in_reach(world, o, res.area):
                    raise Rejected(f"'{o['id']}' is not within reach")
                if o.get("created_by") != "creature":
                    raise Rejected(f"'{o['id']}' was not made by the creature: it can only throw away its own things")
                world.remove_object(o["id"])
                rec.update(object_id=o["id"], area=o["area"], label=o["name"])

            res.applied.append(rec)
            res.log.append({"op": op, "args": raw, "accepted": 1, "reason": None})
        except Rejected as e:
            entry = {"change": raw, "reason": str(e)}
            res.rejected.append(entry)
            res.log.append({"op": raw.get("op") if isinstance(raw, dict) else None, "args": raw,
                            "accepted": 0, "reason": str(e)})
            log.warning("rejected change %s: %s", raw, e)
    return res

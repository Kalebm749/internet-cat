"""Drawing operations: the only instructions the renderer accepts (docs/rewrite-contracts.md §4.10).

Coordinates are in a 512 x 512 space: x from the left edge, y from the top, both 0..512 (fractions allowed).
Each operation names a supply by id; the supply fixes the material and the colour (a red crayon is red).

    stroke  {op, tool, points: [[x, y], ...], pressure, width?, opacity?}
            pressure is 0.05..1, or one value per point
    shape   {op, tool, shape: line|rect|ellipse|polygon, pressure, width?, opacity?, and
             line: points (2) · rect: x, y, w, h · ellipse: cx, cy, rx, ry · polygon: points (3+)}
    fill    {op, tool, x, y, pressure, angle?, cross?, opacity?}
            colours the region around (x, y) bounded by existing marks; angle is the direction of the
            colouring strokes in degrees; cross (pencil) adds a second, crossing set of hatching
    erase   {op, tool (an eraser), points, pressure, width?}

`clean` checks a list of operations against the available supplies and the limits, and returns the valid ones in a
normal form plus a problem for each one it dropped. Nothing here executes anything a model wrote.
"""
from __future__ import annotations

import math
from typing import Any

from cat.art.materials import DRAWING_KINDS, KINDS

SIZE = 512.0
OPS = ("stroke", "shape", "fill", "erase")
SHAPES = ("line", "rect", "ellipse", "polygon")
COMMON = {"op", "tool", "pressure", "width", "opacity"}
FIELDS = {
    "stroke": COMMON | {"points"},
    "shape": COMMON | {"shape", "points", "x", "y", "w", "h", "cx", "cy", "rx", "ry"},
    "fill": {"op", "tool", "pressure", "opacity", "x", "y", "angle", "cross"},
    "erase": {"op", "tool", "pressure", "width", "points"},
}


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _coord(v: Any) -> float:
    if not _num(v) or not -0.0 <= v <= SIZE:
        raise ValueError(f"coordinate {v!r} is outside 0..512")
    return round(float(v), 2)


def _points(v: Any, lo: int, hi: int) -> list[list[float]]:
    if not isinstance(v, list) or not lo <= len(v) <= hi:
        raise ValueError(f"points must be a list of {lo} to {hi} [x, y] pairs")
    out = []
    for p in v:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError("each point is [x, y]")
        out.append([_coord(p[0]), _coord(p[1])])
    return out


def _pressure(v: Any, n: int | None = None) -> float | list[float]:
    if isinstance(v, list) and n is not None:
        if len(v) != n or not all(_num(x) for x in v):
            raise ValueError("a pressure list needs one number per point")
        return [round(min(1.0, max(0.05, float(x))), 3) for x in v]
    if v is None:
        return 0.6
    if not _num(v):
        raise ValueError("pressure must be a number from 0.05 to 1")
    return round(min(1.0, max(0.05, float(v))), 3)


def clean_one(op: Any, supplies: dict[str, dict], limits: dict) -> dict:
    """One operation in normal form, or ValueError with the reason."""
    if not isinstance(op, dict):
        raise ValueError("an operation must be an object")
    kind = op.get("op")
    if kind not in OPS:
        raise ValueError(f"op must be one of {', '.join(OPS)}")
    extra = set(op) - FIELDS[kind]
    if extra:
        raise ValueError(f"{kind}: unknown fields {sorted(extra)}")
    tool = supplies.get(str(op.get("tool")))
    if tool is None:
        raise ValueError(f"{kind}: tool {op.get('tool')!r} is not one of the supplies here")
    material = tool["kind"]
    if kind == "erase" and material != "eraser":
        raise ValueError("erase needs an eraser")
    if kind != "erase" and material not in DRAWING_KINDS:
        raise ValueError(f"{kind}: {tool['id']} cannot draw")
    out: dict[str, Any] = {"op": kind, "tool": tool["id"]}
    lo, hi = KINDS[material]["width_range"]
    if "width" in op:
        if not _num(op["width"]) or not lo <= op["width"] <= hi:
            raise ValueError(f"{kind}: width for {material} is {lo}..{hi}")
        out["width"] = round(float(op["width"]), 2)
    if "opacity" in op:
        if not _num(op["opacity"]) or not 0.05 <= op["opacity"] <= 1:
            raise ValueError(f"{kind}: opacity is 0.05..1")
        out["opacity"] = round(float(op["opacity"]), 3)
    max_pts = int(limits["max_points"])
    if kind in ("stroke", "erase"):
        out["points"] = _points(op.get("points"), 2, max_pts)
        out["pressure"] = _pressure(op.get("pressure"), len(out["points"]))
    elif kind == "shape":
        shape = op.get("shape")
        if shape not in SHAPES:
            raise ValueError(f"shape must be one of {', '.join(SHAPES)}")
        out["shape"] = shape
        out["pressure"] = _pressure(op.get("pressure"))
        if shape == "line":
            out["points"] = _points(op.get("points"), 2, 2)
        elif shape == "polygon":
            out["points"] = _points(op.get("points"), 3, max_pts)
        elif shape == "rect":
            x, y = _coord(op.get("x")), _coord(op.get("y"))
            w, h = op.get("w"), op.get("h")
            if not (_num(w) and _num(h) and w >= 2 and h >= 2 and x + w <= SIZE and y + h <= SIZE):
                raise ValueError("rect needs w and h of at least 2 that stay on the paper")
            out.update(x=x, y=y, w=round(float(w), 2), h=round(float(h), 2))
        else:
            cx, cy = _coord(op.get("cx")), _coord(op.get("cy"))
            rx, ry = op.get("rx"), op.get("ry")
            if not (_num(rx) and _num(ry) and 1 <= rx <= SIZE / 2 and 1 <= ry <= SIZE / 2):
                raise ValueError("ellipse needs rx and ry from 1 to 256")
            out.update(cx=cx, cy=cy, rx=round(float(rx), 2), ry=round(float(ry), 2))
    else:  # fill
        out["x"], out["y"] = _coord(op.get("x")), _coord(op.get("y"))
        out["pressure"] = _pressure(op.get("pressure"))
        if "angle" in op:
            if not _num(op["angle"]):
                raise ValueError("fill: angle is a number of degrees")
            out["angle"] = round(float(op["angle"]) % 180, 2)
        if "cross" in op:
            if not isinstance(op["cross"], bool):
                raise ValueError("fill: cross is true or false")
            out["cross"] = op["cross"]
    return out


def clean(ops: Any, supplies: list[dict], limits: dict, already: int = 0) -> tuple[list[dict], list[str]]:
    """Valid operations (normal form) and a problem per dropped one. `already`: operations the drawing already has."""
    if not isinstance(ops, list):
        return [], ["operations must be a list"]
    by_id = {s["id"]: s for s in supplies if isinstance(s, dict) and "id" in s}
    room = max(0, int(limits["max_total"]) - already)
    cap = min(int(limits["max_per_pass"]), room)
    good, probs = [], []
    total_points = 0
    for i, op in enumerate(ops):
        if len(good) >= cap:
            probs.append(f"operation {i}: over the limit of {cap} operations for this pass")
            continue
        try:
            c = clean_one(op, by_id, limits)
        except ValueError as e:
            probs.append(f"operation {i}: {e}")
            continue
        total_points += len(c.get("points", []))
        if total_points > int(limits["max_points_per_pass"]):
            probs.append(f"operation {i}: too many points in one pass")
            continue
        good.append(c)
    return good, probs

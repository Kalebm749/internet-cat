"""Render example sheets for each material (to look at the renderer, not part of the engine).

    .venv/bin/python scripts/art_samples.py --out data/art-samples
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat.art import operations, render  # noqa: E402

SUPPLIES = [
    {"id": "pencil_hb", "kind": "pencil", "name": "an HB pencil", "hardness": "HB"},
    {"id": "pencil_2b", "kind": "pencil", "name": "a 2B pencil", "hardness": "2B"},
    {"id": "pencil_4b", "kind": "pencil", "name": "a 4B pencil", "hardness": "4B"},
    {"id": "marker_black", "kind": "marker", "name": "a black marker", "color": "#1f1d22"},
    {"id": "marker_red", "kind": "marker", "name": "a red marker", "color": "#d4313a"},
    {"id": "marker_blue", "kind": "marker", "name": "a blue marker", "color": "#2a5fb8"},
    {"id": "marker_yellow", "kind": "marker", "name": "a yellow marker", "color": "#f3c623"},
    {"id": "crayon_red", "kind": "crayon", "name": "a red crayon", "color": "#c8372d"},
    {"id": "crayon_blue", "kind": "crayon", "name": "a blue crayon", "color": "#2f63b5"},
    {"id": "crayon_yellow", "kind": "crayon", "name": "a yellow crayon", "color": "#f2c43a"},
    {"id": "crayon_green", "kind": "crayon", "name": "a green crayon", "color": "#3f8f4a"},
    {"id": "eraser", "kind": "eraser", "name": "a soft eraser"},
]
LIMITS = {"max_points": 120, "max_per_pass": 400, "max_total": 1000, "max_points_per_pass": 20000}


def wave(y, x0=40, x1=470, amp=10, n=9):
    return [[x0 + (x1 - x0) * i / (n - 1), y + amp * math.sin(i * 1.3)] for i in range(n)]


def sheet_ops(tool_prefix: str) -> list[dict]:
    ops = []
    tools = [s["id"] for s in SUPPLIES if s["id"].startswith(tool_prefix)]
    y = 40
    for t in tools:                       # pressure ramps: light .. firm
        for pr in (0.2, 0.5, 0.9):
            ops.append({"op": "stroke", "tool": t, "points": wave(y), "pressure": pr})
            y += 26
    first = tools[0]
    ops.append({"op": "shape", "tool": first, "shape": "ellipse", "cx": 120, "cy": 400, "rx": 70, "ry": 55, "pressure": 0.8})
    ops.append({"op": "fill", "tool": tools[-1], "x": 120, "y": 400, "pressure": 0.7, "angle": 35})
    ops.append({"op": "shape", "tool": first, "shape": "rect", "x": 230, "y": 340, "w": 110, "h": 120, "pressure": 0.8})
    ops.append({"op": "fill", "tool": tools[min(1, len(tools) - 1)], "x": 285, "y": 400, "pressure": 0.4,
                **({"cross": True} if tool_prefix == "pencil" else {}), "angle": 120})
    # overlapping strokes build up
    for k in range(4):
        ops.append({"op": "stroke", "tool": tools[0], "points": [[380 + k * 10, 340], [470 - k * 6, 470]], "pressure": 0.7})
    ops.append({"op": "erase", "tool": "eraser", "points": [[30, 90], [480, 120]], "pressure": 0.8, "width": 14})
    return ops


def scene() -> list[dict]:
    """A small mixed drawing: pencil construction, marker outline, crayon colouring."""
    return [
        {"op": "shape", "tool": "pencil_hb", "shape": "ellipse", "cx": 256, "cy": 230, "rx": 120, "ry": 120, "pressure": 0.25},
        {"op": "stroke", "tool": "pencil_hb", "points": [[60, 400], [200, 380], [320, 395], [460, 380]], "pressure": 0.25},
        {"op": "shape", "tool": "marker_black", "shape": "ellipse", "cx": 256, "cy": 230, "rx": 118, "ry": 116, "pressure": 0.8},
        {"op": "stroke", "tool": "marker_black", "points": [[40, 402], [180, 384], [330, 398], [480, 378]], "pressure": 0.8},
        {"op": "fill", "tool": "crayon_yellow", "x": 256, "y": 230, "pressure": 0.75, "angle": 20},
        {"op": "fill", "tool": "crayon_red", "x": 256, "y": 300, "pressure": 0.35, "angle": 60},
        {"op": "fill", "tool": "crayon_blue", "x": 40, "y": 40, "pressure": 0.45, "angle": 160},
        {"op": "fill", "tool": "crayon_green", "x": 256, "y": 470, "pressure": 0.8, "angle": 10},
        {"op": "stroke", "tool": "pencil_4b", "points": [[190, 200], [210, 190], [230, 200]], "pressure": 0.9},
        {"op": "stroke", "tool": "pencil_4b", "points": [[280, 200], [300, 190], [320, 200]], "pressure": 0.9},
        {"op": "stroke", "tool": "marker_red", "points": [[200, 280], [256, 310], [312, 280]], "pressure": 0.9},
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/art-samples")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, ops in (("pencils", sheet_ops("pencil")), ("markers", sheet_ops("marker")),
                      ("crayons", sheet_ops("crayon")), ("scene", scene())):
        good, probs = operations.clean(ops, SUPPLIES, LIMITS)
        assert not probs, probs
        png, empty = render.render(good, SUPPLIES, seed=7)
        (out / f"{name}.png").write_bytes(png)
        print(name, len(good), "ops", "empty:", empty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

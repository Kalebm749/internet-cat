"""Draw a drawing: an SVG made by code from the drawing's history (cat/drawings.py).

Usually it placed its own marks at each sitting (the sketch tool: shape, place, size, angle, weight, colour), and
code draws exactly those, in a hand its state moves: anxiety shakes the line, energy makes it heavier, an
interrupted sitting shows only the marks it had time for. When a sitting has no marks of its own (the call
failed or sketching is off), code draws that sitting from the subject alone, as below.

Each sitting adds marks, seeded by the drawing and the sitting, so the same history always gives the same
picture. WHAT is drawn shapes the marks: a thing's tags give its motif (round things are rings, climbable or
standing things are uprights, wet things ripple, warm things radiate, fragile things are broken lines, soft
things are blots), a place is a horizon with forms on it, a happening is a scatter radiating from a point. HOW it
was at that sitting shapes the style: mood sets the colour (the website's presence hues), anxiety makes the line
shake, energy makes it heavier, and the minutes it spent set how many marks there are (an interrupted sitting
leaves fewer). A drawing that was started calm and finished anxious shows both."""
from __future__ import annotations

import math
import random
from html import escape

from cat.config import Config

PAPER = "#f3eee2"
MOTIFS = (("toy", "rings"), ("shiny", "rings"), ("edible", "rings"), ("climbable", "uprights"),
          ("hideable", "uprights"), ("container", "uprights"), ("board", "uprights"), ("paper", "uprights"),
          ("wet", "ripples"), ("warm", "rays"), ("fragile", "broken"), ("soft", "blots"))


def motifs(d: dict) -> list[str]:
    """Every motif its subject calls for: a thing that is shiny AND wet is drawn with rings and ripples."""
    if d.get("subject_kind") == "place":
        return ["horizon"]
    if d.get("subject_kind") == "happening":
        return ["burst"]
    tags = set(d.get("subject_tags") or [])
    return list(dict.fromkeys(m for t, m in MOTIFS if t in tags)) or ["rings"]


def motif(d: dict) -> str:
    return motifs(d)[0]


def _hue(cfg: Config, mood: float) -> float:
    p = cfg.settings["web_ui"]["presence"]
    t = (max(-50.0, min(50.0, float(mood))) + 50) / 100
    return p["hue_low_valence"] + (p["hue_high_valence"] - p["hue_low_valence"]) * t


def _wobbly(rng: random.Random, pts: list[tuple[float, float]], jitter: float) -> str:
    """A path through points, each nudged by up to `jitter` (an anxious hand shakes)."""
    out = []
    for i, (x, y) in enumerate(pts):
        x += rng.uniform(-jitter, jitter)
        y += rng.uniform(-jitter, jitter)
        out.append(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}")
    return " ".join(out)


def _mark(rng: random.Random, kind: str, w: float, h: float, jitter: float) -> str:
    cx, cy = w / 2 + rng.gauss(0, w / 8), h / 2 + rng.gauss(0, h / 8)
    if kind == "rings":
        r = rng.uniform(h / 10, h / 3.2)
        n = 14
        pts = [(cx + r * math.cos(2 * math.pi * k / n), cy + r * 0.85 * math.sin(2 * math.pi * k / n)) for k in range(n + 1)]
    elif kind == "uprights":
        x, top = rng.uniform(w * 0.2, w * 0.8), rng.uniform(h * 0.15, h * 0.45)
        pts = [(x, h * 0.85), (x + rng.uniform(-4, 4), (h * 0.85 + top) / 2), (x + rng.uniform(-6, 6), top)]
    elif kind == "ripples":
        y, x0 = rng.uniform(h * 0.3, h * 0.8), rng.uniform(w * 0.05, w * 0.3)
        pts = [(x0 + k * w / 14, y + math.sin(k * 1.2) * 5) for k in range(10)]
    elif kind == "rays":
        a, r0, r1 = rng.uniform(0, 2 * math.pi), h / 10, rng.uniform(h / 4, h / 2.2)
        pts = [(w / 2 + r0 * math.cos(a), h / 2 + r0 * math.sin(a)), (w / 2 + r1 * math.cos(a), h / 2 + r1 * math.sin(a))]
    elif kind == "broken":
        x0, y0 = rng.uniform(w * 0.15, w * 0.6), rng.uniform(h * 0.2, h * 0.8)
        pts = [(x0 + k * 9, y0 + rng.uniform(-3, 3)) for k in range(4)]
    elif kind == "blots":
        r = rng.uniform(4, h / 7)
        pts = [(cx + r * math.cos(2 * math.pi * k / 7), cy + r * math.sin(2 * math.pi * k / 7)) for k in range(8)]
    elif kind == "horizon":
        if rng.random() < 0.35:
            y = h * 0.62 + rng.uniform(-6, 6)
            pts = [(w * 0.04, y), (w * 0.5, y + rng.uniform(-4, 4)), (w * 0.96, y)]
        else:
            x, top = rng.uniform(w * 0.1, w * 0.9), rng.uniform(h * 0.25, h * 0.55)
            pts = [(x, h * 0.62), (x, top), (x + rng.uniform(8, 26), top), (x + rng.uniform(8, 26), h * 0.62)]
    else:                                                     # burst: a happening, radiating from a point
        a, r = rng.uniform(0, 2 * math.pi), rng.uniform(h / 8, h / 2.4)
        ox, oy = w * 0.55, h * 0.45
        pts = [(ox + r * 0.3 * math.cos(a), oy + r * 0.3 * math.sin(a)), (ox + r * math.cos(a), oy + r * math.sin(a))]
    return _wobbly(rng, pts, jitter)


WEIGHT = {"light": 0.9, "medium": 1.8, "heavy": 3.2}


def _poly(rng: random.Random, pts: list[tuple[float, float]], jitter: float, close: bool = False) -> str:
    path = _wobbly(rng, pts, jitter)
    return path + (" Z" if close else "")


def _own_mark(rng: random.Random, m: dict, w: float, h: float, jitter: float) -> tuple[str, bool]:
    """One of its own marks as an SVG path. Returns (path, filled)."""
    x, y, size = m["x"] / 100 * w, m["y"] / 100 * h, m["size"] / 100 * min(w, h)
    a = math.radians(m.get("angle") or 0)
    ca, sa = math.cos(a), math.sin(a)
    rot = lambda dx, dy: (x + dx * ca - dy * sa, y + dx * sa + dy * ca)
    shape = m["shape"]
    if shape == "line":
        return _poly(rng, [rot(-size / 2, 0), rot(0, 0), rot(size / 2, 0)], jitter), False
    if shape == "curve":
        return _poly(rng, [rot(-size / 2 + k * size / 8, -size * 0.35 * math.sin(math.pi * k / 8)) for k in range(9)], jitter), False
    if shape == "circle":
        r = size / 2
        return _poly(rng, [rot(r * math.cos(2 * math.pi * k / 16), r * 0.9 * math.sin(2 * math.pi * k / 16)) for k in range(17)], jitter), False
    if shape == "spiral":
        return _poly(rng, [rot(size / 2 * t / 30 * math.cos(t / 30 * 5 * math.pi), size / 2 * t / 30 * math.sin(t / 30 * 5 * math.pi))
                           for t in range(31)], jitter), False
    if shape == "wave":
        return _poly(rng, [rot(-size / 2 + k * size / 24, size * 0.12 * math.sin(k * 0.65)) for k in range(25)], jitter), False
    if shape == "scribble":
        return _poly(rng, [rot(rng.uniform(-size / 2, size / 2), rng.uniform(-size / 3, size / 3)) for _ in range(12)], jitter), False
    if shape == "rect":
        hw, hh = size / 2, size * 0.35
        return _poly(rng, [rot(-hw, -hh), rot(hw, -hh), rot(hw, hh), rot(-hw, hh)], jitter, close=True), False
    if shape == "dots":
        out = []
        for _ in range(rng.randint(6, 12)):
            cx, cy = rot(rng.uniform(-size / 2, size / 2), rng.uniform(-size / 2, size / 2))
            r = rng.uniform(0.8, 2.2)
            out.append(f"M{cx - r:.1f},{cy:.1f} a{r:.1f},{r:.1f} 0 1,0 {2 * r:.1f},0 a{r:.1f},{r:.1f} 0 1,0 {-2 * r:.1f},0")
        return " ".join(out), True
    if shape == "blot":
        r = size / 3
        return _poly(rng, [rot(r * rng.uniform(0.7, 1.15) * math.cos(2 * math.pi * k / 9),
                               r * rng.uniform(0.7, 1.15) * math.sin(2 * math.pi * k / 9)) for k in range(9)], jitter, close=True), True
    # wash: a broad, translucent sweep of colour (watercolour, or a charcoal smudge)
    return _poly(rng, [rot(size / 2 * math.cos(2 * math.pi * k / 12) * rng.uniform(0.85, 1.1),
                           size * 0.3 * math.sin(2 * math.pi * k / 12) * rng.uniform(0.85, 1.1)) for k in range(12)],
                 jitter * 0.5, close=True), True


def _own_marks(cfg: Config, d: dict, i: int, st: dict, w: float, h: float, jitter: float, energy_k: float) -> list[str]:
    """A sitting where it placed its own marks: washes first (as a painter would), then the rest. An interrupted
    sitting shows only the share of its marks it had time for."""
    colours = {**cfg.settings["drawing"]["colours"]["charcoal"], **cfg.settings["drawing"]["colours"]["paints"]}
    marks = list(st["marks"])
    planned = float(st.get("planned") or st.get("minutes") or 1)
    keep = max(1, math.ceil(len(marks) * min(1.0, float(st.get("minutes") or 0) / planned))) if planned else len(marks)
    marks = sorted(marks[:keep], key=lambda m: m["shape"] != "wash")
    out = []
    for k, m in enumerate(marks):
        rng = random.Random(f"drawing|{d.get('id')}|{i}|{k}")
        path, filled = _own_mark(rng, m, w, h, jitter)
        col = colours.get(m["colour"], "#333")
        if filled:
            op = 0.28 if m["shape"] == "wash" else 0.75
            out.append(f'<path d="{path}" fill="{col}" stroke="none" opacity="{op}"/>')
        else:
            out.append(f'<path d="{path}" stroke="{col}" stroke-width="{WEIGHT.get(m["weight"], 1.8) * energy_k:.2f}" '
                       f'fill="none" stroke-linecap="round" stroke-linejoin="round" opacity="0.85"/>')
    return out


def render_svg(cfg: Config, d: dict) -> str:
    """The picture for one drawing row (with `sittings`, `subject_kind`, `subject_tags`)."""
    ui = cfg.settings["web_ui"]["drawing"]
    w, h = float(ui["width"]), float(ui["height"])
    per = float(cfg.settings["drawing"]["marks_per_sitting"])
    ref = float(cfg.settings["moves"]["reference_minutes"])
    kinds = motifs(d)
    paths = []
    for i, st in enumerate(d.get("sittings") or []):
        rng = random.Random(f"drawing|{d.get('id')}|{i}")
        n = max(1, round(per * float(st.get("minutes") or 0) / ref))
        hue = _hue(cfg, st.get("mood") or 0.0)
        jitter = float(ui["jitter_max"]) * max(0.0, min(100.0, float(st.get("anxiety") or 0))) / 100 + 0.4
        width = 0.8 + 1.6 * (max(-50.0, min(50.0, float(st.get("energy") or 0))) + 50) / 100
        colour = f"hsl({hue:.0f},38%,{30 + 6 * (i % 2)}%)"
        if st.get("marks"):                                   # it placed its own marks: draw those, in its hand
            paths += _own_marks(cfg, d, i, st, w, h, jitter, 0.7 + 0.5 * (width - 0.8) / 1.6)
            continue
        for _ in range(n):
            paths.append(f'<path d="{_mark(rng, rng.choice(kinds), w, h, jitter)}" stroke="{colour}" stroke-width="{width:.2f}" '
                         f'fill="none" stroke-linecap="round" stroke-linejoin="round" opacity="0.72"/>')
    label = escape(f"{d.get('title') or d.get('subject_name') or 'a drawing'}: "
                   f"{len(d.get('sittings') or [])} sitting(s)")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="{label}" '
            f'class="drawing-art"><rect width="{w:.0f}" height="{h:.0f}" fill="{PAPER}"/>' + "".join(paths) + "</svg>")

"""Draw a drawing: an SVG made by code from the drawing's history (cat/drawings.py).

At each sitting it placed its own marks (the sketch tool: shape, place, size, angle, weight, colour, layer, and
freehand points for contours). Code draws exactly those, and makes them look like the tool that made them:

- paper has a grain the media sit on;
- charcoal (the charcoal greys) is several rough strands per stroke with tapered ends, broken by the grain,
  with a soft smudge under heavy marks; areas are built up by hatching, not flat fills;
- watercolour (the paints) bleeds at irregular edges, darkens where the pigment pools at the rim, and builds up
  in translucent glazes; brush strokes taper;
- depth: a mark's layer sets how far away it sits (back marks paler and softer, front marks crisp and dark).

Its state moves the hand: anxiety shakes the line, energy makes it heavier, an interrupted sitting shows only
the marks it had time for. A sitting without marks of its own (the call failed, or sketching is off) is drawn
in charcoal from the subject alone: what it is (the thing's tags) chooses the shapes. Same history, same picture."""
from __future__ import annotations

import math
import random
from html import escape

from cat.config import Config

PAPER = "#f3eee2"
MOTIFS = (("toy", "rings"), ("shiny", "rings"), ("edible", "rings"), ("climbable", "uprights"),
          ("hideable", "uprights"), ("container", "uprights"), ("board", "uprights"), ("paper", "uprights"),
          ("wet", "ripples"), ("warm", "rays"), ("fragile", "broken"), ("soft", "blots"))
WEIGHT = {"light": 1.0, "medium": 1.9, "heavy": 3.2}
LAYER = {"back": (0.55, "back"), "middle": (0.85, None), "front": (1.0, None)}     # opacity, filter

Pt = tuple[float, float]


# ---------- which motifs a subject calls for (used when a sitting has no marks of its own) ----------
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


def _fallback_marks(rng: random.Random, d: dict, n: int) -> list[dict]:
    """Code's own marks for a sitting it did not place: charcoal, shaped by what the subject is."""
    shape = {"rings": "circle", "uprights": "line", "ripples": "wave", "rays": "line", "broken": "line",
             "blots": "blot", "horizon": "line", "burst": "line"}
    out = []
    for _ in range(n):
        k = rng.choice(motifs(d))
        m = {"shape": shape[k], "x": rng.gauss(50, 14), "y": rng.gauss(55, 12), "size": rng.uniform(10, 40),
             "angle": rng.uniform(0, 360), "weight": rng.choice(["light", "medium"]),
             "colour": rng.choice(["black", "dark grey", "grey"]), "layer": rng.choice(["back", "middle", "front"])}
        if k == "uprights":
            m["angle"] = 90 + rng.uniform(-6, 6)
        elif k in ("ripples", "horizon"):
            m["angle"] = rng.uniform(-4, 4)
            m["size"] = rng.uniform(40, 90)
        elif k == "broken":
            m["size"] = rng.uniform(4, 12)
        out.append(m)
    return out


# ---------- geometry ----------
def _spline(pts: list[Pt], closed: bool = False, per: int = 6) -> list[Pt]:
    """Catmull-Rom through the points, sampled: a hand moves in curves, not corners."""
    if len(pts) < 3:
        if len(pts) == 2:
            (x0, y0), (x1, y1) = pts
            return [(x0 + (x1 - x0) * t / per, y0 + (y1 - y0) * t / per) for t in range(per + 1)]
        return list(pts)
    P = pts + pts[:3] if closed else [pts[0]] + pts + [pts[-1]]
    out: list[Pt] = []
    for i in range(1, (len(pts) + 1) if closed else (len(P) - 2)):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        for s in range(per):
            t = s / per
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * ((2 * p1[k]) + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                                    + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3) for k in (0, 1)))
    out.append(out[0] if closed else P[-2])
    return out


def _shake(rng: random.Random, pts: list[Pt], jitter: float) -> list[Pt]:
    return [(x + rng.uniform(-jitter, jitter), y + rng.uniform(-jitter, jitter)) for x, y in pts]


def _ribbon(pts: list[Pt], width: float, rng: random.Random, taper: float = 0.75) -> str:
    """A stroke as a filled shape whose width swells and tapers, as charcoal or a brush does under pressure."""
    if len(pts) < 2:
        return ""
    n = len(pts)
    left, right = [], []
    for i, (x, y) in enumerate(pts):
        a, b = pts[max(0, i - 1)], pts[min(n - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / ln, dx / ln
        t = i / (n - 1)
        wv = width * ((1 - taper) + taper * math.sin(math.pi * t) ** 0.6) * rng.uniform(0.85, 1.15) / 2
        left.append((x + nx * wv, y + ny * wv))
        right.append((x - nx * wv, y - ny * wv))
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in left + right[::-1]) + " Z"


def _closed(pts: list[Pt]) -> str:
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"


def _geometry(rng: random.Random, m: dict, w: float, h: float) -> tuple[str, list[list[Pt]]]:
    """A mark's centre lines (or outline) on the sheet: ("stroke"|"area"|"dots"|"hatch", [point lists])."""
    x, y, size = m["x"] / 100 * w, m["y"] / 100 * h, max(2.0, m["size"] / 100 * min(w, h))
    a = math.radians(m.get("angle") or 0)
    ca, sa = math.cos(a), math.sin(a)
    rot = lambda dx, dy: (x + dx * ca - dy * sa, y + dx * sa + dy * ca)
    shape = m["shape"]
    pts_in = [(px / 100 * w, py / 100 * h) for px, py in (m.get("points") or [])]
    if len(pts_in) >= 2 and shape in ("line", "curve", "outline"):
        return "stroke", [_spline(pts_in, closed=shape == "outline" and len(pts_in) >= 3)]
    if shape == "line":
        return "stroke", [_spline([rot(-size / 2, 0), rot(0, rng.uniform(-1.5, 1.5)), rot(size / 2, 0)])]
    if shape == "curve":
        return "stroke", [_spline([rot(-size / 2, 0), rot(-size / 6, -size * 0.3), rot(size / 6, -size * 0.3), rot(size / 2, 0)])]
    if shape in ("circle", "outline"):
        r = size / 2
        return "stroke", [_spline([rot(r * math.cos(2 * math.pi * k / 7) * rng.uniform(0.92, 1.06),
                                       r * 0.9 * math.sin(2 * math.pi * k / 7) * rng.uniform(0.92, 1.06)) for k in range(7)],
                                  closed=True)]
    if shape == "spiral":
        return "stroke", [[rot(size / 2 * t / 40 * math.cos(t / 40 * 5 * math.pi), size / 2 * t / 40 * math.sin(t / 40 * 5 * math.pi))
                           for t in range(41)]]
    if shape == "wave":
        return "stroke", [[rot(-size / 2 + k * size / 30, size * 0.1 * math.sin(k * 0.55)) for k in range(31)]]
    if shape == "scribble":
        return "stroke", [_spline([rot(rng.uniform(-size / 2, size / 2), rng.uniform(-size / 3, size / 3)) for _ in range(9)])]
    if shape == "rect":
        hw, hh = size / 2, size * 0.35
        return "stroke", [_spline([rot(-hw, -hh), rot(hw, -hh), rot(hw, hh), rot(-hw, hh), rot(-hw, -hh)], per=2)]
    if shape in ("hatch", "crosshatch"):
        lines = []
        for ang in ((0.0,) if shape == "hatch" else (0.0, math.pi / 2.6)):
            c2, s2 = math.cos(a + ang), math.sin(a + ang)
            n = max(3, int(size / 3.2))
            for k in range(n):
                off = -size / 2 + size * (k + 0.5) / n
                half = math.sqrt(max(0.0, (size / 2) ** 2 - off ** 2)) * rng.uniform(0.7, 1.0)
                cx, cy = x - off * s2, y + off * c2
                lines.append(_spline([(cx - half * c2, cy - half * s2), (cx + half * c2, cy + half * s2)], per=4))
        return "hatch", lines
    if shape in ("dots", "stipple"):
        n = rng.randint(8, 14) if shape == "dots" else rng.randint(30, 60)
        return "dots", [[rot(rng.gauss(0, size / 4), rng.gauss(0, size / 4)) for _ in range(n)]]
    r = size / (3 if shape == "blot" else 2)                     # blot, wash
    sq = 0.4 if shape == "wash" else 1.0
    return "area", [_spline([rot(r * math.cos(2 * math.pi * k / 9) * rng.uniform(0.75, 1.15),
                                 r * sq * math.sin(2 * math.pi * k / 9) * rng.uniform(0.75, 1.15)) for k in range(9)],
                            closed=True, per=4)]


# ---------- the media ----------
def _defs(seed: int) -> str:
    """Paper grain, charcoal grain, watercolour bleed, distance and smudge, as SVG filters."""
    return (
        "<defs>"
        f'<filter id="paper" x="0" y="0" width="100%" height="100%"><feTurbulence type="fractalNoise" baseFrequency="0.85" '
        f'numOctaves="2" seed="{seed}"/><feColorMatrix values="0 0 0 0 0.42  0 0 0 0 0.38  0 0 0 0 0.32  0 0 0 0.16 0"/></filter>'
        f'<filter id="charcoal" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="1.1" '
        f'numOctaves="2" seed="{seed + 1}" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="2.6" result="d"/>'
        '<feColorMatrix in="n" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -2.4 1.75" result="g"/>'
        '<feComposite in="d" in2="g" operator="in"/></filter>'
        f'<filter id="wet" x="-15%" y="-15%" width="130%" height="130%"><feTurbulence type="fractalNoise" baseFrequency="0.035" '
        f'numOctaves="3" seed="{seed + 2}" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="13"/>'
        '<feGaussianBlur stdDeviation="0.6"/></filter>'
        f'<filter id="rim" x="-15%" y="-15%" width="130%" height="130%"><feTurbulence type="fractalNoise" baseFrequency="0.035" '
        f'numOctaves="3" seed="{seed + 2}" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="9"/>'
        '<feGaussianBlur stdDeviation="1.1"/></filter>'
        '<filter id="smudge" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="2.4"/></filter>'
        '<filter id="back" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur stdDeviation="0.9"/></filter>'
        "</defs>")


def _charcoal(rng: random.Random, kind: str, lines: list[list[Pt]], col: str, width: float, op: float,
              jitter: float) -> list[str]:
    out = []
    if kind == "area":                                            # rubbed in: a smudge, then hatching on top
        ring = _shake(rng, lines[0], jitter)
        out.append(f'<path d="{_closed(ring)}" fill="{col}" opacity="{0.22 * op:.2f}" filter="url(#smudge)"/>')
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        cx, cy, r = sum(xs) / len(xs), sum(ys) / len(ys), (max(xs) - min(xs) + max(ys) - min(ys)) / 4
        c2, s2 = math.cos(0.6), math.sin(0.6)
        n = max(3, int(r / 2.2))
        for k in range(n):
            off = -r + 2 * r * (k + 0.5) / n
            half = math.sqrt(max(0.0, r * r - off * off)) * rng.uniform(0.6, 0.95)
            a0, b0 = cx - off * s2, cy + off * c2
            seg = _shake(rng, _spline([(a0 - half * c2, b0 - half * s2), (a0 + half * c2, b0 + half * s2)], per=4), jitter / 2)
            out.append(f'<path d="{_ribbon(seg, width * 0.8, rng)}" fill="{col}" opacity="{0.75 * op:.2f}" filter="url(#charcoal)"/>')
        return out
    if kind == "dots":
        for x, y in _shake(rng, lines[0], jitter):
            r = rng.uniform(0.5, 1.4) * width / 1.4
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.2f}" fill="{col}" opacity="{0.8 * op:.2f}" filter="url(#charcoal)"/>')
        return out
    strands = 1 if kind == "hatch" else 3
    for line in lines:
        base = _shake(rng, line, jitter)
        if width > 2.0 and kind != "hatch":                       # a heavy stroke leaves a smudge
            out.append(f'<path d="{_ribbon(base, width * 3.2, rng)}" fill="{col}" opacity="{0.12 * op:.2f}" filter="url(#smudge)"/>')
        for s in range(strands):
            off = (s - (strands - 1) / 2) * width * 0.45
            pts = [(x + rng.uniform(-0.4, 0.4) + off * 0.3, y + rng.uniform(-0.4, 0.4) + off) for x, y in base]
            out.append(f'<path d="{_ribbon(pts, width * rng.uniform(0.55, 0.9), rng)}" fill="{col}" '
                       f'opacity="{rng.uniform(0.7, 0.95) * op:.2f}" filter="url(#charcoal)"/>')
    return out


def _paint(rng: random.Random, kind: str, lines: list[list[Pt]], col: str, width: float, op: float,
           jitter: float) -> list[str]:
    out = []
    if kind == "area":                                            # a wash: glazes that bleed, pooling at the rim
        for _ in range(3):
            ring = _shake(rng, [(x + rng.uniform(-2, 2), y + rng.uniform(-2, 2)) for x, y in lines[0]], jitter / 2)
            out.append(f'<path d="{_closed(ring)}" fill="{col}" opacity="{rng.uniform(0.13, 0.22) * op:.2f}" filter="url(#wet)"/>')
        out.append(f'<path d="{_closed(lines[0])}" fill="none" stroke="{col}" stroke-width="1.6" '
                   f'opacity="{0.5 * op:.2f}" filter="url(#rim)"/>')
        return out
    if kind == "dots":                                            # drops and spatters
        for x, y in _shake(rng, lines[0], jitter):
            r = rng.uniform(0.8, 2.6)
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.2f}" fill="{col}" opacity="{0.45 * op:.2f}" filter="url(#wet)"/>')
        return out
    for line in lines:                                            # a brush stroke: one tapered, wet body
        out.append(f'<path d="{_ribbon(_shake(rng, line, jitter), width * 1.8, rng, taper=0.9)}" fill="{col}" '
                   f'opacity="{0.55 * op:.2f}" filter="url(#wet)"/>')
    return out


def _sitting(cfg: Config, d: dict, i: int, st: dict, marks: list[dict], w: float, h: float) -> list[str]:
    ui = cfg.settings["web_ui"]["drawing"]
    cols = cfg.settings["drawing"]["colours"]
    charcoal, paints = cols["charcoal"], cols["paints"]
    jitter = float(ui["jitter_max"]) * max(0.0, min(100.0, float(st.get("anxiety") or 0))) / 100 * 0.6 + 0.3
    energy_k = 0.75 + 0.5 * (max(-50.0, min(50.0, float(st.get("energy") or 0))) + 50) / 100
    order = {"back": 0, "middle": 1, "front": 2}
    marks = sorted(marks, key=lambda m: (order.get(m.get("layer") or "middle", 1), m["shape"] not in ("wash", "blot")))
    out: list[str] = []
    for k, m in enumerate(marks):
        rng = random.Random(f"drawing|{d.get('id')}|{i}|{k}")
        kind, lines = _geometry(rng, m, w, h)
        op, filt = LAYER.get(m.get("layer") or "middle", LAYER["middle"])
        width = WEIGHT.get(m.get("weight") or "medium", 1.4) * energy_k
        if m["colour"] in paints:
            parts = _paint(rng, kind, lines, paints[m["colour"]], width, op, jitter)
        else:
            parts = _charcoal(rng, kind, lines, charcoal.get(m["colour"], "#2a2725"), width, op, jitter)
        out.append(f'<g filter="url(#{filt})">' + "".join(parts) + "</g>" if filt else "".join(parts))
    return out


def render_svg(cfg: Config, d: dict) -> str:
    """The picture for one drawing row (with `sittings`, `subject_kind`, `subject_tags`)."""
    ui = cfg.settings["web_ui"]["drawing"]
    w, h = float(ui["width"]), float(ui["height"])
    per = float(cfg.settings["drawing"]["marks_per_sitting"])
    ref = float(cfg.settings["moves"]["reference_minutes"])
    seed = int(d.get("id") or 0) % 997 + 1
    body: list[str] = []
    for i, st in enumerate(d.get("sittings") or []):
        if st.get("marks"):
            marks = list(st["marks"])
            planned = float(st.get("planned") or st.get("minutes") or 1)
            keep = max(1, math.ceil(len(marks) * min(1.0, float(st.get("minutes") or 0) / planned)))
            marks = marks[:keep]                                  # an interrupted sitting: only what it had time for
        else:
            n = max(1, round(per * float(st.get("minutes") or 0) / ref))
            marks = _fallback_marks(random.Random(f"drawing|{d.get('id')}|{i}"), d, n)
        body += _sitting(cfg, d, i, st, marks, w, h)
    label = escape(f"{d.get('title') or d.get('subject_name') or 'a drawing'}: "
                   f"{len(d.get('sittings') or [])} sitting(s)")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="{label}" '
            f'class="drawing-art">{_defs(seed)}<rect width="{w:.0f}" height="{h:.0f}" fill="{PAPER}"/>'
            f'<rect width="{w:.0f}" height="{h:.0f}" filter="url(#paper)"/>' + "".join(body) + "</svg>")

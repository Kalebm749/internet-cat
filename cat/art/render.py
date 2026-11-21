"""A small physical drawing simulator: paper, graphite, marker, crayon and an eraser (docs/rewrite-contracts.md §4.10).

The sheet keeps more than colour: the paper's *tooth* (its height map) and how much graphite, marker ink and crayon
wax lie on each spot. Materials interact with these the way the real ones do:

    pencil  thin, slightly wavering lines; darkness from pressure and hardness (H..6B); graphite catches on the
            tooth and builds up where strokes overlap (multiplied, never painted over); fills are hatching
    marker  near-uniform dye of consistent width with slightly ragged edges and a darker pool where the tip
            rests; overlaps darken (dye on dye); fills are side-by-side passes with faint banding
    crayon  wax that only reaches the tooth peaks the pressure allows, in streaks that follow the stroke;
            light pressure leaves broken, grainy colour, heavy pressure a waxy, nearly opaque layer that covers
            what is under it; fills are directional scribbles
    eraser  lifts graphite well, crayon partly and marker barely, leaving a faint ghost and slightly scuffing
            the tooth

Everything random is seeded from the sheet's seed and the operation's position, so replaying the same operations
on the same seed gives the same pixels. No GPU; NumPy and Pillow only.
"""
from __future__ import annotations

import io
import math
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from cat.art.materials import GRAPHITE, KINDS, PENCILS, hex_rgb

PAPER = np.array([0.965, 0.946, 0.905], dtype=np.float32)
SS = 2                       # supersampling for line masks
GAP = 5                      # gaps in an outline up to about this many px still hold a fill in


def _rng(seed: int, *salt: int) -> np.random.Generator:
    return np.random.default_rng([seed & 0xFFFFFFFF, *salt])


def _smooth_noise(rng: np.random.Generator, h: int, w: int, cell: float) -> np.ndarray:
    """Value noise: random values on a coarse grid, smoothly upscaled. Range about 0..1."""
    gh, gw = max(2, int(h / cell) + 2), max(2, int(w / cell) + 2)
    small = rng.random((gh, gw)).astype(np.float32)
    img = Image.fromarray((small * 255).astype(np.uint8)).resize((int(gw * cell), int(gh * cell)), Image.BICUBIC)
    a = np.asarray(img, dtype=np.float32)[:h, :w] / 255.0
    return a


def _fbm(rng: np.random.Generator, h: int, w: int, cells=(64, 24, 8, 3), weights=(0.35, 0.3, 0.2, 0.15)) -> np.ndarray:
    out = np.zeros((h, w), np.float32)
    for c, wt in zip(cells, weights):
        out += wt * _smooth_noise(rng, h, w, c)
    out -= out.min()
    return out / max(1e-6, out.max())


def _smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _catmull(points: np.ndarray, per: int = 8) -> np.ndarray:
    """A smooth curve through the points (hand movement, not a polyline)."""
    if len(points) < 3:
        n = max(2, int(np.linalg.norm(points[-1] - points[0]) / 2) + 2)
        t = np.linspace(0, 1, n)[:, None]
        return points[0] * (1 - t) + points[-1] * t
    p = np.vstack([points[0], points, points[-1]])
    out = []
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        seg = max(2, int(np.linalg.norm(p2 - p1) / 2) + 1)
        for t in np.linspace(0, 1, seg, endpoint=False):
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    out.append(points[-1])
    return np.asarray(out, np.float32)


def _tremor(rng: np.random.Generator, pts: np.ndarray, amount: float) -> np.ndarray:
    """A hand is never perfectly steady: a slow sideways wander along the line."""
    if len(pts) < 3 or amount <= 0:
        return pts
    d = np.gradient(pts, axis=0)
    norm = np.linalg.norm(d, axis=1, keepdims=True) + 1e-6
    perp = np.stack([-d[:, 1], d[:, 0]], 1) / norm
    n = len(pts)
    knots = rng.normal(0, 1, max(3, n // 24 + 3))
    wander = np.interp(np.linspace(0, len(knots) - 1, n), np.arange(len(knots)), knots)
    jitter = rng.normal(0, 0.15, n)
    return pts + perp * (wander * amount + jitter * amount)[:, None]


class Sheet:
    """One sheet of paper and everything on it."""

    def __init__(self, seed: int, size: int = 512):
        self.seed, self.size = int(seed), int(size)
        rng = _rng(self.seed, 0)
        h = w = self.size
        self.tooth = _fbm(rng, h, w, cells=(48, 12, 4, 1.6), weights=(0.2, 0.25, 0.3, 0.25))
        fine = _smooth_noise(rng, h, w, 1.2)
        mottling = _smooth_noise(rng, h, w, 96)
        base = PAPER[None, None, :] * (1 - 0.028 * (self.tooth[..., None] - 0.5) - 0.012 * (fine[..., None] - 0.5)
                                       - 0.018 * (mottling[..., None] - 0.5))
        self.paper = self._fibers(rng, base.astype(np.float32))
        self.rgb = self.paper.copy()
        self.graphite = np.zeros((h, w), np.float32)
        self.ink = np.zeros((h, w), np.float32)
        self.wax = np.zeros((h, w), np.float32)
        self.lines = np.zeros((h, w), np.float32)          # drawn lines (not colouring): what holds a fill in
        self.ops_done = 0

    def _fibers(self, rng: np.random.Generator, base: np.ndarray) -> np.ndarray:
        """Faint short fibres in the paper, a little lighter or darker than the sheet."""
        img = Image.new("L", (self.size, self.size), 128)
        d = ImageDraw.Draw(img)
        for _ in range(int(self.size * self.size / 900)):
            x, y = rng.random(2) * self.size
            ang, length = rng.random() * math.pi, 4 + rng.random() * 14
            bend = rng.normal(0, 0.5)
            pts = [(x + math.cos(ang + bend * t) * length * t, y + math.sin(ang + bend * t) * length * t)
                   for t in np.linspace(0, 1, 5)]
            d.line(pts, fill=int(128 + rng.choice([-1, 1]) * rng.integers(6, 16)), width=1)
        f = (np.asarray(img.filter(ImageFilter.GaussianBlur(0.6)), np.float32) - 128) / 128
        return np.clip(base * (1 + 0.035 * f[..., None]), 0, 1)

    # ---------------------------------------------------------------- masks

    def _window(self, pts: np.ndarray, margin: float) -> tuple[int, int, int, int]:
        x0 = int(max(0, math.floor(pts[:, 0].min() - margin)))
        y0 = int(max(0, math.floor(pts[:, 1].min() - margin)))
        x1 = int(min(self.size, math.ceil(pts[:, 0].max() + margin)))
        y1 = int(min(self.size, math.ceil(pts[:, 1].max() + margin)))
        return x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)

    def _line_mask(self, pts: np.ndarray, widths: np.ndarray, values: np.ndarray, win, blur: float) -> np.ndarray:
        """Draw a varying-width line (values 0..1 along it) into a mask over the window, supersampled."""
        x0, y0, x1, y1 = win
        W, H = (x1 - x0) * SS, (y1 - y0) * SS
        img = Image.new("L", (max(1, W), max(1, H)), 0)
        d = ImageDraw.Draw(img)
        p = (pts - np.array([x0, y0], np.float32)) * SS
        for i in range(len(p) - 1):
            wdt = max(1, int(round((widths[i] + widths[i + 1]) / 2 * SS)))
            v = int(round(255 * float(values[i])))
            d.line([tuple(p[i]), tuple(p[i + 1])], fill=v, width=wdt)
            r = wdt / 2
            d.ellipse([p[i][0] - r, p[i][1] - r, p[i][0] + r, p[i][1] + r], fill=v)
        if blur > 0:
            img = img.filter(ImageFilter.GaussianBlur(blur * SS))
        img = img.resize((x1 - x0, y1 - y0), Image.BOX)
        return np.asarray(img, np.float32) / 255.0

    def _path(self, op: dict, rng: np.random.Generator, material: str, width: float):
        """Points, widths and pressures along the hand's path for a stroke-like operation."""
        raw = np.asarray(op["points"], np.float32)
        pres = op["pressure"]
        pres = np.asarray(pres if isinstance(pres, list) else [pres] * len(raw), np.float32)
        pts = _catmull(raw)
        t_raw = np.linspace(0, 1, len(raw))
        t = np.linspace(0, 1, len(pts))
        p = np.interp(t, t_raw, pres)
        pts = _tremor(rng, pts, KINDS[material]["tremor"])
        return pts, p

    # ---------------------------------------------------------------- materials

    def _deposit_multiply(self, win, alpha: np.ndarray, color) -> None:
        x0, y0, x1, y1 = win
        c = np.asarray(color, np.float32)
        sub = self.rgb[y0:y1, x0:x1]
        sub *= 1 - alpha[..., None] * (1 - c[None, None, :])

    def _deposit_cover(self, win, alpha: np.ndarray, color) -> None:
        x0, y0, x1, y1 = win
        c = np.asarray(color, np.float32)
        sub = self.rgb[y0:y1, x0:x1]
        sub += (c[None, None, :] - sub) * alpha[..., None]

    def pencil(self, op: dict, supply: dict, rng: np.random.Generator) -> None:
        spec = PENCILS[supply.get("hardness", "HB")]
        width = float(op.get("width", spec.width))
        pts, p = self._path(op, rng, "pencil", width)
        win = self._window(pts, width * 2 + 3)
        widths = width * (0.55 + 0.6 * p)
        mask = self._line_mask(pts, widths, np.clip(0.35 + 0.65 * p, 0, 1), win, blur=0.35 * spec.smudge)
        self._graphite(win, mask, spec.darkness * float(op.get("opacity", 1.0)), rng)
        self._line(win, mask * spec.darkness)

    def _graphite(self, win, mask: np.ndarray, darkness: float, rng: np.random.Generator) -> None:
        x0, y0, x1, y1 = win
        tooth = self.tooth[y0:y1, x0:x1]
        grain = np.clip(0.35 + 1.1 * tooth + rng.normal(0, 0.08, tooth.shape).astype(np.float32), 0.15, 1.25)
        # a firm line still fills the valleys a little; a light one only touches the peaks
        catch = np.where(mask > 0.6, 0.55 + 0.45 * grain, grain)
        alpha = np.clip(mask * darkness * catch, 0, 0.97)
        already = self.graphite[y0:y1, x0:x1]
        alpha *= 1 - 0.35 * np.clip(already, 0, 1)          # burnished graphite takes less
        self._deposit_multiply(win, alpha, GRAPHITE)
        self.graphite[y0:y1, x0:x1] = np.clip(already + alpha, 0, 2)

    def marker(self, op: dict, supply: dict, rng: np.random.Generator) -> None:
        width = float(op.get("width", supply.get("width", KINDS["marker"]["width"])))
        pts, p = self._path(op, rng, "marker", width)
        win = self._window(pts, width + 4)
        widths = width * (0.85 + 0.2 * p)                    # a felt tip barely changes with pressure
        mask = self._line_mask(pts, widths, np.ones(len(pts)), win, blur=0.45)
        self._marker_ink(win, mask, hex_rgb(supply["color"]), float(op.get("opacity", 1.0)), rng)
        self._line(win, mask)
        # the tip rests a moment at the start and the end: a slightly darker pool
        for q in (pts[0], pts[-1]):
            pool = self._line_mask(np.stack([q, q + 0.01]), np.array([width * 0.9] * 2), np.array([0.35, 0.35]),
                                   win, blur=0.8)
            self._deposit_multiply(win, np.clip(pool, 0, 1) * 0.5 * (1 - 0.6 * self._lum_color(supply)),
                                   hex_rgb(supply["color"]))

    @staticmethod
    def _lum_color(supply: dict) -> float:
        r, g, b = hex_rgb(supply["color"])
        return 0.3 * r + 0.59 * g + 0.11 * b

    def _marker_ink(self, win, mask: np.ndarray, color, opacity: float, rng: np.random.Generator) -> None:
        x0, y0, x1, y1 = win
        h, w = mask.shape
        edge = _smooth_noise(rng, h, w, 3.0)
        # crisp but slightly ragged edge: the dye wicks a little into the paper fibres
        m = _smoothstep(0.32, 0.55, mask + 0.12 * (edge - 0.5) + 0.04 * (self.tooth[y0:y1, x0:x1] - 0.5))
        streak = 1 - 0.05 * _smooth_noise(rng, h, w, 6.0)
        alpha = np.clip(m * KINDS["marker"]["alpha"] * opacity * streak, 0, 0.95)
        self._deposit_multiply(win, alpha, color)
        self.ink[y0:y1, x0:x1] = np.clip(self.ink[y0:y1, x0:x1] + alpha, 0, 2)

    def crayon(self, op: dict, supply: dict, rng: np.random.Generator) -> None:
        width = float(op.get("width", supply.get("width", KINDS["crayon"]["width"])))
        pts, p = self._path(op, rng, "crayon", width)
        win = self._window(pts, width + 6)
        # a crayon tip wears into an uneven flat: the width wobbles more than a marker's
        wob = 1 + 0.18 * np.interp(np.linspace(0, 1, len(pts)), np.linspace(0, 1, 7), rng.normal(0, 1, 7))
        widths = width * (0.75 + 0.35 * p) * wob
        mask = self._line_mask(pts, widths, np.clip(0.4 + 0.6 * p, 0, 1), win, blur=0.6)
        d = pts[-1] - pts[0]
        angle = math.degrees(math.atan2(d[1], d[0])) if np.linalg.norm(d) > 1 else 0.0
        self._wax(win, mask, hex_rgb(supply["color"]), float(np.mean(p)), angle, float(op.get("opacity", 1.0)), rng)
        self._line(win, mask)

    def _line(self, win, mask: np.ndarray) -> None:
        x0, y0, x1, y1 = win
        self.lines[y0:y1, x0:x1] = np.maximum(self.lines[y0:y1, x0:x1], mask)

    def _streaks(self, rng: np.random.Generator, h: int, w: int, angle: float, spacing: float) -> np.ndarray:
        """Directional noise: long thin streaks running at `angle` degrees (wax dragged across paper)."""
        n = int(math.ceil(math.hypot(h, w))) + 8
        small = rng.random((max(2, int(n / spacing)), max(2, int(n / (spacing * 9))))).astype(np.float32)
        img = Image.fromarray((small * 255).astype(np.uint8)).resize((max(2, int(n / (spacing * 9))) * int(spacing * 9),
                                                                      max(2, int(n / spacing)) * int(spacing)),
                                                                     Image.BICUBIC)
        img = img.rotate(-angle, resample=Image.BILINEAR, expand=False)
        a = np.asarray(img, np.float32) / 255.0
        cy, cx = (a.shape[0] - h) // 2, (a.shape[1] - w) // 2
        return a[max(0, cy):max(0, cy) + h, max(0, cx):max(0, cx) + w]

    def _wax(self, win, mask: np.ndarray, color, pressure: float, angle: float, opacity: float,
             rng: np.random.Generator) -> None:
        x0, y0, x1, y1 = win
        h, w = mask.shape
        tooth = self.tooth[y0:y1, x0:x1]
        streak = self._streaks(rng, h, w, angle, spacing=2.2)
        if streak.shape != (h, w):
            streak = np.pad(streak, ((0, h - streak.shape[0]), (0, w - streak.shape[1])), mode="edge")
        surface = 0.55 * tooth + 0.45 * streak
        # wax only reaches what sticks up: more pressure presses it into the valleys too
        level = 0.8 - 0.5 * pressure - 0.2 * np.clip(self.wax[y0:y1, x0:x1], 0, 1)
        coverage = _smoothstep(level - 0.12, level + 0.08, surface)
        alpha = np.clip(mask * coverage * KINDS["crayon"]["alpha"] * opacity, 0, 0.93)
        self._deposit_cover(win, alpha, color)
        # a faint darker rim where wax piles up at the stroke edges
        rim = np.clip(mask * (1 - mask) * 3.2, 0, 1) * coverage * 0.18
        self._deposit_multiply(win, rim, color)
        self.wax[y0:y1, x0:x1] = np.clip(self.wax[y0:y1, x0:x1] + alpha, 0, 2)

    def erase(self, op: dict, supply: dict, rng: np.random.Generator) -> None:
        width = float(op.get("width", KINDS["eraser"]["width"]))
        pts, p = self._path(op, rng, "eraser", width)
        win = self._window(pts, width + 4)
        x0, y0, x1, y1 = win
        mask = self._line_mask(pts, width * (0.8 + 0.3 * p), np.clip(0.5 + 0.5 * p, 0, 1), win, blur=1.2)
        strength = mask * 0.9
        lift = strength * (1 - 0.85 * np.clip(self.ink[y0:y1, x0:x1], 0, 1)) \
            * (1 - 0.5 * np.clip(self.wax[y0:y1, x0:x1], 0, 1))
        lift *= 0.9                                           # never quite clean: a ghost remains
        sub = self.rgb[y0:y1, x0:x1]
        sub += (self.paper[y0:y1, x0:x1] - sub) * lift[..., None]
        for layer in (self.graphite, self.wax, self.ink, self.lines):
            layer[y0:y1, x0:x1] *= 1 - lift
        # rubbing scuffs the tooth a little: later graphite catches slightly differently there
        self.tooth[y0:y1, x0:x1] = np.clip(self.tooth[y0:y1, x0:x1] * (1 - 0.08 * strength) + 0.04 * strength, 0, 1)

    # ---------------------------------------------------------------- shapes and fills

    def shape_points(self, op: dict, rng: np.random.Generator) -> list[list[float]]:
        """A hand-drawn outline: corners overshoot a little, an ellipse's ends overlap instead of meeting."""
        s = op["shape"]
        if s in ("line", "polygon"):
            pts = [list(p) for p in op["points"]]
            if s == "polygon":
                pts = pts + [pts[0], [pts[1][0] * 0.15 + pts[0][0] * 0.85, pts[1][1] * 0.15 + pts[0][1] * 0.85]]
            return pts
        if s == "rect":
            x, y, w, h = op["x"], op["y"], op["w"], op["h"]
            o = min(3.0, 0.04 * min(w, h))
            corners = [[x - o, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y - o]]
            return [[min(512, max(0, a + rng.normal(0, 0.6))), min(512, max(0, b + rng.normal(0, 0.6)))]
                    for a, b in corners]
        cx, cy, rx, ry = op["cx"], op["cy"], op["rx"], op["ry"]
        start = rng.random() * 2 * math.pi
        n = int(min(48, max(12, (rx + ry) / 4)))
        sweep = 2 * math.pi * (1.06 + 0.04 * rng.random())
        out = []
        for i in range(n + 1):
            a = start + sweep * i / n
            k = 1 + rng.normal(0, 0.012)
            out.append([min(512, max(0, cx + math.cos(a) * rx * k)), min(512, max(0, cy + math.sin(a) * ry * k))])
        return out

    def region(self, x: float, y: float) -> np.ndarray | None:
        """The area around (x, y) bounded by marks already on the paper (or the paper's edge)."""
        xi, yi = int(min(self.size - 1, max(0, x))), int(min(self.size - 1, max(0, y)))
        marked = self.lines > 0.22
        # an outline in crayon or pencil has small gaps in the grain: close them so colour stays inside the line
        closed = Image.fromarray(np.where(marked, 255, 0).astype(np.uint8)).filter(ImageFilter.MaxFilter(GAP))
        dark = np.asarray(closed) > 0
        free = Image.fromarray(np.where(dark, 0, 255).astype(np.uint8)).copy()   # a copy: floodfill writes into it
        if dark[yi, xi]:
            ys, xs = np.nonzero(~dark[max(0, yi - 4):yi + 5, max(0, xi - 4):xi + 5])
            if len(xs) == 0:
                return None
            xi, yi = max(0, xi - 4) + int(xs[0]), max(0, yi - 4) + int(ys[0])
        ImageDraw.floodfill(free, (xi, yi), 128, thresh=0)
        reg = np.asarray(free) == 128
        return reg if reg.sum() >= 4 else None

    def fill(self, op: dict, supply: dict, rng: np.random.Generator) -> bool:
        reg = self.region(op["x"], op["y"])
        if reg is None:
            return False
        kind = supply["kind"]
        # colour reaches into the outline a little and stops a little short elsewhere: by hand, not by bucket
        grown = Image.fromarray((reg * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(GAP + 2))
        soft = np.asarray(grown.filter(ImageFilter.GaussianBlur(1.2)), np.float32) / 255.0
        edge = _smooth_noise(rng, self.size, self.size, 4.0)
        area = _smoothstep(0.45, 0.62, soft + 0.18 * (edge - 0.5))
        ys, xs = np.nonzero(area > 0.01)
        if len(xs) == 0:
            return False
        win = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        x0, y0, x1, y1 = win
        area = area[y0:y1, x0:x1]
        h, w = area.shape
        pressure = float(op["pressure"])
        angle = float(op.get("angle", 30.0 + 120 * rng.random()))
        opacity = float(op.get("opacity", 1.0))
        if kind == "marker":
            width = float(supply.get("width", KINDS["marker"]["width"]))
            bands = self._stripes(h, w, angle, width * 0.85, rng)
            mask = area * (0.9 + 0.1 * bands)                   # passes overlap: faint darker bands
            self._marker_ink(win, mask, hex_rgb(supply["color"]), opacity, rng)
        elif kind == "crayon":
            strokes = self._stripes(h, w, angle, 4.5 - 2 * pressure, rng, jitter=0.6)
            mask = area * np.clip(0.55 + 0.45 * strokes, 0, 1)
            self._wax(win, mask, hex_rgb(supply["color"]), pressure, angle, opacity, rng)
        else:
            spec = PENCILS[supply.get("hardness", "HB")]
            spacing = 7.5 - 4.5 * pressure
            hatch = self._hatch(h, w, angle, spacing, spec.width * (0.6 + 0.5 * pressure), rng)
            if op.get("cross"):
                hatch = np.maximum(hatch, self._hatch(h, w, angle + 70, spacing * 1.1,
                                                      spec.width * (0.6 + 0.5 * pressure), rng))
            mask = area * hatch * (0.45 + 0.55 * pressure)
            self._graphite(win, mask, spec.darkness * opacity, rng)
        return True

    def _stripes(self, h: int, w: int, angle: float, spacing: float, rng: np.random.Generator,
                 jitter: float = 0.25) -> np.ndarray:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        a = math.radians(angle)
        across = -xx * math.sin(a) + yy * math.cos(a)
        wobble = (_smooth_noise(rng, h, w, 18.0) - 0.5) * spacing * jitter * 2
        return 0.5 + 0.5 * np.cos((across + wobble) * 2 * math.pi / max(1.2, spacing))

    def _hatch(self, h: int, w: int, angle: float, spacing: float, width: float, rng: np.random.Generator) -> np.ndarray:
        """Hand hatching: parallel lines that waver, vary in weight and stop short now and then."""
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        a = math.radians(angle)
        across = -xx * math.sin(a) + yy * math.cos(a)
        along = xx * math.cos(a) + yy * math.sin(a)
        wobble = (_smooth_noise(rng, h, w, 22.0) - 0.5) * spacing * 0.5
        u = (across + wobble) / spacing
        line_no = np.floor(u + 0.5)
        dist = np.abs(u - line_no) * spacing
        line = np.clip(1 - dist / max(0.5, width * 0.6), 0, 1)
        # each line has its own weight, and some break off along their length
        weight = 0.7 + 0.3 * np.cos(line_no * 12.9898 + self.seed % 97)
        breaks = _smoothstep(0.18, 0.35, _smooth_noise(rng, h, w, 14.0) + 0.0 * along)
        return np.clip(line * weight * (0.55 + 0.45 * breaks), 0, 1)

    # ---------------------------------------------------------------- running operations

    def apply(self, op: dict, supply: dict) -> bool:
        """Run one cleaned operation. Returns False when it left no mark (e.g. a fill with no region)."""
        rng = _rng(self.seed, 1 + self.ops_done)
        self.ops_done += 1
        kind = supply["kind"]
        if op["op"] == "erase":
            self.erase(op, supply, rng)
            return True
        if op["op"] == "fill":
            return self.fill(op, supply, rng)
        if op["op"] == "shape":
            op = {**op, "points": self.shape_points(op, rng)}
            op["pressure"] = op["pressure"] if not isinstance(op["pressure"], list) else op["pressure"][0]
        getattr(self, kind)(op, supply, rng)
        return True

    def image(self) -> Image.Image:
        return Image.fromarray((np.clip(self.rgb, 0, 1) * 255 + 0.5).astype(np.uint8), "RGB")

    def png(self) -> bytes:
        buf = io.BytesIO()
        self.image().save(buf, format="PNG", optimize=False)
        return buf.getvalue()


def render(ops: list[dict], supplies: list[dict], seed: int, size: int = 512) -> tuple[bytes, list[int]]:
    """Replay cleaned operations on a fresh sheet. Returns the PNG and the indexes of operations that left no mark."""
    by_id = {s["id"]: s for s in supplies}
    sheet = Sheet(seed, size)
    empty = []
    for i, op in enumerate(ops):
        supply = by_id.get(op["tool"])
        if supply is None or not sheet.apply(op, supply):
            empty.append(i)
    return sheet.png(), empty


def seed_for(root_id: str) -> int:
    """The same sheet for every version of one drawing."""
    import hashlib
    return int(hashlib.sha256(root_id.encode()).hexdigest()[:8], 16)

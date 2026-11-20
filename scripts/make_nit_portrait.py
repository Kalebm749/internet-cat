"""Draw Nit's portrait as SVG (viewer-side art: it is never put in a prompt, and the creature's own self-image is
learned in sleep and may disagree with it). Dark, fuzzy, vaguely humanoid, two empty white oval eyes and no other
features. Deterministic: the same seed gives the same picture.

  .venv/bin/python scripts/make_nit_portrait.py            # writes cat/web/static/img/nit/nit.svg and nit-asleep.svg
"""
import math
import random
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "cat" / "web" / "static" / "img" / "nit"
W, H = 400, 560

# the silhouette: overlapping ellipses (cx, cy, rx, ry, rotation degrees)
PARTS = [(200, 168, 92, 100, 0),            # head
         (200, 345, 118, 150, 0),           # body
         (96, 360, 30, 105, 7),             # left arm
         (304, 360, 30, 105, -7),           # right arm
         (160, 480, 34, 28, 0), (240, 480, 34, 28, 0)]   # feet-like tufts


def inside(x, y, part):
    cx, cy, rx, ry, rot = part
    a = math.radians(-rot)
    dx, dy = x - cx, y - cy
    ux, uy = dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
    return (ux / rx) ** 2 + (uy / ry) ** 2 <= 1.0


def outline_points(rng, per_part=150):
    pts = []
    for i, p in enumerate(PARTS):
        cx, cy, rx, ry, rot = p
        r = math.radians(rot)
        for _ in range(per_part):
            t = rng.uniform(0, 2 * math.pi)
            ux, uy = rx * math.cos(t), ry * math.sin(t)
            x, y = cx + ux * math.cos(r) - uy * math.sin(r), cy + ux * math.sin(r) + uy * math.cos(r)
            if any(j != i and inside(x, y, q) for j, q in enumerate(PARTS)):
                continue                                   # inside another part: not on the outside
            nx, ny = math.cos(t) / rx, math.sin(t) / ry      # outward normal (before rotation)
            nl = math.hypot(nx, ny)
            nx, ny = nx / nl, ny / nl
            ox, oy = nx * math.cos(r) - ny * math.sin(r), nx * math.sin(r) + ny * math.cos(r)
            pts.append((x, y, ox, oy))
    return pts


def hair(rng, sleepy):
    out = []
    for (x, y, ox, oy) in outline_points(rng):
        for _ in range(3):
            ang = math.atan2(oy, ox) + rng.uniform(-0.55, 0.55)
            ln = rng.uniform(9, 30) * (0.8 if sleepy else 1.0)
            droop = 6 if y > 260 else 0                    # lower fur hangs a little
            ex, ey = x + math.cos(ang) * ln, y + math.sin(ang) * ln + droop * rng.random()
            mx, my = (x + ex) / 2 + rng.uniform(-3, 3), (y + ey) / 2 + rng.uniform(-3, 3)
            shade = rng.choice(["#0a0a0f", "#0f0f16", "#14141c", "#1b1b26"])
            out.append(f'<path d="M{x - ox * 6:.1f} {y - oy * 6:.1f} Q{mx:.1f} {my:.1f} {ex:.1f} {ey:.1f}" '
                       f'stroke="{shade}" stroke-width="{rng.uniform(1.6, 3.2):.1f}" fill="none" stroke-linecap="round"/>')
    return "\n    ".join(out)


def svg(asleep: bool) -> str:
    rng = random.Random(7)
    body = "\n    ".join(
        f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" transform="rotate({rot} {cx} {cy})"/>' for cx, cy, rx, ry, rot in PARTS)
    fur = hair(rng, asleep)
    if asleep:
        eyes = ('<g opacity="0.3"><ellipse cx="163" cy="172" rx="17" ry="5" transform="rotate(8 163 172)"/>'
                '<ellipse cx="237" cy="172" rx="17" ry="5" transform="rotate(-8 237 172)"/></g>')
        lean = 'transform="translate(0 14) rotate(2 200 300)"'
    else:
        eyes = ('<g filter="url(#glow)"><ellipse cx="163" cy="160" rx="17" ry="27" transform="rotate(8 163 160)"/>'
                '<ellipse cx="237" cy="160" rx="17" ry="27" transform="rotate(-8 237 160)"/></g>')
        lean = ""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" aria-label="{'Nit, asleep' if asleep else 'Nit'}">
  <defs>
    <radialGradient id="rim" gradientUnits="userSpaceOnUse" cx="170" cy="120" r="380">
      <stop offset="0" stop-color="#2a2a38"/><stop offset="0.45" stop-color="#101018"/><stop offset="1" stop-color="#07070a"/>
    </radialGradient>
    <filter id="fuzz" x="-15%" y="-15%" width="130%" height="130%">
      <feTurbulence type="fractalNoise" baseFrequency="0.07" numOctaves="2" seed="3" result="n"/>
      <feDisplacementMap in="SourceGraphic" in2="n" scale="9"/>
    </filter>
    <filter id="glow" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="5" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>
  <g {lean}>
  <g filter="url(#fuzz)" fill="url(#rim)">
    {body}
  </g>
  <g>
    {fur}
  </g>
  <g fill="#f3f2ec">{eyes}</g>
  </g>
</svg>
'''


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "nit.svg").write_text(svg(False), encoding="utf-8")
    (OUT / "nit-asleep.svg").write_text(svg(True), encoding="utf-8")
    print("wrote", OUT / "nit.svg", "and", OUT / "nit-asleep.svg")
    return 0


if __name__ == "__main__":
    sys.exit(main())

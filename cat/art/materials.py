"""Physical art materials: how each kind of supply behaves on paper (docs/rewrite-contracts.md §4.10).

A *supply* is one thing in the art room (content: the `art_room.supplies` fact), e.g.
    {id: crayon_red, kind: crayon, name: "a red crayon", color: "#c8372d"}
    {id: pencil_2b,  kind: pencil, name: "a 2B pencil", hardness: 2B}
A *material* is how a kind of supply marks paper. Supplies are data (so wear, running out, new colours and new
supplies are world changes, not code); materials are code (a new kind of material is a new entry here and a
renderer for it).
"""
from __future__ import annotations

from dataclasses import dataclass

GRAPHITE = (0.20, 0.20, 0.22)


@dataclass(frozen=True)
class Pencil:
    darkness: float          # how dark one firm pass is
    width: float             # base line width in px (512 canvas)
    smudge: float            # softness of the line edge


PENCILS = {
    "H": Pencil(darkness=0.38, width=1.3, smudge=0.45),
    "HB": Pencil(darkness=0.55, width=1.6, smudge=0.55),
    "2B": Pencil(darkness=0.72, width=2.0, smudge=0.7),
    "4B": Pencil(darkness=0.88, width=2.8, smudge=0.95),
    "6B": Pencil(darkness=0.95, width=3.4, smudge=1.15),
}

# Base widths and behaviour per kind. The renderer reads these; supplies may override width.
KINDS = {
    "pencil": {"width": 1.8, "width_range": (0.6, 8.0), "tremor": 0.35},
    "marker": {"width": 7.0, "width_range": (2.0, 18.0), "tremor": 0.45, "alpha": 0.86},
    "crayon": {"width": 9.0, "width_range": (3.0, 24.0), "tremor": 0.8, "alpha": 0.9},
    "eraser": {"width": 16.0, "width_range": (4.0, 40.0), "tremor": 0.5},
}
DRAWING_KINDS = ("pencil", "marker", "crayon")


def hex_rgb(value: str) -> tuple[float, float, float]:
    v = value.strip().lstrip("#")
    if len(v) != 6 or any(c not in "0123456789abcdefABCDEF" for c in v):
        raise ValueError(f"not a colour: {value!r}")
    return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def validate_supply(s: object) -> list[str]:
    """Problems with one supply entry (used by the content validator and the renderer)."""
    if not isinstance(s, dict):
        return ["a supply must be a mapping"]
    probs = []
    sid, kind = s.get("id"), s.get("kind")
    if not isinstance(sid, str) or not sid:
        probs.append("a supply needs an id")
    if kind not in KINDS:
        probs.append(f"supply {sid!r}: kind must be one of {', '.join(KINDS)}")
    if not isinstance(s.get("name"), str):
        probs.append(f"supply {sid!r}: needs a name")
    if kind in ("marker", "crayon"):
        try:
            hex_rgb(str(s.get("color", "")))
        except ValueError:
            probs.append(f"supply {sid!r}: needs a color like '#c8372d'")
    if kind == "pencil" and s.get("hardness", "HB") not in PENCILS:
        probs.append(f"supply {sid!r}: hardness must be one of {', '.join(PENCILS)}")
    return probs

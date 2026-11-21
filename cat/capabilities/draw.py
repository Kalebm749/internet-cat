"""Create or revisit an image from Nit's own stated intention."""
from __future__ import annotations

import json
import re
import uuid
import xml.etree.ElementTree as ET

from cat.capabilities import CapContext, CapabilityResult
from cat.llm import BackendError, call_json


def _safe_svg(value: str) -> str:
    """Accept a small self-contained SVG subset; reject active or external content."""
    if not isinstance(value, str) or len(value) > 300_000:
        raise BackendError("image is missing or too large")
    try:
        root = ET.fromstring(value)
    except ET.ParseError as e:
        raise BackendError(f"invalid SVG: {e}") from None
    allowed = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}
    allowed_attrs = {"viewBox", "width", "height", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy",
                     "r", "rx", "ry", "d", "points", "fill", "stroke", "stroke-width", "opacity",
                     "stroke-linecap", "stroke-linejoin", "transform"}
    if root.tag.split("}")[-1] != "svg" or root.attrib.get("viewBox") not in ("0 0 512 512", "0,0,512,512"):
        raise BackendError("SVG must use the 512 by 512 canvas")
    for node in root.iter():
        if node.tag.split("}")[-1] not in allowed or node.text and node.text.strip():
            raise BackendError("SVG contains unsupported content")
        for key, val in node.attrib.items():
            key = key.split("}")[-1]
            if key not in allowed_attrs or re.search(r"url\(|https?:|data:|javascript:|@", val, re.I):
                raise BackendError("SVG contains unsupported attributes")
    return ET.tostring(root, encoding="unicode")


class Drawing:
    name = "draw"
    external = False

    def run(self, ctx: CapContext, inputs: dict) -> CapabilityResult:
        intention = str(inputs.get("intention", "")).strip()
        if not intention:
            return CapabilityResult(ok=False, text="No drawing intention came to mind.")
        previous = None
        if inputs.get("artwork"):
            previous = next((x for x in ctx.facts.get("art_room.artworks", [])
                             if str(x.get("id")) == str(inputs["artwork"])), None)
            if previous is None:
                return CapabilityResult(ok=False, text=ctx.texts["capability_failed"])
        thoughts = str(inputs.get("thoughts", "")).strip()
        situation = json.dumps({"place": ctx.content.locations[ctx.location].description,
                                "world": {k: v for k, v in ctx.facts.items() if k.startswith("world.")}},
                               ensure_ascii=False, default=str)[:3000]
        pass_guidance = (
            "This is the first pass. Make a clear, satisfying sketch with a strong composition and a few broad "
            "color shapes. Leave details unfinished so Nit can return to it. Use at most 12 shapes and keep the "
            "SVG compact, under 4,000 characters. "
            if previous is None else
            "This is one refinement pass on existing unfinished work. Preserve its main composition and subject; "
            "make only the changes Nit asked for, leaving room for another pass. Use at most 18 shapes and keep "
            "the complete SVG compact, under 5,000 characters. "
        )
        prompt = ("You are Gwen, a visual craft tool for Nit. Nit has already chosen what it wants to make. "
                  "Do not choose a new subject, interpret its personality, or add narrative text. Translate its "
                  "intention into a self-contained SVG image. Be visually expressive and make a readable image. "
                  + pass_guidance +
                  "Use only SVG elements svg,g,path,rect,circle,ellipse,line,polyline,polygon; no scripts, text, "
                  "links, stylesheets, or external resources. Canvas viewBox='0 0 512 512'. Return a title, a short "
                  "description of the visible image, and the SVG. Keep the description faithful to what is drawn.")
        user = f"Nit's intention: {intention}\nNit's associated thoughts: {thoughts}\nContext: {situation}"
        if previous:
            user += (f"\nThis is a new pass on {previous.get('title')}: {previous.get('description')}\n"
                     f"Previous SVG: {previous.get('svg', '')}\nNit's revision intention: {intention}. Preserve the "
                     "prior version as history and make a distinct but related version.")
        schema = {"type": "object", "properties": {"title": {"type": "string"},
                   "description": {"type": "string"}, "svg": {"type": "string"}},
                  "required": ["title", "description", "svg"]}
        def parse(result):
            if not all(isinstance(result.get(k), str) and result[k].strip() for k in ("title", "description", "svg")):
                raise BackendError("drawing response is incomplete")
            return result
        model = (ctx.tuning or {}).get("models", {}).get("art_model")
        if not ctx.backend or not model:
            raise BackendError("large creative model is unavailable")
        result = call_json(ctx.backend, "capability", model,
                           [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
                           schema, parse, retries=0, max_tokens=1400)
        svg = _safe_svg(result["svg"])
        artifact_id = uuid.uuid4().hex
        artifact = {"id": artifact_id, "type": "drawing", "title": result["title"][:100],
                    "created_at": ctx.sim_time, "subject": intention[:500],
                    "description": result["description"][:1000], "thoughts": thoughts[:1000], "svg": svg,
                    "status": "unfinished", "revision_of": previous.get("id") if previous else None}
        # A revision links to the same original root while retaining every prior activity/artifact.
        fields = {k: v for k, v in artifact.items() if k != "svg"}
        fields["svg"] = svg
        return CapabilityResult(text="An unfinished first pass takes shape." if previous is None
                                else "Another unfinished pass takes shape.", fields=fields, artifact=artifact)


CAPABILITY = Drawing()

"""Drawing with physical supplies (docs/rewrite-contracts.md §4.10).

Nit decides what to draw, which supplies to pick up, how much effort to give, and (with other actions) when to
come back to a drawing and when it is finished. Gwen (the art model) only translates that intention into drawing
operations with the supplies Nit picked up; code checks every operation (cat/art/operations.py) and the simulator
(cat/art/render.py) puts them on paper. A new pass on a drawing replays every earlier operation on the same sheet
and adds the new ones; the result is a new version, and every earlier version stays as it was.

Inputs: intention, thoughts, materials (Nit's own words about which supplies and how), effort (quick, careful,
patient), and artwork (the drawing to return to, for a new pass).
"""
from __future__ import annotations

import json
import re
import uuid

from cat.art import operations, render
from cat.art.materials import validate_supply
from cat.capabilities import CapabilityResult, CapContext
from cat.config import CONFIG_DIR
from cat.decision import load_prompt_file
from cat.llm import BackendError, call_json

PROMPTS_PATH = CONFIG_DIR / "prompts" / "art.yaml"
PROMPT_FIELDS = {
    "system": {"size", "max_points", "budget"},
    "first": set(),
    "revision": {"title", "description", "history"},
    "fresh_from_old": {"title", "description"},
    "user": {"intention", "thoughts", "supplies", "materials", "effort", "place", "sheet"},
}
SUPPLIES_FACT = "art_room.supplies"
ARTWORKS_FACT = "art_room.artworks"
KIND_WORDS = {"pencil": ("pencil",), "marker": ("marker", "felt"), "crayon": ("crayon", "wax"),
              "eraser": ("eraser", "erase", "rubber", "rub out")}
_prompts: dict | None = None


def prompts() -> dict:
    global _prompts
    if _prompts is None:
        _prompts = load_prompt_file(PROMPTS_PATH, PROMPT_FIELDS)
    return _prompts


def supplies_here(facts: dict) -> list[dict]:
    return [s for s in facts.get(SUPPLIES_FACT, []) if not validate_supply(s)]


def picked_up(supplies: list[dict], words: str) -> list[dict]:
    """The supplies Nit's own words name: kinds (pencil, marker, crayon, eraser) and, when it names them,
    colours or hardness. Words that name no supply at all leave every supply within reach."""
    t = (words or "").lower()

    def says(w: str) -> bool:
        return re.search(rf"\b{re.escape(w.lower())}", t) is not None
    kinds = {k for k, ws in KIND_WORDS.items() if any(says(w) for w in ws)}
    if not kinds:
        return list(supplies)
    out = []
    for s in supplies:
        if s["kind"] not in kinds:
            continue
        same = [x for x in supplies if x["kind"] == s["kind"]]
        marks = [str(x.get("color_name") or x.get("hardness") or "") for x in same]
        named = [m for m in marks if m and says(m)]
        mine = str(s.get("color_name") or s.get("hardness") or "")
        if named and mine not in named:
            continue
        out.append(s)
    return out or list(supplies)


def _history(ops: list[dict], limit: int = 3500) -> str:
    text = json.dumps(ops, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return f"(the first {len(ops)} operations, shortened) " + text[-limit:]


def _schema(tool_ids: list[str]) -> dict:
    num = {"type": "number"}
    op = {"type": "object", "properties": {
        "op": {"type": "string", "enum": list(operations.OPS)}, "tool": {"type": "string", "enum": tool_ids},
        "points": {"type": "array", "items": {"type": "array", "items": num}}, "pressure": num, "width": num,
        "opacity": num, "shape": {"type": "string", "enum": list(operations.SHAPES)}, "x": num, "y": num, "w": num,
        "h": num, "cx": num, "cy": num, "rx": num, "ry": num, "angle": num, "cross": {"type": "boolean"}},
        "required": ["op", "tool"]}
    return {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                                             "operations": {"type": "array", "items": op}},
            "required": ["title", "description", "operations"]}


class Drawing:
    name = "draw"
    external = False

    def run(self, ctx: CapContext, inputs: dict) -> CapabilityResult:
        intention = str(inputs.get("intention", "")).strip()
        if not intention:
            return CapabilityResult(ok=False, text="No drawing intention came to mind.")
        art = (ctx.tuning or {}).get("art") or {}
        model = (ctx.tuning or {}).get("models", {}).get("art_model")
        if not ctx.backend or not model or not art:
            raise BackendError("the art model is unavailable")
        thoughts = str(inputs.get("thoughts", "")).strip()
        materials_words = str(inputs.get("materials", "")).strip()
        effort = str(inputs.get("effort") or "careful")
        budget = int(art["effort_ops"].get(effort, max(art["effort_ops"].values())))
        supplies = supplies_here(ctx.facts)
        chosen = picked_up(supplies, materials_words)
        if not chosen:
            return CapabilityResult(ok=False, text="There is nothing here to draw with.")

        # a new pass: the earlier version, its operations and its sheet
        previous, prev_item = None, None
        if inputs.get("artwork"):
            prev_item = next((x for x in ctx.facts.get(ARTWORKS_FACT, []) if str(x.get("id")) == str(inputs["artwork"])),
                             None)
            if prev_item is None:
                return CapabilityResult(ok=False, text=ctx.texts["capability_failed"])
            previous = ctx.artifact(prev_item["id"]) if ctx.artifact else None
        p = prompts()
        prev_ops = list((previous or {}).get("meta", {}).get("ops", []))
        if previous:
            sheet = p["revision"].format(title=previous["title"], description=previous["description"],
                                         history=_history(prev_ops))
        elif prev_item:
            sheet = p["fresh_from_old"].format(title=prev_item.get("title", ""), description=prev_item.get("description", ""))
        else:
            sheet = p["first"]
        system = p["system"].format(size=int(art["canvas"]), max_points=int(art["max_points"]), budget=budget)
        user = p["user"].format(
            intention=intention, thoughts=thoughts or "(none)",
            supplies="; ".join(f"{s['id']} ({s['name']})" for s in chosen),
            materials=materials_words or "(Nit did not say)", effort=effort,
            place=ctx.content.locations[ctx.location].name, sheet=sheet)
        limits = {**art, "max_per_pass": budget}
        dropped: list[str] = []

        def parse(raw: dict) -> dict:
            title, desc = raw.get("title"), raw.get("description")
            if not (isinstance(title, str) and title.strip() and isinstance(desc, str) and desc.strip()):
                raise BackendError("the drawing needs a title and a description")
            good, probs = operations.clean(raw.get("operations"), chosen, limits, already=len(prev_ops))
            if not good:
                raise BackendError("no usable drawing operations: " + "; ".join(probs[:3]))
            dropped[:] = probs
            return {"title": title.strip()[:100], "description": desc.strip()[:1000], "ops": good}

        result = call_json(ctx.backend, "capability", model,
                           [{"role": "system", "content": system}, {"role": "user", "content": user}],
                           _schema([s["id"] for s in chosen]), parse, retries=1, max_tokens=int(art["max_tokens"]))

        # put it on paper: every earlier operation, then this pass, on the same sheet
        root = (previous or {}).get("root_id") or (previous or {}).get("id")
        artifact_id = uuid.uuid4().hex
        root = root or artifact_id
        seed = int((previous or {}).get("meta", {}).get("seed") or render.seed_for(root))
        snapshot = {s["id"]: s for s in (previous or {}).get("meta", {}).get("supplies", [])}
        snapshot.update({s["id"]: s for s in chosen})
        all_ops = prev_ops + result["ops"]
        png, empty = render.render(all_ops, list(snapshot.values()), seed, int(art["canvas"]))
        used = []
        for op in result["ops"]:
            name = snapshot[op["tool"]]["name"]
            if name not in used:
                used.append(name)
        artifact = {
            "id": artifact_id, "type": "drawing", "format": "png", "title": result["title"],
            "created_at": ctx.sim_time, "subject": intention[:500], "description": result["description"],
            "thoughts": thoughts[:1000], "status": "unfinished",
            "revision_of": prev_item["id"] if prev_item else None, "root_id": root, "materials": used,
            "mime": "image/png", "data": png,
            "meta": {"ops": all_ops, "pass_ops": len(result["ops"]), "supplies": list(snapshot.values()),
                     "seed": seed, "size": int(art["canvas"]), "effort": effort, "materials_words": materials_words,
                     "dropped": dropped[:20], "empty_ops": empty, "model": model},
        }
        fields = {k: v for k, v in artifact.items() if k not in ("data", "meta", "mime")}
        fields["materials"] = ", ".join(used)
        fields["revision_of"] = fields["revision_of"] or ""
        text = ctx.texts["drawing_made"].format(materials=fields["materials"], description=result["description"])
        return CapabilityResult(text=text, fields=fields, artifact=artifact)


CAPABILITY = Drawing()

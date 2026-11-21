"""Looking closely at a drawing and how it developed (docs/rewrite-contracts.md §4.10).

Nit can't see pixels, so looking at a drawing gives it what is on the paper (the description of each version,
made from the operations actually drawn), the supplies used, and its history from the first pass to this one.
Read only: it changes nothing. Its words are Nit's own work, so it is not `external`.
"""
from __future__ import annotations

import base64
import logging

from cat.capabilities import CapabilityResult, CapContext
from cat.llm import BackendError

log = logging.getLogger(__name__)

ARTWORKS_FACT = "art_room.artworks"


class LookAtDrawing:
    name = "look_at_drawing"
    external = False

    def run(self, ctx: CapContext, inputs: dict) -> CapabilityResult:
        items = {str(x.get("id")): x for x in ctx.facts.get(ARTWORKS_FACT, []) if isinstance(x, dict)}
        item = items.get(str(inputs.get("artwork")))
        if item is None:
            return CapabilityResult(ok=False, text=ctx.texts["capability_failed"])
        chain, cur, seen = [], item, set()
        while cur is not None and str(cur.get("id")) not in seen:
            seen.add(str(cur.get("id")))
            stored = ctx.artifact(cur["id"]) if ctx.artifact else None
            mats = (stored or {}).get("materials") or cur.get("materials") or []
            chain.append({"when": cur.get("created_at", ""), "title": cur.get("title", ""),
                          "description": (stored or cur).get("description", ""),
                          "materials": ", ".join(mats) if isinstance(mats, list) else str(mats)})
            cur = items.get(str(cur.get("revision_of") or ""))
        chain.reverse()
        history = " ".join(ctx.texts["drawing_version"].format(n=i + 1, when=v["when"][-11:], description=v["description"],
                                                               materials=v["materials"] or "unknown supplies")
                           for i, v in enumerate(chain))
        status = {"unfinished": "unfinished", "completed": "finished", "superseded": "an earlier version"}.get(
            item.get("status", ""), item.get("status", ""))
        text = ctx.texts["drawing_looked_at"].format(title=item.get("title", ""), status=status, history=history)
        seen = self._see(ctx, item)
        if seen:
            text += " " + ctx.texts["drawing_seen"].format(seen=seen)
        fields = {"title": item.get("title", ""), "versions": str(len(chain)), "verdict": "", "rating": "", "feeling": ""}
        inner: dict = {}
        verdict = self._judge(ctx, item, chain, seen or (chain[-1]["description"] if chain else ""))
        if verdict:
            v, words, rating = verdict
            fields.update(verdict=v, rating=f"{rating:.2f}", feeling=words)
            text += " " + ctx.texts["drawing_feeling"].format(words=words)
            inner = {"liked": {"satisfaction": 0.12, "confidence": 0.05},
                     "disliked": {"frustration": 0.15, "confidence": -0.08, "satisfaction": -0.05}}.get(v, {})
        return CapabilityResult(text=text, fields=fields, inner=inner)

    def _judge(self, ctx: CapContext, item: dict, chain: list, seen: str):
        """How it feels about the drawing against what it meant: its own, subjective verdict. A self-critical creature
        rates the same drawing lower. None when it cannot judge (no model, no stored intention)."""
        model = ((ctx.tuning or {}).get("models") or {}).get("decision_model")
        stored = ctx.artifact(item.get("root_id") or item["id"]) if ctx.artifact else None
        intention = (stored or {}).get("intention") or item.get("subject") or ""
        if not (model and ctx.backend and intention):
            return None
        from cat.capabilities.draw import prompts
        p = prompts()
        msgs = [{"role": "system", "content": p["evaluate_system"]},
                {"role": "user", "content": p["evaluate_user"].format(title=item.get("title", ""), intention=intention,
                                                                       seen=seen or "(nothing you can describe)")}]
        schema = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["liked", "mixed", "disliked"]},
                                                   "words": {"type": "string"}}, "required": ["verdict", "words"]}
        try:
            out = ctx.backend.chat_json("evaluate_drawing", model, msgs, schema)
        except BackendError as e:
            log.warning("could not judge the drawing: %s", e)
            return None
        v, words = out.get("verdict"), " ".join(str(out.get("words") or "").split())[:300]
        if v not in ("liked", "mixed", "disliked") or not words:
            return None
        crit = float((ctx.temperament or {}).get("self_criticism", 0.5))
        rating = max(0.0, min(1.0, {"liked": 0.8, "mixed": 0.5, "disliked": 0.2}[v] - 0.4 * (crit - 0.5)))
        return v, words, rating

    def _see(self, ctx: CapContext, item: dict) -> str | None:
        """The vision model looks at the latest PNG; loaded for this one look and unloaded right after, so it
        never shares the GPU with the decision model. Without it (missing, failing) Nit has the descriptions."""
        model = ((ctx.tuning or {}).get("models") or {}).get("vision_model")
        stored = ctx.artifact(item["id"]) if ctx.artifact else None
        png = (stored or {}).get("data")
        if not (model and ctx.backend and isinstance(png, (bytes, bytearray)) and png):
            return None
        from cat.capabilities.draw import prompts
        p = prompts()
        msgs = [{"role": "system", "content": p["see_system"]},
                {"role": "user", "content": p["see_user"].format(title=item.get("title", "")),
                 "images": [base64.b64encode(bytes(png)).decode("ascii")]}]
        schema = {"type": "object", "properties": {"seen": {"type": "string"}}, "required": ["seen"]}
        try:
            call = getattr(ctx.backend, "chat_json_with_options", None)
            out = (call(self.name, model, msgs, schema, {"keep_alive": 0}) if call
                   else ctx.backend.chat_json(self.name, model, msgs, schema))
        except BackendError as e:
            log.warning("vision model could not look: %s", e)
            return None
        seen = out.get("seen") if isinstance(out, dict) else None
        return " ".join(seen.split())[:800] if isinstance(seen, str) and seen.strip() else None


CAPABILITY = LookAtDrawing()

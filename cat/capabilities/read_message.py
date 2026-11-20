"""read_message: hand one visitor letter to the creature (docs/rewrite-contracts.md §4.8).

Input `which`: the letter's id in the mailbox fact named by content/world.yaml `mailbox`. The letter's full text
reaches the creature as perception, marked as a visitor's words; once read it also stays among the letters on
its mind until answered. Fields for content: {capability.sender}, {capability.summary}, {capability.text}.
"""
from __future__ import annotations

from cat.capabilities import CapabilityResult, CapContext

SUMMARY_CHARS = 120


class ReadMessage:
    name = "read_message"

    def run(self, ctx: CapContext, inputs: dict) -> CapabilityResult:
        key = ctx.content.world.get("mailbox")
        which = str(inputs.get("which"))
        letter = next((it for it in ctx.facts.get(key, []) if isinstance(it, dict) and str(it.get("id")) == which), None)
        if letter is None:
            return CapabilityResult(ok=False, text=ctx.texts["letter_gone"],
                                    fields={"sender": "nobody", "summary": "", "text": ""})
        text = str(letter.get("text", ""))
        sender = letter.get("sender") or "someone"
        summary = text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 3].rstrip() + "..."
        return CapabilityResult(
            ok=True,
            text=ctx.texts["letter_read"].format(sender=sender, text=text),
            fields={"sender": sender, "summary": summary, "text": text})


CAPABILITY = ReadMessage()

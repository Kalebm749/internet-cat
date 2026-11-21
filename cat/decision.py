"""One waking decision (docs/rewrite-contracts.md §3.3): prompt, output schema, validation, retries.

Three kinds:
    full       choose any option                                 (activity finished, interval)
    interrupt  "continue" the running activity, or any option    (event, mail, body band)
    check_in   "continue" or "stop" the running activity         (long activity, nothing happened)

The schema lists the valid action ids as an enum, and asks for the thought and reasoning before the action,
so the model thinks before it chooses. An answer that does not validate is retried with the problems; after
the last retry the creature continues what it is doing (or idles). If the backend itself fails, BackendError
goes up: the engine dozes.
"""
from __future__ import annotations

import json
import string
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from cat import clock
from cat.config import CONFIG_DIR
from cat.llm import BackendError, LLMBackend, _record
from cat.mind import Mind, Option

KINDS = ("full", "interrupt", "check_in")
CONTINUE, STOP, IDLE = "continue", "stop", "idle"
PROMPTS_PATH = CONFIG_DIR / "prompts" / "decision.yaml"
PROMPT_FIELDS = {
    "system": {"name", "nature", "senses", "abilities", "traits", "tendencies", "background", "boundaries", "core"},
    "core": {"lines"}, "note_liked": set(), "note_disliked": set(), "note_repeated": set(),
    "learned": {"lines"}, "dwelling": {"lines"}, "subject_repeat": {"count", "example"},
    "stay": {"hours"}, "exit_never": set(), "exit_recent": set(), "exit_ago": {"hours"},
    "time": {"part_of_day", "clock", "weather"},
    "place": {"location", "description"},
    "visible": {"lines"}, "perception": {"lines"}, "body": {"lines"}, "recent": {"lines"},
    "memories": {"lines"}, "letters": {"lines"}, "options": {"lines"},
    "activity": {"name", "elapsed", "planned"},
    "letter": {"id", "sender", "text"},
    "option": {"id", "name", "description", "inputs", "notes"},
    "input_text": {"name", "max_chars", "prompt"},
    "input_choice": {"name", "choices"},
    "input_int": {"name", "min", "max", "prompt"},
    "ask_full": set(), "ask_interrupt": {"activity"}, "ask_check_in": {"activity", "elapsed"},
    "retry": {"problems"},
}


class PromptError(ValueError):
    pass


class DecisionInvalid(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class Decision:
    kind: str
    action: str                     # an option id, or continue / stop / idle
    inputs: dict = field(default_factory=dict)
    thought: str = ""
    reasoning: str = ""
    preserve: bool = False
    letter: str | None = None
    fallback: bool = False          # True when the model's answers could not be used
    notes: list[str] = field(default_factory=list)   # what validation adjusted (truncations, a dropped letter id)


@dataclass
class Result:
    decision: Decision
    attempts: list[dict]            # {raw, problems} per attempt, for the trace
    messages: list[dict]            # the final conversation
    dropped: list[str]              # what the token budget left out of the prompt


# ---------------------------------------------------------------- prompts

def load_prompts(path: Path | str = PROMPTS_PATH) -> dict[str, str]:
    return load_prompt_file(path, PROMPT_FIELDS)


def load_prompt_file(path: Path | str, fields: dict[str, set[str]]) -> dict[str, str]:
    """Load a prompt file and check that it has exactly these entries with exactly these placeholders."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    probs = []
    for key, want in fields.items():
        if not isinstance(data.get(key), str):
            probs.append(f"missing prompt '{key}'")
            continue
        have = {f for _, f, _, _ in string.Formatter().parse(data[key]) if f}
        if have != want:
            probs.append(f"prompt '{key}': placeholders {sorted(have)} should be {sorted(want)}")
    for key in sorted(set(data) - set(fields)):
        probs.append(f"unknown prompt '{key}'")
    if probs:
        raise PromptError(f"{Path(path).name}: " + "; ".join(probs))
    return data


def _lines(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items)


def _input_line(p: dict, name: str, spec: dict) -> str:
    prompt = f" ({spec['prompt']})" if spec.get("prompt") else ""
    if spec["type"] == "text":
        return p["input_text"].format(name=name, max_chars=spec["max_chars"], prompt=prompt)
    if spec["type"] == "choice":
        choices = "; ".join(f'"{c["value"]}" = {c["label"]}' if c["label"] != str(c["value"]) else f'"{c["value"]}"'
                            for c in spec["choices"])
        return p["input_choice"].format(name=name, choices=choices)
    return p["input_int"].format(name=name, min=spec["min"], max=spec["max"], prompt=prompt)


def _option_line(p: dict, o: Option) -> str:
    inputs = (" Inputs: " + "; ".join(_input_line(p, n, s) for n, s in o.inputs.items()) + ".") if o.inputs else ""
    notes = (" (" + "; ".join(o.notes) + ")") if o.notes else ""
    return p["option"].format(id=o.id, name=o.name, description=o.description, inputs=inputs, notes=notes)


def system_prompt(mind: Mind, p: dict) -> str:
    i = mind.identity
    return p["system"].format(
        name=i["name"], nature=i["nature"], senses=", ".join(i["senses"]) or "nothing",
        abilities=", ".join(i["abilities"]) or "little", traits=", ".join(i["traits"]) or "yourself",
        tendencies=(" You tend to: " + "; ".join(i["tendencies"]) + ".") if i["tendencies"] else "",
        background=i["background"], boundaries=_lines(i["boundaries"]),
        core=p["core"].format(lines="\n".join(f"  - {c}" for c in i.get("core_memories") or []))
        if i.get("core_memories") else "").strip()


def _ask(mind: Mind, kind: str, p: dict) -> str:
    if kind == "full" or not mind.activity:
        return p["ask_full"]
    if kind == "interrupt":
        return p["ask_interrupt"].format(activity=mind.activity["name"])
    return p["ask_check_in"].format(activity=mind.activity["name"], elapsed=round(mind.activity["elapsed"]))


def user_prompt(mind: Mind, kind: str, p: dict, *, recent: list[dict], memories: list[str],
                visible: list[str], perception: list[str], learned: list[str] | None = None) -> str:
    parts = [p["time"].format(part_of_day=mind.now["part_of_day"], clock=mind.now["sim_time"][-5:],
                              weather=mind.now["weather"] or "unknown"),
             p["place"].format(location=mind.location["name"], description=mind.location["description"])]
    stay = mind.location.get("stay_minutes", 0)
    if stay >= 120:
        parts.append(p["stay"].format(hours=round(stay / 60)))
    if visible:
        parts.append(p["visible"].format(lines=_lines(visible)))
    if perception:
        parts.append(p["perception"].format(lines=_lines(perception)))
    body = list(mind.body["words"]) + ([mind.body["derived_words"]] if mind.body.get("derived_words") else [])
    if body:
        parts.append(p["body"].format(lines=_lines(body)))
    if mind.activity:
        a = mind.activity
        parts.append(p["activity"].format(name=a["name"], elapsed=round(a["elapsed"]), planned=round(a["planned"])))
    if recent:
        parts.append(p["recent"].format(lines=_lines([r["text"] for r in recent])))
    if memories:
        parts.append(p["memories"].format(lines=_lines(memories)))
    learned = mind.learned if learned is None else learned
    if learned:
        parts.append(p["learned"].format(lines=_lines(learned)))
    if mind.dwelling:
        parts.append(p["dwelling"].format(lines=_lines([f'"{d}"' for d in mind.dwelling])))
    if mind.letters:
        parts.append(p["letters"].format(lines="\n".join(p["letter"].format(**{k: l[k] for k in ("id", "sender", "text")})
                                                        for l in mind.letters)))
    if kind != "check_in":
        parts.append(p["options"].format(lines="\n".join(_option_line(p, o) for o in mind.options)))
    parts.append(_ask(mind, kind, p))
    return "\n\n".join(parts)


def tokens(text: str) -> int:
    return (len(text) + 3) // 4


def messages(mind: Mind, kind: str, p: dict, budget: int) -> tuple[list[dict], list[str]]:
    """The prompt, shortened to the token budget: older recent activities first, then the least relevant
    memories, then what is visible, then older perceptions. The options are never dropped."""
    sysm = system_prompt(mind, p)
    recent, memories, learned = list(mind.recent), list(mind.memories), list(mind.learned)
    visible, perception = list(mind.location["visible_facts"]), list(mind.perception)
    dropped: list[str] = []
    while True:
        user = user_prompt(mind, kind, p, recent=recent, memories=memories, visible=visible, perception=perception,
                           learned=learned)
        if tokens(sysm) + tokens(user) <= budget:
            break
        if recent:
            dropped.append("recent: " + recent.pop(0)["text"])
        elif learned:
            dropped.append("learned: " + learned.pop())
        elif memories:
            dropped.append("memory: " + memories.pop())
        elif visible:
            dropped.append("visible: " + visible.pop())
        elif perception:
            dropped.append("perception: " + perception.pop(0))
        else:
            break
    return [{"role": "system", "content": sysm}, {"role": "user", "content": user}], dropped


# ---------------------------------------------------------------- schema and validation

def allowed_actions(mind: Mind, kind: str) -> list[str]:
    if kind == "check_in":
        return [CONTINUE, STOP]
    ids = [o.id for o in mind.options]
    return ([CONTINUE] + ids) if kind == "interrupt" and mind.activity else ids


def schema(mind: Mind, kind: str, tuning: dict) -> dict:
    d = tuning["decisions"]
    inputs: dict[str, dict] = {}
    if kind != "check_in":
        for o in mind.options:
            for name, spec in o.inputs.items():
                if spec["type"] == "text":
                    prop = {"type": "string", "maxLength": spec["max_chars"]}
                elif spec["type"] == "int":
                    prop = {"type": "integer", "minimum": spec["min"], "maximum": spec["max"]}
                else:
                    prop = {"type": "string", "enum": [str(c["value"]) for c in spec["choices"]]}
                old = inputs.get(name)
                if old and "enum" in old and "enum" in prop:
                    prop["enum"] = list(dict.fromkeys(old["enum"] + prop["enum"]))
                inputs[name] = prop
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string", "maxLength": d["thought_max_chars"]},
            "reasoning": {"type": "string", "maxLength": d["reasoning_max_chars"]},
            "action": {"type": "string", "enum": allowed_actions(mind, kind)},
            "inputs": {"type": "object", "properties": inputs},
            "preserve": {"type": "boolean"},
            "letter": {"type": ["string", "null"]},
        },
        "required": ["thought", "reasoning", "action", "inputs", "preserve", "letter"],
    }


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return (cut[: cut.rfind(" ")] if " " in cut[limit // 2:] else cut).rstrip()


def _has_stray_han(text: str) -> bool:
    return any(unicodedata.name(c, "").startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH"))
               for c in text)


def validate(raw: Any, mind: Mind, kind: str, tuning: dict) -> Decision:
    """Check a model answer against the Mind. Raises DecisionInvalid with every problem."""
    d = tuning["decisions"]
    if not isinstance(raw, dict):
        raise DecisionInvalid(["the answer must be a JSON object"])
    probs: list[str] = []
    notes: list[str] = []
    texts = {}
    for k, limit in (("thought", d["thought_max_chars"]), ("reasoning", d["reasoning_max_chars"])):
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip():
            probs.append(f'"{k}" must be some words')
            texts[k] = ""
        else:
            texts[k] = _shorten(v.strip(), limit)
            if len(texts[k]) < len(v.strip()):
                notes.append(f"{k} shortened")
            if _has_stray_han(texts[k]):
                probs.append(f'"{k}" contains stray Chinese characters; write it in English')
    action = raw.get("action")
    allowed = allowed_actions(mind, kind)
    if action not in allowed:
        probs.append(f'"action" must be one of {", ".join(allowed)}; got {action!r}')
    inputs: dict[str, Any] = {}
    given = raw.get("inputs") or {}
    if not isinstance(given, dict):
        probs.append('"inputs" must be an object')
        given = {}
    opt = mind.option(action) if isinstance(action, str) else None
    if opt is not None:
        for name, spec in opt.inputs.items():
            v = given.get(name)
            if v is None or (isinstance(v, str) and not v.strip()):
                probs.append(f'{action} needs the input "{name}"')
                continue
            if spec["type"] == "text":
                v = str(v).strip()
                inputs[name] = _shorten(v, spec["max_chars"])
                if len(inputs[name]) < len(v):
                    notes.append(f"input {name} shortened")
                if _has_stray_han(inputs[name]):
                    probs.append(f'input "{name}" contains stray Chinese characters; write it in English')
            elif spec["type"] == "choice":
                by_value = {str(c["value"]): c["value"] for c in spec["choices"]}
                by_label = {c["label"]: c["value"] for c in spec["choices"]}
                if str(v) in by_value:
                    inputs[name] = by_value[str(v)]
                elif str(v) in by_label:
                    inputs[name] = by_label[str(v)]
                else:
                    probs.append(f'input "{name}" of {action} must be one of {", ".join(by_value)}; got {v!r}')
            else:
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    n = None
                if n is None or not spec["min"] <= n <= spec["max"]:
                    probs.append(f'input "{name}" of {action} must be a number from {spec["min"]} to {spec["max"]}')
                else:
                    inputs[name] = n
    letter = raw.get("letter")
    if letter is not None:
        if str(letter) in {l["id"] for l in mind.letters}:
            letter = str(letter)
        else:
            notes.append(f"letter {letter!r} is not on its mind; ignored")
            letter = None
    if probs:
        raise DecisionInvalid(probs)
    return Decision(kind=kind, action=action, inputs=inputs, thought=texts["thought"], reasoning=texts["reasoning"],
                    preserve=raw.get("preserve") is True, letter=letter, notes=notes)


def fallback(mind: Mind, kind: str) -> Decision:
    action = CONTINUE if mind.activity else IDLE
    return Decision(kind=kind, action=action, fallback=True)


def decide(backend: LLMBackend, model: str, mind: Mind, kind: str, prompts: dict, tuning: dict,
           reconsider: Callable[[Decision], list[str]] | None = None) -> Result:
    """Ask the model for a decision. Retries invalid answers with their problems; falls back after the last.
    Raises BackendError when the backend itself fails (offline, timeout) on the final attempt."""
    if kind not in KINDS:
        raise ValueError(f"unknown decision kind {kind!r}")
    msgs, dropped = messages(mind, kind, prompts, tuning["decisions"]["prompt_tokens"])
    sch = schema(mind, kind, tuning)
    attempts: list[dict] = []
    retries = tuning["decisions"]["retries"]
    reconsidered = False
    # One additional attempt is reserved for a behavioral reconsideration if its guard fires.
    for attempt in range(1, retries + 3):
        if attempt > retries + 1 + int(reconsidered):
            break
        backend.last_call = None
        try:
            raw = backend.chat_json("decide", model, msgs, sch)
        except BackendError as e:
            _record(backend, "decide", model, attempt, None, str(e))
            attempts.append({"raw": None, "problems": [str(e)]})
            if attempt >= retries + 1 + int(reconsidered):
                raise
            continue
        try:
            dec = validate(raw, mind, kind, tuning)
        except DecisionInvalid as e:
            _record(backend, "decide", model, attempt, raw, str(e))
            attempts.append({"raw": raw, "problems": e.problems})
            msgs = msgs + [{"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)},
                           {"role": "user", "content": prompts["retry"].format(problems="; ".join(e.problems))}]
            continue
        if reconsider and not reconsidered:
            problems = reconsider(dec)
            if problems:
                issue = "; ".join(problems)
                _record(backend, "decide", model, attempt, raw, issue)
                attempts.append({"raw": raw, "problems": problems})
                msgs = msgs + [{"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)},
                               {"role": "user", "content": prompts["retry"].format(problems=issue)}]
                reconsidered = True
                continue
        _record(backend, "decide", model, attempt, raw, None)
        attempts.append({"raw": raw, "problems": []})
        return Result(dec, attempts, msgs, dropped)
    return Result(fallback(mind, kind), attempts, msgs, dropped)


def activity_view(name: str, started: str, planned_minutes: float, now: str) -> dict:
    """The Mind's view of a running activity."""
    elapsed = (clock.parse(now) - clock.parse(started)).total_seconds() / 60
    return {"name": name, "elapsed": elapsed, "planned": planned_minutes,
            "progress": min(1.0, elapsed / planned_minutes) if planned_minutes else 1.0}

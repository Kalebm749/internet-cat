"""Carrying out decisions (docs/rewrite-contracts.md §4.5, §4.7): activities, the builtin actions, outcomes,
consequences, events and the passing of time.

    start(decision, ...)      inside: check the action can still happen, then persist it as running (or reject,
                              continue / stop / idle); a new action interrupts the running one
    prepare_running()         outside the transaction: run its capability, if any, while the running row is visible
    advance(minutes)          inside: pass sim time in steps: regrowth, the body, events, and the running
                              activity's end (or waking up)

Every consequence goes through the world ops (all-or-nothing) and the body, and is recorded: the activity row,
an experience row (code's facts), body changes with causes. What the creature perceives is collected in
`perception` until the next decision. The words come from config/prompts/world.yaml; nothing is narrated.
"""
from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from cat import artifacts, behaviour, clock, conditions, knowledge, life as lifemod, memories, templating
from cat.capabilities import CapabilityResult, CapContext, Registry
from cat.content import ActionSpec
from cat.decision import CONTINUE, IDLE, STOP, Decision, load_prompt_file
from cat.config import CONFIG_DIR
from cat.mind import resolve_choices
from cat.ops import OpError
from cat.scheduler import Scheduler

log = logging.getLogger("cat.execute")

WORLD_TEXT_PATH = CONFIG_DIR / "prompts" / "world.yaml"
WORLD_TEXT_FIELDS = {
    "arrive": {"location"}, "inspect_found": {"hint", "name"}, "inspect_nothing": set(), "discovered": {"name"},
    "finished": {"name"}, "interrupted": {"name"}, "rejected": {"name", "reason"}, "fall_asleep": set(),
    "wake": set(), "reflection": set(), "letter_read": {"sender", "text"}, "letter_gone": set(),
    "capability_failed": set(), "same_thought": set(), "mail_arrived": set(),
    "wish_granted": {"wish", "note"}, "drawing_made": {"materials", "description"},
    "drawing_looked_at": {"title", "status", "history"}, "drawing_version": {"n", "when", "description", "materials"},
}


def load_texts(path=WORLD_TEXT_PATH) -> dict[str, str]:
    return load_prompt_file(path, WORLD_TEXT_FIELDS)


@dataclass
class Running:
    id: int
    action: ActionSpec
    inputs: dict
    started: str
    planned: float                       # sim minutes
    cap: dict | None = None              # the capability result (asdict), if the action used one
    thought: str = ""
    freshness: float = 1.0               # §6: positive mood effects are multiplied by this

    def elapsed(self, now: str) -> float:
        return (clock.parse(now) - clock.parse(self.started)).total_seconds() / 60

    def view(self, now: str) -> dict:
        e = self.elapsed(now)
        return {"id": self.id, "action": self.action.id, "name": self.action.name, "started": self.started,
                "elapsed": e, "planned": self.planned, "progress": min(1.0, e / self.planned) if self.planned else 1.0,
                "continuous": self.action.continuous}


def _dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class Executor:
    def __init__(self, conn: sqlite3.Connection, life: lifemod.Life, tuning: dict, texts: dict[str, str],
                 registry: Registry, scheduler: Scheduler, rng: random.Random, backend: Any = None,
                 wake_gate: Any = None):
        self.conn, self.life, self.t, self.texts = conn, life, tuning, texts
        self.registry, self.scheduler, self.rng, self.backend = registry, scheduler, rng, backend
        self.perception: list[str] = []
        self.wake_gate = wake_gate or (lambda: True)        # False while the sleep cycle has not finished
        self.held = False                                    # True when the clock waited for the sleep cycle
        self.emitted: list[tuple[str, str]] = []          # (due sim time, event id), from emit_event ops
        self.running: Running | None = self._load_running()
        self.mood_base = 0.0                                # the last applied mood effect before preferences
        self._bands = self._band_indexes()

    # ---------------------------------------------------------------- helpers

    @property
    def c(self):
        return self.life.content

    def day(self) -> str:
        return self.life.sim_time[:10]

    def _load_running(self) -> Running | None:
        r = self.conn.execute("SELECT * FROM activities WHERE status = 'running' ORDER BY id DESC LIMIT 1").fetchone()
        if r is None:
            return None
        action = self.c.actions.get(r["action_id"])
        if action is None:                                  # the content lost this action: end it quietly
            self.conn.execute("UPDATE activities SET status = 'interrupted', ended_at = ?, text = ? WHERE id = ?",
                              (self.life.sim_time, "(action no longer exists)", r["id"]))
            return None
        return Running(r["id"], action, json.loads(r["inputs"]), r["started_at"], r["planned_minutes"],
                       json.loads(r["capability"]) if r["capability"] else None, freshness=r["freshness"])

    def _experience(self, kind: str, text: str, *, action_id: str | None = None, activity_id: int | None = None,
                    data: dict | None = None, thought: str | None = None, importance: float = 0.0,
                    remember: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO experiences(sim_time, kind, action_id, activity_id, location, text, data, thought, importance, "
            "remember) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.life.sim_time, kind, action_id, activity_id, self.life.location, text, _dump(data or {}), thought,
             importance, int(remember)))
        return cur.lastrowid

    def _effort(self, a: ActionSpec) -> float:
        if a.effort is None:
            return 0.0
        if isinstance(a.effort, str):
            return float(self.c.world.get("effort", {}).get(a.effort, 0.0))
        return float(a.effort)

    def _ctx(self) -> conditions.Ctx:
        return conditions.Ctx(creature=self.life.bodystate.as_dict(), facts=self.life.world.values,
                              hour=clock.hour_float(clock.parse(self.life.sim_time)),
                              known=knowledge.known_ids(self.conn),
                              last_done=knowledge.minutes_since_done(self.conn, self.life.sim_time), rng=self.rng,
                              count_done=self.count_done, time_scale=float(self.t["time_scale"]))

    def count_done(self, action_id: str, minutes: float) -> int:
        return knowledge.count_done(self.conn, action_id, self.life.sim_time, minutes)

    def _vars(self, a: ActionSpec | None, inputs: dict, cap: dict | None) -> dict:
        loc = self.c.locations[self.life.location]
        return {"inputs": inputs, "now": self.life.sim_time, "location": {"id": loc.id, "name": loc.name},
                "exits": list(loc.exits), "capability": (cap or {}).get("fields", {})}

    def _body(self, changes) -> None:
        if changes:
            lifemod.record_body(self.conn, self.life, changes)

    def _band_indexes(self) -> dict[str, int]:
        out = {}
        for var, bands in (self.c.world.get("body_words") or {}).items():
            v = self.life.bodystate.get(var)
            out[var] = sum(1 for b in bands if v >= b["from"])
        return out

    def recent(self, n: int) -> list[dict]:
        rows = self.conn.execute("SELECT ended_at, text FROM activities WHERE status IN ('finished', 'interrupted', "
                                 "'rejected') ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [{"text": f"{r['ended_at'][-5:]} {r['text']}"} for r in reversed(rows)]

    def capability_ok(self, name: str) -> bool:
        return self.registry.available(self.conn, name, self.day())[0]

    # ---------------------------------------------------------------- checks

    def check(self, action_id: str, inputs: dict) -> str | None:
        """Why this action cannot happen right now (None: it can). Time passed while the creature thought."""
        a = self.c.actions.get(action_id)
        if a is None:
            return "there is no such thing to do"
        if a.location not in (None, self.life.location):
            return "it is not possible here"
        if action_id not in knowledge.known_ids(self.conn):
            return "it does not know how"
        if action_id in (self.c.character.get("core", {}).get("prohibited_actions") or []):
            return "it is against its nature"
        if not conditions.all_hold(a.requires, self._ctx()):
            return "the moment has passed"
        for name, spec in a.inputs.items():
            if spec["type"] == "choice":
                values = {str(c["value"]) for c in resolve_choices(spec["options"], self.life)}
                if str(inputs.get(name)) not in values:
                    return f"{name} is no longer possible"
        if a.capability:
            ok, why = self.registry.available(self.conn, a.capability, self.day())
            if not ok:
                return why
        return None

    # ---------------------------------------------------------------- outside the transaction

    def prepare(self, decision: Decision) -> CapabilityResult | None:
        a = self.c.actions.get(decision.action)
        if a is None or not a.capability or self.check(a.id, decision.inputs):
            return None
        ctx = CapContext(content=self.c, facts=self.life.world.values, location=self.life.location,
                         sim_time=self.life.sim_time, texts=self.texts, backend=self.backend, tuning=self.t,
                         artifact=lambda i: artifacts.load(self.conn, i))
        return self.registry.run(a.capability, ctx, decision.inputs)

    def prepare_running(self) -> CapabilityResult | None:
        """Run a capability after its activity has been committed as running for live status views."""
        r = self.running
        if r is None or not r.action.capability or r.cap is not None:
            return None
        ctx = CapContext(content=self.c, facts=self.life.world.values, location=self.life.location,
                         sim_time=self.life.sim_time, texts=self.texts, backend=self.backend, tuning=self.t,
                         artifact=lambda i: artifacts.load(self.conn, i))
        return self.registry.run(r.action.capability, ctx, r.inputs)

    def attach_capability(self, cap: CapabilityResult) -> None:
        """Persist a completed capability result on the already-visible running activity."""
        if self.running is None:
            return
        if not cap.ok:
            text = self.texts["rejected"].format(name=self.running.action.name, reason=cap.text)
            self.perception.append(text)
            self._close("rejected", text, [], {"capability_failed": True}, note=False)
            return
        if isinstance(cap.artifact, dict) and "data" in cap.artifact:
            # the made thing is kept whole in its own table; the activity and the world keep a reference
            artifacts.save(self.conn, cap.artifact, self.running.id)
            cap = replace(cap, artifact=artifacts.reference(cap.artifact))
        self.running.cap = asdict(cap)
        self.conn.execute("UPDATE activities SET capability = ? WHERE id = ? AND status = 'running'",
                          (_dump(self.running.cap), self.running.id))
        if cap.text:
            self.perception.append(cap.text)

    # ---------------------------------------------------------------- inside the transaction

    def start(self, decision: Decision, decision_id: int | None, cap: CapabilityResult | None = None) -> dict:
        """Act on a decision. Returns {status, activity_id?, text?}."""
        if decision.preserve and decision.thought:
            self._experience("thought", decision.thought, thought=decision.thought, data={"preserve": True},
                             importance=0.8, remember=True)
        act = decision.action
        if act in (CONTINUE, IDLE):
            return {"status": act}
        if act == STOP:
            if self.running:
                return {"status": "stopped", **self.interrupt()}
            return {"status": "idle"}
        a = self.c.actions[act] if act in self.c.actions else None
        reason = self.check(act, decision.inputs)
        if reason is None and cap is not None and not cap.ok:
            reason = cap.text
        if reason is not None:
            name = a.name if a else act
            text = self.texts["rejected"].format(name=name, reason=reason)
            cur = self.conn.execute(
                "INSERT INTO activities(decision_id, action_id, location, inputs, started_at, planned_minutes, ended_at, "
                "status, text) VALUES (?, ?, ?, ?, ?, 0, ?, 'rejected', ?)",
                (decision_id, act, self.life.location, _dump(decision.inputs), self.life.sim_time,
                 self.life.sim_time, text))
            self.perception.append(text)
            return {"status": "rejected", "activity_id": cur.lastrowid, "text": text}
        if self.running:
            self.interrupt(note=False)                      # replaced by the new action: no extra decision
        if a.builtin == "sleep":
            planned = float(self.t["sleep"]["max_hours"]) * 60
            self.life.asleep = True
            self.perception.append(self.texts["fall_asleep"])
        else:
            planned = float(round(self.rng.uniform(*a.duration)))      # whole sim minutes
        capd = None
        if a.capability:
            self.registry.record_use(self.conn, a.capability, self.day())
        made = None
        if cap is not None:
            if isinstance(cap.artifact, dict) and "data" in cap.artifact:
                made, cap = cap.artifact, replace(cap, artifact=artifacts.reference(cap.artifact))
            capd = asdict(cap)
            if cap.text:
                self.perception.append(cap.text)
        fr = behaviour.freshness(self.conn, self.c, self.t, a, decision.inputs, self.life.sim_time,
                                 self.life.world.values)
        cur = self.conn.execute(
            "INSERT INTO activities(decision_id, action_id, location, inputs, started_at, planned_minutes, status, "
            "capability, family, input_sig, revisit_sig, restart_of, freshness, repeat_parts) "
            "VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)",
            (decision_id, a.id, self.life.location, _dump(decision.inputs), self.life.sim_time, planned,
             _dump(capd) if capd else None, a.family, behaviour.input_signature(decision.inputs),
             behaviour.revisit_signature(a, self.life.world.values), fr.restart_of, fr.value,
             _dump({**fr.parts, "revisit_changed": fr.revisit_changed})))
        if made is not None:
            artifacts.save(self.conn, made, cur.lastrowid)
        self.running = Running(cur.lastrowid, a, dict(decision.inputs), self.life.sim_time, planned, capd,
                               decision.thought, fr.value)
        if planned <= 0:
            self._complete()
        return {"status": "started", "activity_id": cur.lastrowid}

    def advance(self, minutes: int) -> None:
        """Let whole `minutes` of sim time pass (the clock has minute resolution; the loop carries fractions),
        ending the running activity at its moment."""
        step = int(self.t["body"]["step_minutes"])
        left = int(minutes)
        self.held = False
        while True:
            if self.running and self.running.action.builtin == "sleep" and self._ends_now() and not self.wake_gate():
                self.held = True                             # it would wake, but the night is not processed yet:
                break                                        # the clock waits (§3.4)
            if self.running and self._ends_now():
                self._complete()
            if left <= 0:
                break
            chunk = min(left, step)
            if self.running and self.running.action.builtin != "sleep":
                remaining = math.ceil(self.running.planned - self.running.elapsed(self.life.sim_time))
                chunk = min(chunk, max(remaining, 1))
            self._pass(chunk)
            left -= chunk

    # ---------------------------------------------------------------- time passing

    def _pass(self, minutes: int) -> None:
        before = self.life.sim_time
        self.life.sim_time = clock.advance(before, minutes)
        self.life.world.regrow(before, self.life.sim_time)
        r = self.running
        if self.life.asleep:
            self._body(self.life.body.passage(self.life.bodystate, minutes, asleep=True))
        elif r:
            self._body(self.life.body.passage(self.life.bodystate, minutes, effort_per_min=self._effort(r.action),
                                              per_minute=r.action.per_minute if r.action.continuous else None,
                                              cause=r.action.id))
        else:
            self._body(self.life.body.passage(self.life.bodystate, minutes))
        self._roll_events(minutes)
        bands = self._band_indexes()
        if not self.life.asleep:
            for var, idx in bands.items():
                if idx != self._bands.get(var):
                    self.scheduler.note("body", f"{var} changed")
        self._bands = bands

    def _roll_events(self, minutes: float) -> None:
        ctx = self._ctx()
        due = [e for t, e in self.emitted if t <= self.life.sim_time]
        self.emitted = [(t, e) for t, e in self.emitted if t > self.life.sim_time]
        for ev in self.c.events.values():
            if ev.hours and not conditions.evaluate({"hours": ev.hours}, ctx):
                continue
            if ev.weather_in and not conditions.evaluate({"weather_in": ev.weather_in}, ctx):
                continue
            p = 1 - math.exp(-ev.chance_per_hour * minutes / 60)
            if self.rng.random() < p:
                due.append(ev.id)
        for eid in due:
            self._fire(self.c.events[eid])

    def _fire(self, ev) -> None:
        if ev.world:
            try:
                res = self.life.world.apply(ev.world, self._vars(None, {}, None), self.rng, by="world")
                self.emitted += [(clock.advance(self.life.sim_time, e["delay_minutes"]), e["event"]) for e in res.events]
            except OpError as e:
                log.warning("event %s: %s", ev.id, e)
        here = ev.location in (None, self.life.location)
        senses = self.c.character.get("core", {}).get("senses") or []
        if here and ev.sense in senses and not self.life.asleep:
            self.perception.append(ev.text)
            self.scheduler.note("event", ev.text, ev.salience)
            self._experience("event", ev.text, data={"event": ev.id, "sense": ev.sense}, importance=ev.salience * 0.5)

    # ---------------------------------------------------------------- endings

    def _ends_now(self) -> bool:
        r = self.running
        if r.action.builtin == "sleep":
            sl = self.t["sleep"]
            hours = r.elapsed(self.life.sim_time) / 60
            if hours >= sl["max_hours"]:
                return True
            night = conditions.evaluate({"hours": sl["night_hours"]}, self._ctx())
            return hours >= sl["min_hours"] and self.life.bodystate.energy >= sl["wake_energy"] and not night
        return r.elapsed(self.life.sim_time) >= r.planned - 1e-6

    def _pick_block(self, a: ActionSpec) -> dict:
        if a.on_finish is not None:
            return a.on_finish
        ctx = self._ctx()
        for o in a.outcomes or []:
            when = o.get("when")
            conds = [] if when is None else (when if isinstance(when, list) else [when])
            if conditions.all_hold(conds, ctx):
                return o
        return {}

    def _apply_block(self, a: ActionSpec, block: dict, r: Running, factor: float = 1.0,
                     world: bool = True) -> tuple[list, list[str], dict]:
        """World ops, creature effects (times factor), discoveries. Returns (body changes, discovered ids, vars).
        Mood effects are multiplied by the learned preferences for the action, its family and the place (§2.4);
        the mood effect before that is kept in `self.mood_base` so preferences are learned from it, not from
        themselves."""
        vars = self._vars(a, r.inputs, r.cap)
        if world and block.get("world"):
            try:
                res = self.life.world.apply(block["world"], vars, self.rng, by="creature")
                self.emitted += [(clock.advance(self.life.sim_time, e["delay_minutes"]), e["event"]) for e in res.events]
            except OpError as e:
                log.warning("action %s: world change failed, nothing changed: %s", a.id, e)
        # Capabilities may return outside text (for example, a Wikipedia extract). Apply these
        # narrowly scoped raw fact updates without interpreting that text as a template.
        if world:
            for op in (r.cap or {}).get("world", []):
                try:
                    if op.get("op") == "append_raw":
                        self.life.world.append_raw(op["fact"], op["value"], by="creature")
                    elif op.get("op") == "update_item_raw":
                        self.life.world.update_item_raw(op["fact"], str(op["id"]), op["fields"], by="creature")
                except (KeyError, OpError) as e:
                    log.warning("action %s: capability world change failed: %s", a.id, e)
        effects = {k: v * factor for k, v in (block.get("creature") or {}).items()}
        if effects.get("mood", 0.0) > 0:
            effects["mood"] *= r.freshness                    # a tired repetition really does feel flat (§6.2)
        cap = r.cap or {}
        for k, v in (cap.get("creature") or {}).items():
            effects[k] = effects.get(k, 0.0) + v
        self.mood_base = effects.get("mood", 0.0)
        if "mood" in effects:
            prefs = memories.preferences(self.conn)
            effects["mood"] *= memories.mood_factor(prefs, a, self.life.location, self.t["sleep"]["pref_weight"])
        changes = self.life.body.apply(self.life.bodystate, effects, a.id)
        self._body(changes)
        found = []
        for t in block.get("discover") or []:
            if knowledge.learn(self.conn, t, self.c.actions[t].location or "", "effect", self.life.sim_time):
                found.append(t)
                self.perception.append(self.texts["discovered"].format(name=self.c.actions[t].name))
        return changes, found, vars

    def _close(self, status: str, text: str, changes: list, data: dict, importance: float = 0.0,
               remember: bool = False, remember_text: str | None = None, note: bool = True) -> dict:
        r = self.running
        # If a drawing session is interrupted, keep the image already made by its capability as an
        # unfinished piece so Nit can return to it later. Completed works are added by on_finish.
        if status == "interrupted" and r.action.capability == "draw" and (r.cap or {}).get("artifact"):
            piece = dict(r.cap["artifact"])
            piece["status"] = "unfinished"
            try:
                self.life.world.apply([{"op": "append", "fact": "art_room.artworks", "value": piece}],
                                      self._vars(r.action, r.inputs, r.cap), self.rng, by="creature")
            except OpError as e:
                log.warning("could not preserve interrupted drawing: %s", e)
        if not changes:
            self.mood_base = 0.0
        self.conn.execute("UPDATE activities SET status = ?, ended_at = ?, text = ?, changes = ? WHERE id = ?",
                          (status, self.life.sim_time, text, _dump([asdict(c) for c in changes]), r.id))
        if status == "finished":
            knowledge.done(self.conn, r.action.id, self.life.sim_time, text)
        cap_external = (r.cap or {}).get("external")
        external = self.registry.is_external(r.action.capability) if cap_external is None else cap_external
        self._experience("activity", remember_text or text, action_id=r.action.id, activity_id=r.id,
                         data={"status": status, "inputs": r.inputs, "minutes": round(r.elapsed(self.life.sim_time), 1),
                               "changes": [asdict(c) for c in changes], "mood_base": round(self.mood_base, 5),
                               **({"external": True} if external else {}), **data},
                         thought=r.thought or None, importance=importance, remember=remember)
        self.running = None
        if note:
            self.scheduler.note("activity_finished", r.action.id)
        return {"activity_id": r.id, "text": text}

    def _complete(self) -> dict:
        r = self.running
        a = r.action
        if a.builtin == "move":
            dest = r.inputs["to"]
            self.life.location = dest
            learned = knowledge.arrive(self.conn, self.c, dest, self.life.sim_time)
            text = self.texts["arrive"].format(location=self.c.locations[dest].name)
            return self._close("finished", text, [], {"arrived": dest, "learned": learned})
        if a.builtin == "inspect":
            return self._inspect()
        if a.builtin == "sleep":
            self.life.asleep = False
            text = self.texts["wake"]
            self.perception.append(text)
            return self._close("finished", text, [], {"slept_minutes": round(r.elapsed(self.life.sim_time))})
        block = self._pick_block(a)
        changes, found, vars = self._apply_block(a, block, r)
        try:
            text = templating.render(block.get("text"), {**vars, "facts": self.life.world.values}) \
                if block.get("text") else self.texts["finished"].format(name=a.name)
        except templating.TemplateError:
            text = self.texts["finished"].format(name=a.name)
        if block.get("reflection"):
            self.perception.append(self.texts["reflection"])
        rem = block.get("remember")
        rem_text = None
        if rem and rem.get("text"):
            try:
                rem_text = templating.render(rem["text"], {**vars, "facts": self.life.world.values})
            except templating.TemplateError:
                rem_text = None
        return self._close("finished", text, changes, {"discovered": found}, importance=(rem or {}).get("importance", 0.0),
                           remember=bool(rem), remember_text=rem_text)

    def _inspect(self) -> dict:
        ctx = self._ctx()
        here = self.c.locations[self.life.location].actions
        cands = [self.c.actions[x] for x in here
                 if self.c.actions[x].discovery == "hidden" and self.c.actions[x].inspect_hint and x not in ctx.known]
        if not cands:
            return self._close("finished", self.texts["inspect_nothing"], [], {"found": None})
        weights = [3.0 if conditions.all_hold(a.requires, ctx) else 1.0 for a in cands]
        found = self.rng.choices(cands, weights=weights)[0]
        knowledge.learn(self.conn, found.id, self.life.location, "inspect", self.life.sim_time)
        text = self.texts["inspect_found"].format(hint=found.inspect_hint.rstrip(), name=found.name)
        self.perception.append(text)
        return self._close("finished", text, [], {"found": found.id}, importance=0.5, remember=True)

    def interrupt(self, note: bool = True) -> dict:
        """Stop the running activity early: on_interrupt if the action has one, otherwise the share of its
        on_finish creature effects that matches its progress (no world changes)."""
        r = self.running
        a = r.action
        if a.builtin == "sleep":
            self.life.asleep = False
        progress = min(1.0, r.elapsed(self.life.sim_time) / r.planned) if r.planned else 1.0
        if a.on_interrupt is not None:
            changes, _, _ = self._apply_block(a, a.on_interrupt, r)
        elif a.on_finish is not None and a.on_finish.get("creature"):
            changes, _, _ = self._apply_block(a, {"creature": a.on_finish["creature"]}, r, factor=progress, world=False)
        else:
            changes = []
        return self._close("interrupted", self.texts["interrupted"].format(name=a.name), changes,
                           {"progress": round(progress, 2)}, note=note)

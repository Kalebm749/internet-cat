"""The engine loop of the new life (docs/rewrite-contracts.md §3.3): the only writer of nit.db.

Each step:
  1. sim time catches up with real time (x time_scale), carried in whole minutes: regrowth, body, events,
     the running activity's end                                                   (one transaction)
  2. if a decision is due (cat/scheduler.py): build the Mind, ask the decision model    (no transaction)
  3. the time spent thinking passes (the clock keeps running while it thinks)    (one transaction)
  4. run the chosen action's capability, if any                                  (no transaction)
  5. record the decision and act on it                                           (one transaction)
If the model fails (Ollama down), the creature dozes: no decisions for `doze_seconds`, while time and any
running activity go on. The clock is injectable, so tests and `--fast` runs drive real time themselves.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import threading

from cat import content as contentmod, decision as decisionmod, identity, knowledge, life as lifemod, memories
from cat import mind as mindmod, sleepcycle, store
from cat.capabilities import Registry
from cat.decision import Decision
from cat.execute import Executor, load_texts
from cat.llm import BackendError, LLMBackend
from cat.scheduler import Scheduler

log = logging.getLogger("cat.loop")


@dataclass
class StepReport:
    sim_time: str
    decision: Decision | None = None
    result: dict | None = None
    dozing: bool = False
    notes: list[str] = field(default_factory=list)


class Sleeper:
    """Runs one night's sleep cycle (cat/sleepcycle.py plan) beside the loop, on its own read-only connection,
    or inline when the database is in memory or `sleep.threaded` is false. The loop commits the plan."""

    def __init__(self, loop: "Loop"):
        self.loop = loop
        self.plan: sleepcycle.SleepPlan | None = None
        self.error: str | None = None
        self.done = False
        self.committed = False
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        path = self.loop.conn.execute("PRAGMA database_list").fetchone()[2]
        if self.loop.t["sleep"]["threaded"] and path:
            self.thread = threading.Thread(target=self._run, args=(path,), name="sleep-cycle", daemon=True)
            self.thread.start()
        else:
            self._run(None)

    def _run(self, path: str | None) -> None:
        lp = self.loop
        conn = store.connect(path, readonly=True) if path else lp.conn
        try:
            self.plan = sleepcycle.plan(conn, lp.life.content, lp.t, lp.backend, lp.sleep_prompts,
                                        lp.life.character.sheet, lp.life.sim_time)
        except Exception as e:                               # a broken night must never keep it asleep
            log.exception("the sleep cycle failed")
            self.error = str(e)
        finally:
            if path:
                conn.close()
            self.done = True


class Loop:
    def __init__(self, conn: sqlite3.Connection, content: contentmod.Content, tuning: dict, backend: LLMBackend, *,
                 clock: Callable[[], float] = time.monotonic, seed: Any = None):
        self.conn, self.t, self.backend, self.clock = conn, tuning, backend, clock
        self.life = lifemod.open_life(conn, content, tuning)
        self.prompts = decisionmod.load_prompts()
        self.sleep_prompts = sleepcycle.load_prompts()
        self.texts = load_texts()
        self.sleeper: Sleeper | None = None
        self.registry = Registry(content)
        self.scheduler = Scheduler(tuning["decisions"])
        self.rng = random.Random(seed if seed is not None else time.time_ns())
        self.exec = self._executor()
        self.last_real = clock()
        self.carry = 0.0                       # sim minutes not yet passed (the clock has minute resolution)
        self.doze_until: float | None = None
        self.seed = seed

    def _executor(self) -> Executor:
        return Executor(self.conn, self.life, self.t, self.texts, self.registry, self.scheduler, self.rng,
                        self.backend, wake_gate=self._may_wake)

    def _may_wake(self) -> bool:
        return self.sleeper is not None and self.sleeper.committed

    def _sleep_cycle(self) -> None:
        """Start the night's cycle when it falls asleep; commit it when ready; forget it once awake."""
        if not self.life.asleep:
            self.sleeper = None
            return
        if self.sleeper is None:
            self.sleeper = Sleeper(self)
            self.sleeper.start()
        sp = self.sleeper
        if sp.done and not sp.committed:
            with store.transaction(self.conn):
                if sp.plan is not None:
                    sleepcycle.commit(self.conn, sp.plan, self.life.sim_time)
                else:
                    self.conn.execute("INSERT INTO sleep_runs(started_at, finished_at, status, stages, summary) "
                                      "VALUES (?, ?, 'failed', '{}', ?)",
                                      (self.life.sim_time, self.life.sim_time, sp.error))
            self.life.character = identity.current(self.conn)
            sp.committed = True
            log.info("%s the night's sleep cycle is done", self.life.sim_time)

    # ---------------------------------------------------------------- time

    def _catch_up(self) -> None:
        now = self.clock()
        self.carry += (now - self.last_real) * float(self.t["time_scale"]) / 60.0
        self.last_real = now
        cap = float(self.t["loop"]["max_step_minutes"])
        if self.carry > cap:
            log.warning("%.0f sim minutes behind; passing %d now", self.carry, cap)
            self.carry = cap
        whole = int(self.carry)
        if whole <= 0:
            return
        self.carry -= whole
        with store.transaction(self.conn):
            self.exec.advance(whole)
            lifemod.save(self.conn, self.life)
        if self.exec.held:
            self.carry = 0.0                                 # the clock waited for the sleep cycle

    # ---------------------------------------------------------------- a step

    def step(self) -> StepReport:
        self._catch_up()
        self._sleep_cycle()
        rep = StepReport(self.life.sim_time)
        now = self.clock()
        if self.doze_until is not None:
            if now < self.doze_until:
                rep.dozing = True
                return rep
            self.doze_until = None
        view = self.exec.running.view(self.life.sim_time) if self.exec.running else None
        due = self.scheduler.due(now, self.life.sim_time, view)
        if due is None:
            return rep
        recalled = self._recall(view)
        sleep_ids = [a.id for a in self.life.content.actions.values() if a.builtin == "sleep"]
        notable = memories.notable_today(self.conn, self.t, memories.last_wake(self.conn, sleep_ids))
        mind = mindmod.build(self.life, self.t, known=knowledge.known_ids(self.conn),
                             last_done=knowledge.minutes_since_done(self.conn, self.life.sim_time),
                             perception=self.exec.perception, activity=view,
                             recent=self.exec.recent(self.t["decisions"]["recent_activities"]),
                             memories=list(dict.fromkeys([r["text"] for r in recalled] + notable)),
                             seed=f"{self.seed}|{now}", capability_ok=self.exec.capability_ok,
                             notes=self._notes(), core_memories=memories.core_texts(self.conn))
        self.exec.perception = []
        self.scheduler.started(due, now, self.life.sim_time)
        t0 = self.clock()
        try:
            res = decisionmod.decide(self.backend, self.t["models"]["decision_model"], mind, due.kind, self.prompts,
                                     self.t)
        except BackendError as e:
            self.scheduler.finished()
            self.doze_until = self.clock() + float(self.t["loop"]["doze_seconds"])
            self.exec.perception = list(mind.perception) + self.exec.perception      # not lost
            log.warning("the brain is unavailable (%s): dozing", e)
            rep.dozing, rep.notes = True, [str(e)]
            return rep
        finally:
            ms = int((self.clock() - t0) * 1000)
        self.scheduler.finished()
        self._catch_up()                                                          # it thought; time passed
        d = res.decision
        cap = self.exec.prepare(d)
        with store.transaction(self.conn):
            did = self._record(due, d, res, mind, ms)
            memories.mark_recalled(self.conn, [r["id"] for r in recalled], self.life.sim_time)
            rep.result = self.exec.start(d, did, cap)
            lifemod.save(self.conn, self.life)
        rep.decision = d
        rep.sim_time = self.life.sim_time
        log.info("%s %s -> %s %s", self.life.sim_time, due.kind, d.action,
                 json.dumps(d.inputs, ensure_ascii=False) if d.inputs else "")
        return rep

    def _recall(self, view: dict | None) -> list[dict]:
        """Long-term memories for this moment (embedding the query happens here, outside any transaction)."""
        if self.t["memory"]["recall_k"] == 0 or not self.conn.execute("SELECT 1 FROM memories WHERE kind = 'long' "
                                                                       "LIMIT 1").fetchone():
            return []
        loc = self.life.content.locations[self.life.location].name
        q = memories.query_text(loc, view, self.exec.perception, mindmod.letters_on_mind(self.life, self.t))
        vec = memories.embed(self.backend, self.t["models"]["embed_model"], [q])[0]
        return memories.recall(self.conn, self.t, vec, self.life.sim_time)

    def _notes(self):
        prefs = memories.preferences(self.conn)
        if not prefs:
            return None
        words = {1: self.prompts["note_liked"], -1: self.prompts["note_disliked"]}
        minimum = self.t["sleep"]["pref_note_min"]

        def notes(action):
            n = memories.note_for(prefs, action, minimum)
            return [words[n]] if n else []
        return notes

    def _record(self, due, d: Decision, res, mind, ms: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions(sim_time, real_time, kind, triggers, action, inputs, thought, reasoning, preserve, "
            "letter, fallback, notes, attempts, messages, mind, dropped, ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.life.sim_time, datetime.now().isoformat(timespec="seconds"), due.kind,
             json.dumps([t.__dict__ for t in due.triggers]), d.action, json.dumps(d.inputs, ensure_ascii=False),
             d.thought, d.reasoning, int(d.preserve), d.letter, int(d.fallback), json.dumps(d.notes),
             json.dumps(res.attempts, ensure_ascii=False, default=str), json.dumps(res.messages, ensure_ascii=False),
             json.dumps(mind.to_dict(), ensure_ascii=False, default=str), json.dumps(res.dropped), ms))
        return cur.lastrowid

    def reload(self) -> None:
        """After an error: the transaction rolled back, so rebuild the in-memory state from the database."""
        self.life = lifemod.open_life(self.conn, self.life.content, self.t)
        self.exec = self._executor()
        self.scheduler.in_flight = False

    def run(self, steps: int | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        n = 0
        while steps is None or n < steps:
            try:
                self.step()
            except Exception:
                log.exception("step failed; state reloaded from the database")
                self.reload()
                sleep(float(self.t["loop"]["doze_seconds"]))
            n += 1
            sleep(float(self.t["loop"]["step_seconds"]))


class FakeClock:
    """A real-time clock that only moves when told to (tests and --fast runs)."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

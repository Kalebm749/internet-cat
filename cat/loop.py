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

from cat import clock as clock_mod, mail as mailmod
from cat import behaviour, content as contentmod, decision as decisionmod, identity, knowledge, life as lifemod, memories
from cat import mind as mindmod, sleepcycle, store
from cat.capabilities import Registry
from cat.config import Config
from cat.healthcheck import write_heartbeat
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
                 clock: Callable[[], float] = time.monotonic, seed: Any = None, cfg: Config | None = None):
        self.conn, self.t, self.backend, self.clock, self.cfg = conn, tuning, backend, clock, cfg
        self.last_mail_check: float | None = None
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
                    if sp.plan.summary_repeated:                  # the same entry again: progressively flatter
                        streak = sleepcycle.repeat_streak(self.conn)
                        mood = self.t["repetition"]["reflection_dup_mood"] * streak
                        lifemod.record_body(self.conn, self.life,
                                            self.life.body.apply(self.life.bodystate, {"mood": mood}, "same reflection"))
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

    def _import_mail(self) -> int:
        """New visitor letters from mail.db (written by the website) into the mailbox fact. Returns how many."""
        key = self.life.content.world.get("mailbox")
        if self.cfg is None or not key:
            return 0
        now = self.clock()
        if self.last_mail_check is not None and now - self.last_mail_check < self.cfg.settings["web"]["mail_poll_seconds"]:
            return 0
        self.last_mail_check = now
        mconn = mailmod.open_for_reading(self.cfg.mail_db_path)
        if mconn is None:
            return 0
        try:
            last = self.conn.execute("SELECT COALESCE(MAX(letter_id), 0) FROM mail_imports").fetchone()[0]
            rows = mailmod.new_letters(mconn, last)
        finally:
            mconn.close()
        if not rows:
            return 0
        with store.transaction(self.conn):
            for r in rows:
                self.life.world.append_raw(key, {"id": mailmod.letter_key(r["id"]), "sender": r["sender"],
                                                 "text": r["text"], "status": "unread", "received": self.life.sim_time})
                self.conn.execute("INSERT INTO mail_imports(letter_id, sim_time, real_time) VALUES (?, ?, ?)",
                                  (r["id"], self.life.sim_time, r["received_at"]))
            lifemod.save(self.conn, self.life)
        if key.split(".", 1)[0] == self.life.location and not self.life.asleep:
            self.exec.perception.append(self.texts["mail_arrived"])
            self.scheduler.note("mail", f"{len(rows)} new letter(s)")
        log.info("%d new letter(s) in the mailbox", len(rows))
        return len(rows)

    def step(self) -> StepReport:
        if self.cfg is not None:
            write_heartbeat(self.cfg.heartbeat_path)
        self._catch_up()
        self._sleep_cycle()
        self._import_mail()
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
                             repetition=behaviour.repetition_score(self.conn, self.t["repetition"]["recent"]),
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
        tvec = memories.embed(self.backend, self.t["models"]["embed_model"], [d.thought])[0] \
            if d.thought and not d.fallback else None
        with store.transaction(self.conn):
            did = self._record(due, d, res, mind, ms)
            memories.mark_recalled(self.conn, [r["id"] for r in recalled], self.life.sim_time)
            if d.thought and not d.fallback:
                self._thought(did, d.thought, tvec)
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
        """Menu notes: learned likes and dislikes (§2.4) and staleness (§6.2), in the prompt file's words."""
        prefs = memories.preferences(self.conn)
        words = {1: self.prompts["note_liked"], -1: self.prompts["note_disliked"]}
        minimum = self.t["sleep"]["pref_note_min"]
        below = self.t["repetition"]["note_below"]

        def notes(action):
            out = []
            n = memories.note_for(prefs, action, minimum) if prefs else 0
            if n:
                out.append(words[n])
            if not action.builtin:
                f = behaviour.freshness(self.conn, self.life.content, self.t, action, None, self.life.sim_time,
                                        self.life.world.values)
                if f.value < below:
                    out.append(self.prompts["note_repeated"])
            return out
        return notes

    def _thought(self, decision_id: int, text: str, vec) -> None:
        """Store the thought; a near-duplicate of a recent one costs a little mood and is noticed (§6.4)."""
        rp = self.t["repetition"]
        repeated = False
        if vec is not None and rp["thought_window"]:
            rows = self.conn.execute("SELECT embedding FROM thoughts WHERE embedding IS NOT NULL ORDER BY id DESC "
                                     "LIMIT ?", (rp["thought_window"],)).fetchall()
            v = memories.from_blob(memories.to_blob(vec))
            repeated = any(memories.cosine(memories.from_blob(r[0]), v) > rp["thought_dup_threshold"] for r in rows)
        self.conn.execute("INSERT INTO thoughts(decision_id, sim_time, text, embedding, repeated) VALUES (?, ?, ?, ?, ?)",
                          (decision_id, self.life.sim_time, text, memories.to_blob(vec), int(repeated)))
        if repeated:
            self.exec.perception.append(self.texts["same_thought"])
            last = self.conn.execute("SELECT MAX(sim_time) FROM body_changes WHERE cause = 'same thought'").fetchone()[0]
            if last is None or (clock_mod.parse(self.life.sim_time) - clock_mod.parse(last)).total_seconds() / 60 \
                    >= rp["thought_dup_cooldown_minutes"]:
                changes = self.life.body.apply(self.life.bodystate, {"mood": rp["thought_dup_mood"]}, "same thought")
                lifemod.record_body(self.conn, self.life, changes)

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

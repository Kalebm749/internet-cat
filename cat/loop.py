"""The engine loop of the new life (docs/rewrite-contracts.md §3.3): the only writer of nit.db.

Each step:
  1. sim time catches up with real time (x time_scale), carried in whole minutes: regrowth, body, events,
     the running activity's end                                                   (one transaction)
  2. if a decision is due (cat/scheduler.py): build the Mind, ask the decision model    (no transaction)
  3. the time spent thinking passes (the clock keeps running while it thinks)    (one transaction)
  4. record the decision and start the chosen action                             (one transaction)
  5. run its capability, if any, while the running activity remains visible      (no transaction)
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
from cat import behaviour, content as contentmod, decision as decisionmod, identity, knowledge, life as lifemod, loops as loopsmod, memories
from cat import beliefs as beliefsmod, inner as innermod, mind as mindmod, people as peoplemod, projects as projectsmod, sleepcycle, starving, store, wishes
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
                                        lp.life.character.sheet, lp.life.sim_time, lp.texts)
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
        with store.transaction(conn):                        # wishes the author granted since the last start
            told = wishes.grant(conn, self.life, self.texts)
            lifemod.save(conn, self.life)
        self.exec.perception += told
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
                    sleepcycle.commit(self.conn, sp.plan, self.life.sim_time, self.t)
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
                peoplemod.touch(self.conn, r["sender"], self.life.sim_time, 0.05, 0.0, 0)
                self.conn.execute("INSERT INTO mail_imports(letter_id, sim_time, real_time) VALUES (?, ?, ?)",
                                  (r["id"], self.life.sim_time, r["received_at"]))
            lifemod.save(self.conn, self.life)
        if key.split(".", 1)[0] == self.life.location and not self.life.asleep:
            self.exec.perception.append(self.texts["mail_arrived"])
            self.scheduler.note("mail", f"{len(rows)} new letter(s)")
        log.info("%d new letter(s) in the mailbox", len(rows))
        return len(rows)

    def _prepare_running_activity(self) -> None:
        """Finish a capability after its running row is visible to live status views."""
        running = self.exec.running
        if running is None or not running.action.capability or running.cap is not None:
            return
        cap = self.exec.prepare_running()
        if cap is not None:
            with store.transaction(self.conn):
                self.exec.attach_capability(cap)
                lifemod.save(self.conn, self.life)

    def step(self) -> StepReport:
        if self.cfg is not None:
            write_heartbeat(self.cfg.heartbeat_path)
        # A process may have restarted while a capability was being prepared. Resume it before
        # passing sim time so the persisted activity cannot finish without its result.
        self._prepare_running_activity()
        self._catch_up()
        self._form_memories()
        self._resurface()
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
        self._place_awareness()
        recalled = self._recall(view)
        sleep_ids = [a.id for a in self.life.content.actions.values() if a.builtin == "sleep"]
        notable = memories.notable_today(self.conn, self.t, memories.last_wake(self.conn, sleep_ids))
        mind = mindmod.build(self.life, self.t, known=knowledge.known_ids(self.conn),
                             last_done=knowledge.minutes_since_done(self.conn, self.life.sim_time),
                             perception=self.exec.perception, activity=view,
                             recent=self.exec.recent(self.t["decisions"]["recent_activities"]),
                             memories=list(dict.fromkeys([r["text"] for r in recalled] + notable
                                                         + [self.prompts["faded"].format(gist=g)
                                                            for g in getattr(self, "_faded", [])])),
                             repetition=behaviour.repetition_score(self.conn, self.t["repetition"]["recent"]),
                             seed=f"{self.seed}|{now}", capability_ok=self.exec.capability_ok,
                             notes=self._notes(), core_memories=memories.core_texts(self.conn),
                             count_done=self.exec.count_done,
                             dwelling=behaviour.dwelling(self.conn, self.life.sim_time, self.t),
                             loops=loopsmod.on_mind(self.conn, self.t),
                             mulling=behaviour.mulling(self.conn, self.t),
                             inner=innermod.describe(self.life.inner, self.life.content.world.get("inner_words"),
                                                     self.t["inner"]),
                             temperament_words=self._temperament_words(),
                             projects=projectsmod.on_mind(self.conn, self.prompts),
                             people=peoplemod.on_mind(self.conn, self.prompts),
                             beliefs=beliefsmod.lines_for_now(self.conn, self.t["beliefs"], self.life.location,
                                                              self.life.world.values.get("world.weather")),
                             menu_size=self.t["decisions"]["menu_size"], protected=self._protected_options(),
                             carry=self._carry_on(view))
        self.exec.perception = []
        self.scheduler.started(due, now, self.life.sim_time)
        t0 = self.clock()
        forced = self._unthinking(due, mind)       # starving, a planned move, or nothing new: no model asked
        try:
            if forced is not None:
                res = decisionmod.Result(forced[0], [], [], [])
            else:
                gut, temp = self._manner(due.kind)
                res = decisionmod.decide(self.backend, self.t["models"]["decision_model"], mind, due.kind,
                                         self.prompts, self.t,
                                         reconsider=lambda d: self._reconsider_stale_choice(mind, d), gut=gut,
                                         options={"temperature": temp} if temp is not None else None)
                if gut:
                    res.decision.notes.append("gut")
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
        if d.action == "continue" and mind.carry and view is None:        # "carry on" is the same action again
            d.action, d.inputs = mind.carry["action"], dict(mind.carry["inputs"])
            d.notes.append("carried on")
        with store.transaction(self.conn):
            did = self._record(due, d, res, mind, ms)
            if forced is not None and starving.NOTE in d.notes:
                if forced[1]:
                    self.life.world.apply(forced[1], {"now": self.life.sim_time}, self.rng, by="world")
                    self.exec.perception.append(self.texts["starving_restock"])
                if d.action != "continue":
                    self.exec.perception.append(self.texts["starving_go"])
            memories.mark_recalled(self.conn, [r["id"] for r in recalled], self.life.sim_time,
                                   self.t["memory"]["recall_gain"])
            rep.result = self.exec.start(d, did)
            started = rep.result.get("status") in ("started", "continue")
            self._queue_set(forced[2] if forced is not None else (d.then if started else []))
            self._sig = self._signature()
            lifemod.save(self.conn, self.life)
        if rep.result.get("status") == "started":
            self._record_subject(d, rep.result.get("activity_id"))
            self._prepare_running_activity()
        tvec = memories.embed(self.backend, self.t["models"]["embed_model"], [d.thought])[0] \
            if d.thought and not d.fallback else None
        with store.transaction(self.conn):
            if d.thought and not d.fallback:
                self._thought(did, d.thought, tvec)
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
        self._faded = memories.faded_cue(self.conn, self.t["memory"], vec)
        return memories.recall(self.conn, self.t, vec, self.life.sim_time)

    def _notes(self):
        """Menu notes: learned likes and dislikes (§2.4) and staleness (§6.2), in the prompt file's words."""
        prefs = memories.preferences(self.conn)
        words = {1: self.prompts["note_liked"], -1: self.prompts["note_disliked"]}
        minimum = self.t["sleep"]["pref_note_min"]
        below = self.t["repetition"]["note_below"]

        felt = memories.feelings_by_action(self.conn, self.t["memory"], self.life.sim_time)
        thinks = beliefsmod.notes_by_action(self.conn, self.t["beliefs"])
        done = {k: v.times_done for k, v in knowledge.all_known(self.conn).items()}

        def notes(action):
            out = []
            out += [self.prompts["belief_note"].format(statement=s) for s in thinks.get(action.id, [])[:1]]
            if innermod.practice(done.get(action.id, 0), self.t["inner"]) >= 0.6:
                out.append(self.prompts["practised"])
            f = felt.get(action.id, 0.0)
            if abs(f) >= self.t["memory"]["memory_note_min"]:
                out.append(self.prompts["memory_good" if f > 0 else "memory_bad"])
            n = memories.note_for(prefs, action, minimum) if prefs else 0
            if n:
                out.append(words[n])
            if action.builtin == "sleep":
                night = clock_mod.hour_in_range(clock_mod.parse(self.life.sim_time),
                                                 *self.t["sleep"]["night_hours"])
                if self.life.bodystate.energy <= 0.5:
                    out.append("your energy is low; sleep restores it")
                elif night:
                    out.append("it is night; sleeping now will restore your energy")
            out += innermod.relief_notes(self.life.inner, self.life.content.world.get("inner_words"), self.t["inner"],
                                         action.inner, self.prompts["need_relief"])
            if not action.builtin:
                f = behaviour.freshness(self.conn, self.life.content, self.t, action, None, self.life.sim_time,
                                        self.life.world.values, location=self.life.location)
                if f.value < below:
                    out.append(self.prompts["note_repeated"])
            return out
        return notes

    def _reconsider_stale_choice(self, mind, decision: Decision) -> list[str]:
        """One extra attempt when the choice is stale: an action that cannot happen or has felt tired lately, a
        subject it keeps coming back to. Never about its thoughts: quoting a thought back made a small model copy it
        (spec §6.4), and each retry is another full model call."""
        return self._stale_action_problems(mind, decision)

    # ---------------------------------------------------------------- deciding without a model (event-driven)

    def _signature(self) -> tuple:
        """What its situation amounts to: where, which body bands, which needs press, what it is doing."""
        ex = self.exec
        return (self.life.location, tuple(sorted(ex._band_indexes().items())),
                tuple(innermod.pressing(self.life.inner, self.t["inner"])), ex.running.action.id if ex.running else None)

    def _queue(self) -> list[dict]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'queue'").fetchone()
        return json.loads(row[0]) if row else []

    def _queue_set(self, items: list[dict]) -> None:
        """Persist the moves it has planned (inside the caller's transaction)."""
        self.conn.execute("INSERT INTO meta(key, value) VALUES ('queue', ?) ON CONFLICT(key) DO UPDATE SET "
                          "value = excluded.value", (json.dumps(items),))

    def _unthinking(self, due, mind) -> tuple[Decision, list, list] | None:
        """A decision made without asking the model, with the world ops to apply first and the queue left after:
        hunger taking over (§2.5); the next move it planned, when nothing new has happened; carrying on with a long
        activity at a check-in when its situation has not changed. Anything new (an event, a letter, a need that
        became pressing, something it noticed) drops the plan and it decides again."""
        s = starving.decide(self.life, self.exec, self.t)
        if s is not None:
            return s[0], s[1], []
        kinds = {t.kind for t in due.triggers}
        quiet = not mind.perception
        queue = self._queue()
        if due.kind == "check_in" and kinds == {"check_in"} and quiet and getattr(self, "_sig", None) == self._signature():
            return Decision(kind="check_in", action="continue", notes=["unchanged"]), [], queue
        if queue and due.kind == "full" and kinds <= {"activity_finished", "interval"} and quiet:
            item = queue[0]
            if self.exec.check(item["action"], item["inputs"]) is None:
                return Decision(kind="full", action=item["action"], inputs=dict(item["inputs"]), notes=["queued"]), \
                    [], queue[1:]
        return None

    def _protected_options(self) -> set[str]:
        """What must always be among the options: sleep when worn out, food when hungry. Offered, never chosen for it."""
        dec = self.t["decisions"]
        out: set[str] = set()
        if self.life.bodystate.energy <= dec["sleep_offer_energy"]:
            out |= {a.id for a in self.life.content.actions.values() if a.builtin == "sleep"}
        food = (self.life.content.world.get("starving") or {}).get("eat")
        if food and self.life.bodystate.hunger >= dec["food_offer_hunger"]:
            out.add(food)
        return out

    def _carry_on(self, view) -> dict | None:
        """The last thing it did here, if it could simply carry on with it (nothing running, no words to repeat)."""
        if view is not None:
            return None
        row = self.conn.execute("SELECT action_id, inputs, location FROM activities WHERE status = 'finished' "
                                "ORDER BY id DESC LIMIT 1").fetchone()
        spec = self.life.content.actions.get(row["action_id"]) if row else None
        if spec is None or row["location"] != self.life.location or spec.builtin in ("move", "sleep", "inspect") \
                or any(s["type"] == "text" for s in spec.inputs.values()):
            return None
        return {"action": spec.id, "inputs": json.loads(row["inputs"] or "{}"), "name": spec.name}

    def _manner(self, kind: str) -> tuple[bool, float | None]:
        """How it decides this time: by its first pull (a short, fast answer) with a chance that grows with
        impulsiveness, and a temperature that rises with it (a cautious creature is steadier)."""
        if kind == "check_in":
            return False, None
        inner_t = self.t["inner"]
        imp = innermod.trait(self.life.character.sheet.get("temperament"), "impulsiveness")
        base = float(self.cfg.settings["llm_temperature"]["decide"]) if self.cfg is not None else 0.8
        return self.rng.random() < imp * inner_t["gut_scale"], round(max(0.2, base + inner_t["temperature_span"] * (imp - 0.5)), 3)

    def _resurface(self) -> None:
        """Now and then an unsettled concern comes back to mind by itself and asks for a decision (never a task)."""
        if self.life.asleep:
            return
        with store.transaction(self.conn):
            pick = loopsmod.resurface(self.conn, self.t, self.life.sim_time, self.rng)
        if pick is not None:
            self.exec.perception.append(self.texts["concern_back"].format(text=pick["text"]))
            self.scheduler.note("concern", pick["text"])

    def _form_memories(self) -> None:
        """What just happened leaves an impression only if it mattered to *this* creature (its significance), and the
        same thing again strengthens the impression it already has. The embedding is made outside the transaction."""
        if not self.exec.formed:
            return
        batch, self.exec.formed = self.exec.formed, []
        m = self.t["memory"]
        temp = self.life.character.sheet.get("temperament")
        keep = []
        for f in batch:
            s = memories.significance(f["importance"], f["mood_base"], f["remember"], f["freshness"], temp, m)
            if s >= m["short_min"]:
                keep.append((f, s))
        if not keep:
            return
        vecs = memories.embed(self.backend, self.t["models"]["embed_model"], [f["text"] for f, _ in keep])
        with store.transaction(self.conn):
            for (f, s), v in zip(keep, vecs):
                memories.add_memory(self.conn, m, self.life.sim_time, f["text"], v, s,
                                    feeling=memories.feeling_of(f["mood_base"]), action_id=f["action_id"],
                                    location=f["location"], source=f["id"])

    def _temperament_words(self) -> list[str]:
        """The few ways it is strongly inclined, as clauses for "You tend to: ..." (high >= 0.65, low <= 0.35)."""
        temp = self.life.character.sheet.get("temperament") or {}
        out = []
        for d in innermod.DIMENSIONS:
            v = temp.get(d)
            if v is not None and (v >= 0.65 or v <= 0.35):
                out.append(self.prompts[f"temp_{d}_{'high' if v >= 0.65 else 'low'}"])
        return out

    def _stale_action_problems(self, mind, decision: Decision) -> list[str]:
        if decision.kind == "check_in":
            if self.life.bodystate.energy <= 0.2 and decision.action != "stop":
                return ["Your energy is critically low. Stop this activity so you can consider sleep."]
            return []
        action = self.life.content.actions.get(decision.action)
        if action is None:
            return []
        why = self.exec.duplicate(decision.action, decision.inputs)
        if why:                                              # it is already there: say so and let it choose again
            return [self.texts["rejected"].format(name=action.name, reason=why)]
        if self.life.bodystate.energy <= 0.2 and decision.action != "sleep" and mind.option("sleep"):
            return ["Your energy is critically low. Sleep is available and restores energy; reconsider before continuing."]
        threshold = self.t["repetition"]["note_below"]
        subject = behaviour.subject_text(action, decision.inputs)
        if subject:                                          # the same subject in new words (§6.6)
            found = behaviour.echoes(self.conn, self._subject_vec(subject), self.life.sim_time, self.t)
            if found and behaviour.subject_freshness(found, self.t) < threshold:
                return [self.prompts["subject_repeat"].format(count=len(found), example=found[0].text)]
        selected = behaviour.freshness(self.conn, self.life.content, self.t, action, decision.inputs,
                                       self.life.sim_time, self.life.world.values,
                                 location=self.life.location)
        if selected.value >= threshold:
            return []
        fresh = []
        for option in mind.options:
            if option.id == decision.action:
                continue
            candidate = self.life.content.actions.get(option.id)
            if candidate is None:
                continue
            score = behaviour.freshness(self.conn, self.life.content, self.t, candidate, None,
                                        self.life.sim_time, self.life.world.values,
                                 location=self.life.location)
            if score.value >= threshold:
                fresh.append(candidate.name)
        if len(fresh) < 2:
            return []
        alternatives = ", ".join(fresh[:3])
        return [f"You chose {action.name}, whose freshness is low ({selected.value:.2f}). "
                f"Other available actions have felt fresher: {alternatives}. Repeating may still matter; "
                "if something has changed, say what. Otherwise reconsider and choose a different action."]

    def _place_awareness(self) -> None:
        """Where it could go, from its own history, and how long it has been here (before building the Mind)."""
        stay, last = behaviour.stay_and_visits(self.conn, self.life.content, self.life.location, self.life.sim_time)
        self.life.stay_minutes = stay
        notes = {}
        for x in self.life.content.locations[self.life.location].exits:
            if x not in last:
                notes[x] = self.prompts["exit_never"]
            elif last[x] < 60:
                notes[x] = self.prompts["exit_recent"]
            else:
                notes[x] = self.prompts["exit_ago"].format(hours=round(last[x] / 60))
        self.life.exit_notes = notes

    def _subject_vec(self, text: str):
        """The embedding of a subject (cached: the reconsider guard and the record use the same words)."""
        cache = self.__dict__.setdefault("_subjects", {})
        if text not in cache:
            if len(cache) > 64:
                cache.clear()
            cache[text] = memories.embed(self.backend, self.t["models"]["embed_model"], [text])[0]
        return cache[text]

    def _record_subject(self, d: Decision, activity_id: int | None) -> None:
        """Remember what this activity is about; a subject close to recent ones makes it less fresh (§6.6)."""
        action = self.life.content.actions.get(d.action)
        text = behaviour.subject_text(action, d.inputs) if action else None
        if not text or activity_id is None:
            return
        vec = self._subject_vec(text)
        found = behaviour.echoes(self.conn, vec, self.life.sim_time, self.t)
        sf = behaviour.subject_freshness(found, self.t)
        with store.transaction(self.conn):
            self.conn.execute("INSERT INTO subjects(activity_id, action_id, sim_time, text, embedding) "
                              "VALUES (?, ?, ?, ?, ?)",
                              (activity_id, d.action, self.life.sim_time, text, memories.to_blob(vec)))
            row = self.conn.execute("SELECT freshness, repeat_parts FROM activities WHERE id = ?",
                                    (activity_id,)).fetchone()
            if row is not None and sf < row["freshness"]:
                parts = json.loads(row["repeat_parts"] or "{}")
                parts["subject"] = {"freshness": sf, "close_to": [e.text for e in found[:3]]}
                self.conn.execute("UPDATE activities SET freshness = ?, repeat_parts = ? WHERE id = ?",
                                  (sf, json.dumps(parts, ensure_ascii=False), activity_id))
                if self.exec.running is not None and self.exec.running.id == activity_id:
                    self.exec.running.freshness = sf

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
            "letter, fallback, notes, attempts, messages, mind, dropped, ms, factors) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.life.sim_time, datetime.now().isoformat(timespec="seconds"), due.kind,
             json.dumps([t.__dict__ for t in due.triggers]), d.action, json.dumps(d.inputs, ensure_ascii=False),
             d.thought, d.reasoning, int(d.preserve), d.letter, int(d.fallback), json.dumps(d.notes),
             json.dumps(res.attempts, ensure_ascii=False, default=str), json.dumps(res.messages, ensure_ascii=False),
             json.dumps(mind.to_dict(), ensure_ascii=False, default=str), json.dumps(res.dropped), ms,
             json.dumps(self._factors(d, mind), ensure_ascii=False, default=str)))
        return cur.lastrowid

    def _factors(self, d: Decision, mind) -> dict:
        """The structured facts behind a decision (not the model's own explanation): how it was made, what was drawn,
        what it felt, what bore on which option. The dashboard reads this."""
        tier = next((n for n in ("queued", "unchanged", "gut", "carried on", starving.NOTE) if n in d.notes), "full")
        return {"tier": tier, "menu": mind.menu_info, "inner": self.life.inner.as_dict(),
                "pressing": innermod.pressing(self.life.inner, self.t["inner"]),
                "body": {k: round(v, 3) for k, v in self.life.bodystate.as_dict().items()},
                "option_notes": {o.id: o.notes for o in mind.options if o.notes},
                "memories": list(mind.memories), "beliefs": list(mind.beliefs), "projects": list(mind.projects),
                "concerns": list(mind.loops), "people": list(mind.people), "mulling": mind.mulling, "queue": d.then}

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

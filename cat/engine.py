"""The tick loop. One tick = world events -> brain -> narrator -> apply -> record (one DB
transaction for the last two; LLM calls happen outside it)."""
from __future__ import annotations

import logging
import random
import signal
import time
from datetime import datetime
from typing import Callable

from collections import deque

from cat import brain, character, clock, db, effects, narrator
from cat.config import Config
from cat.db import dumps
from cat.llm import BackendError, LLMBackend, cosine
from cat.world import WorldState

log = logging.getLogger("cat.engine")


def _now_real() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Engine:
    def __init__(self, cfg: Config, backend: LLMBackend, conn=None, rng: random.Random | None = None):
        self.cfg = cfg
        self.backend = backend
        self.rng = rng or random.Random()
        self.conn = conn or db.connect(cfg.db_path)
        db.init_db(self.conn)
        self.stop_requested = False
        self.recent_action_vecs: deque = deque(maxlen=self.cfg.settings["repetition_penalty"]["window"])
        self._bootstrap()
        self._load()

    # ---------- state ----------
    def _bootstrap(self) -> None:
        if db.get_state(self.conn) is not None:
            return
        cfg = self.cfg
        start = cfg.settings["sim_start"]
        needs = character.initial_needs(cfg)
        world = WorldState.seed(self.conn, cfg, start)
        state = {"sim_time": start, "area": cfg.character["start_area"], "needs": needs,
                 "mood": character.initial_mood(needs, cfg),
                 "energy": character.energy(needs, cfg), "weather": world.weather,
                 "tendencies": dict(cfg.character["tendencies"]), "is_asleep": False,
                 "brain_online": True}
        with db.transaction(self.conn):
            world.sync(self.conn)
            db.save_state(self.conn, state, _now_real())
            db.set_meta(self.conn, "first_sim_time", start)
            db.set_meta(self.conn, "brain_model", cfg.settings["brain_model"])

    def _load(self) -> None:
        self.state = db.get_state(self.conn)
        self.state["is_asleep"] = bool(self.state["is_asleep"])
        self.world = WorldState.load(self.conn, self.cfg, self.state["weather"])
        self.first_dt = clock.parse(db.get_meta(self.conn, "first_sim_time"))
        row = self.conn.execute(
            "SELECT outcome FROM ticks WHERE kind='tick' ORDER BY id DESC LIMIT 1").fetchone()
        self.last_outcome = row["outcome"] if row else None

    # ---------- prompts ----------
    def _brain_context(self, fired: list[dict]) -> brain.BrainContext:
        cfg, st, s = self.cfg, self.state, self.cfg.settings
        n = s["recent_actions_shown"]
        recent = [r["action"] for r in self.conn.execute(
            "SELECT action FROM ticks WHERE kind='tick' ORDER BY id DESC LIMIT ?", (n,))][::-1]
        k = s["preferences_shown"]
        prefs = [dict(r) for r in self.conn.execute(
            "SELECT subject, valence, reason FROM preferences WHERE valence>0 "
            "ORDER BY valence DESC LIMIT ?", (k,))] + [dict(r) for r in self.conn.execute(
            "SELECT subject, valence, reason FROM preferences WHERE valence<0 "
            "ORDER BY valence ASC LIMIT ?", (k,))]
        return brain.BrainContext(
            sim_time=st["sim_time"], area_id=st["area"], needs=st["needs"], energy=st["energy"],
            mood=st["mood"], tendencies=st["tendencies"], weather=self.world.weather,
            objects_here=self.world.here(st["area"]), inventory=self.world.inventory(),
            last_outcome=self.last_outcome, events=[e["text"] for e in fired],
            recent_actions=recent, preferences=prefs)

    def _narrator_context(self) -> str:
        st = self.state
        return narrator.describe_world(self.cfg, st["sim_time"], st["area"], self.world.weather,
                                       self.world.here(st["area"]), self.world.inventory())

    def _repetition_multiplier(self, action: str) -> float:
        """Embed the action; shrink gains if it resembles a recent one. Embedding trouble just
        means no penalty this tick."""
        try:
            vec = self.backend.embed(self.cfg.settings["embed_model"], [action])[0]
        except BackendError:
            return 1.0
        mult = effects.repetition_multiplier(vec, list(self.recent_action_vecs), self.cfg, cosine)
        self.recent_action_vecs.append(vec)
        return mult

    # ---------- the tick ----------
    def tick(self) -> dict:
        try:
            return self._tick()
        except BaseException:
            # In-memory state may be ahead of the DB after a failed transaction: reload it.
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            self._load()
            raise

    def _tick(self) -> dict:
        cfg, st, s = self.cfg, self.state, self.cfg.settings
        now = st["sim_time"]
        dt = clock.parse(now)

        # 1. world events (perceived at the start of the tick)
        fired = self.world.roll_events(self.rng, dt, st["area"], now)
        nudge = st["mood"].get("event_nudge", 0.0) * s["mood"]["event_decay"] \
            + sum(e["mood"] for e in fired)

        # 2+3. brain and narrator (LLM calls, outside any transaction)
        result = None
        online = self.backend.is_online()
        if online:
            try:
                b = brain.think(self.backend, cfg, self._brain_context(fired))
                n = narrator.resolve(self.backend, cfg, b["action"], self._narrator_context())
                result = (b, n)
            except BackendError as e:
                log.warning("brain/narrator unavailable (%s): dozing", e)
                online = False

        # 4. apply
        before = dict(st["needs"])
        if result:
            b, n = result
            minutes = n["duration"]
            fx = effects.apply_changes(n["changes"], self.world, st["area"], cfg, now)
            mult = self._repetition_multiplier(b["action"])
            needs = character.decay(before, minutes, cfg)
            needs = character.apply_gains(needs, n["gains"], n["effort"], cfg,
                                          consumed=fx.consumed, multiplier=mult)
            feeling = b["feeling"]
            new_area = fx.area
            row = {"kind": "tick", "reaction": b["reaction"], "thought": b["thought"],
                   "action": b["action"], "intent": b["intent"], "outcome": n["outcome"],
                   "gains": n["gains"], "effort": n["effort"], "duration": minutes,
                   "changes": fx.applied, "rejected": fx.rejected}
        else:
            minutes = s["doze"]["sim_minutes"]
            needs = character.decay(before, minutes, cfg)
            needs = character.restore(needs, s["doze"]["energy_restore"], cfg)
            feeling = None
            new_area = st["area"]
            row = {"kind": "doze", "reaction": None, "thought": None, "action": None,
                   "intent": None, "outcome": "dozing", "gains": {}, "effort": None,
                   "duration": minutes, "changes": [], "rejected": []}

        for e in fired:  # event-spawned objects are world changes too (audit trail)
            if e["spawned"]:
                o = self.world.objects[e["spawned"]]
                row["changes"] = row["changes"] + [{"op": "create", "source": "event",
                                                    "object_id": o["id"], "area": o["area"],
                                                    "name": o["name"], "label": o["name"]}]
        mood = character.compute_mood(needs, before, nudge, feeling, cfg)
        new_state = {**st, "area": new_area, "sim_time": clock.advance(now, minutes), "needs": needs,
                     "mood": mood, "energy": character.energy(needs, cfg),
                     "weather": self.world.weather, "brain_online": online}

        # 5. record: one transaction; a crash leaves the previous state intact
        real = _now_real()
        with db.transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO ticks(kind,sim_time,real_time,area,reaction,thought,action,intent,"
                "outcome,gains,effort,duration,changes,rejected_changes,needs_after,mood_after) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["kind"], now, real, st["area"], row["reaction"], row["thought"],
                 row["action"], row["intent"], row["outcome"], dumps(row["gains"]), row["effort"],
                 row["duration"], dumps(row["changes"]), dumps(row["rejected"]), dumps(needs),
                 dumps(mood)))
            tick_id = cur.lastrowid
            for e in fired:
                self.conn.execute("INSERT INTO events(tick_id,sim_time,area,text) VALUES(?,?,?,?)",
                                  (tick_id, now, e["area"] or st["area"], e["text"]))
            self.world.sync(self.conn)
            db.save_state(self.conn, new_state, real)
            if online:
                db.set_meta(self.conn, "brain_last_seen", real)
        self.state = new_state
        if result:
            self.last_outcome = row["outcome"]
        return {"id": tick_id, "kind": row["kind"], "sim_time": now, "area": st["area"],
                "action": row["action"], "outcome": row["outcome"], "mood": mood["word"],
                "energy": new_state["energy"], "fired": [e["text"] for e in fired]}

    # ---------- the loop ----------
    def run(self, ticks: int | None = None, interval: float | None = None,
            on_tick: Callable[[dict], None] | None = None,
            sleep: Callable[[float], None] = time.sleep) -> int:
        """Run until `ticks` is reached or stop() is called. Never raises on a bad tick:
        errors are logged and the loop carries on."""
        interval = self.cfg.settings["tick_interval_seconds"] if interval is None else interval
        done = 0
        while not self.stop_requested and (ticks is None or done < ticks):
            try:
                info = self.tick()
                if on_tick:
                    on_tick(info)
            except Exception:
                log.exception("tick failed; state reloaded, continuing")
            done += 1
            if interval and not self.stop_requested and (ticks is None or done < ticks):
                sleep(interval)
        return done

    def install_signal_handlers(self) -> None:
        def _stop(signum, frame):
            log.info("signal %s: stopping after this tick", signum)
            self.stop_requested = True
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

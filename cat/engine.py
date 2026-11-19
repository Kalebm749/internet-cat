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

from cat import brain, character, clock, db, downtime, effects, narrator, sleep
from cat.config import Config
from cat.db import dumps
from cat.healthcheck import write_heartbeat
from cat.llm import BackendError, LLMBackend, cosine
from cat.memory import Memory, build_query, importance_for, notable_reasons
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
        self.time = time.time          # injectable for tests
        self.stop_requested = False
        self._online_cache: bool | None = None
        self.recent_action_vecs: deque = deque(maxlen=self.cfg.settings["repetition_penalty"]["window"])
        self.memory = Memory(self.conn, cfg, backend)
        self._bootstrap()
        self._reconcile_config()
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

    def _reconcile_config(self) -> None:
        """The YAML can be edited while a database already exists (it is mounted, not baked in).
        Bring the saved state in line with it so a changed config never makes every tick fail:
        new needs/tendencies start at their configured value, removed ones are dropped, and a
        creature standing in an area that no longer exists goes back to the start area.
        (World objects are seeded once, on first start; a reset is needed to re-seed them.)"""
        st = db.get_state(self.conn)
        cfg, notes = self.cfg, []
        needs = {k: v for k, v in st["needs"].items() if k in cfg.character["needs"]}
        for k, spec in cfg.character["needs"].items():
            if k not in needs:
                needs[k] = float(spec["start"])
                notes.append(f"new need '{k}' starts at {spec['start']}")
        notes += [f"dropped need '{k}'" for k in st["needs"] if k not in needs]
        tend = {k: v for k, v in st["tendencies"].items() if k in cfg.character["tendencies"]}
        for k, v in cfg.character["tendencies"].items():
            if k not in tend:
                tend[k] = float(v)
                notes.append(f"new tendency '{k}' starts at {v}")
        notes += [f"dropped tendency '{k}'" for k in st["tendencies"] if k not in tend]
        area = st["area"]
        if area not in cfg.world["areas"]:
            area = cfg.character["start_area"]
            notes.append(f"area '{st['area']}' no longer exists: moved to '{area}'")
        if not notes:
            return
        log.warning("config changed since the last run; adjusting saved state: %s", "; ".join(notes))
        st = {**st, "needs": needs, "tendencies": tend, "area": area,
              "energy": character.energy(needs, cfg), "is_asleep": bool(st["is_asleep"]),
              "brain_online": bool(st["brain_online"])}
        with db.transaction(self.conn):
            db.save_state(self.conn, st, _now_real())

    def _load(self) -> None:
        self.state = db.get_state(self.conn)
        self.state["is_asleep"] = bool(self.state["is_asleep"])
        self.world = WorldState.load(self.conn, self.cfg, self.state["weather"])
        self.first_dt = clock.parse(db.get_meta(self.conn, "first_sim_time"))
        row = self.conn.execute(
            "SELECT outcome FROM ticks WHERE kind='tick' ORDER BY id DESC LIMIT 1").fetchone()
        self.last_outcome = row["outcome"] if row else None
        # what the previous non-sleep tick was: 'doze' => the first prompt notes the doze
        k = self.conn.execute(
            "SELECT kind FROM ticks ORDER BY id DESC LIMIT 1").fetchone()
        self.last_kind = k["kind"] if k else None

    # ---------- prompts ----------
    def _brain_context(self, fired: list[dict], long_mems: list[dict] | None = None) -> brain.BrainContext:
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
            recent_actions=recent, preferences=prefs, just_woke_from_doze=self.last_kind in ("doze", "catchup"),
            long_memories=[m["text"] for m in (long_mems or [])],
            short_memories=[m["text"] for m in self.memory.recent_short()])

    def _recall(self, fired: list[dict]) -> list[dict]:
        """Relevant long-term memories for this situation (empty if there are none yet)."""
        st = self.state
        if not self.conn.execute("SELECT 1 FROM memories WHERE kind='long' LIMIT 1").fetchone():
            return []
        q = build_query({"area": self.cfg.world["areas"][st["area"]]["name"],
                         "objects": [o["name"] for o in self.world.here(st["area"])],
                         "mood": st["mood"]["word"],
                         "last_outcome": " ".join([e["text"] for e in fired] + [self.last_outcome or ""]).strip()})
        return self.memory.recall(self.memory.embed_one(q), st["sim_time"])

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
    def _online(self) -> bool:
        """Brain health check (GET /api/tags), cached for the duration of one tick."""
        if self._online_cache is None:
            self._online_cache = self.backend.is_online()
        return self._online_cache

    def tick(self) -> dict:
        self._online_cache = None
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

        # sleeping, or too exhausted at night to carry on: no brain call this tick
        if st["is_asleep"]:
            return self._rest_tick(fired, nudge, onset=False)
        if clock.is_night(dt, s["phases"]) and st["energy"] < s["sleep"]["fall_asleep_energy_below"]:
            return self._rest_tick(fired, nudge, onset=True)

        # 2+3. brain and narrator (LLM calls, outside any transaction)
        result = None
        recalled: list[dict] = []
        online = self._online()
        if online:
            try:
                recalled = self._recall(fired)
                b = brain.think(self.backend, cfg, self._brain_context(fired, recalled))
                n = narrator.resolve(self.backend, cfg, b["action"], self._narrator_context())
                result = (b, n)
            except BackendError as e:
                log.warning("brain/narrator unavailable (%s): dozing", e)
                online = False
                self._online_cache = False

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

        mood = character.compute_mood(needs, before, nudge, feeling, cfg)
        new_state = {**st, "area": new_area, "sim_time": clock.advance(now, minutes),
                     "needs": needs, "mood": mood, "energy": character.energy(needs, cfg),
                     "weather": self.world.weather, "brain_online": online}

        # the creature chose to sleep (honoured when tired enough, or at night)
        falls_asleep = bool(result) and result[0]["wants_sleep"] and (
            new_state["energy"] < s["sleep"]["wants_sleep_energy_below"]
            or clock.is_night(dt, s["phases"]))
        new_state["is_asleep"] = falls_asleep

        # notable moment -> short-term memory (embedding is an HTTP call: do it before the txn)
        short_mem = None
        if result:
            why = notable_reasons(cfg, before, needs, feeling,
                                  row["changes"], row["rejected"])
            if why:
                t = clock.time_label(dt)
                text = (f"{t}, {cfg.world['areas'][st['area']]['name']}: {row['action']}. "
                        f"{row['outcome']}")
                if row["reaction"]:
                    text += f' You felt: "{row["reaction"]}"'
                short_mem = self.memory.prepare(text, importance_for(cfg, len(why)), now)

        info = self._commit(row, fired, now, st["area"], new_state, short_mem, recalled,
                            online, sleep_start=falls_asleep)
        if result:
            self.last_outcome = row["outcome"]
        if falls_asleep:
            self._try_sleep_cycle()
        return info

    # ---------- sleeping ----------
    def _rest_tick(self, fired: list[dict], nudge: float, onset: bool) -> dict:
        """One tick of sleep: no brain or narrator. Restores rest, advances the clock, wakes
        when rested (and it is morning), retries the sleep cycle if it is still pending."""
        cfg, st, s = self.cfg, self.state, self.cfg.settings
        sl = s["sleep"]
        now = st["sim_time"]
        online = self._online()
        before = dict(st["needs"])
        minutes = sl["sim_minutes_per_tick"]
        needs = character.decay(before, minutes * sl["decay_multiplier"], cfg)
        needs = character.restore(needs, sl["restore_per_sim_hour"] * minutes / 60, cfg)
        new_time = clock.advance(now, minutes)
        new_dt = clock.parse(new_time)
        wake = False
        if not onset:
            started = db.get_meta(self.conn, "sleep_started", now)
            slept_h = (new_dt - clock.parse(started)).total_seconds() / 3600
            rested = needs[character.need_with_flag(cfg, "sleep_restore")] >= sl["wake_rest_at_least"]
            morning = (new_dt.hour >= sl["wake_not_before_hour"]
                       and not clock.is_night(new_dt, s["phases"]))
            wake = (rested and morning) or slept_h >= sl["max_hours"]
        mood = character.compute_mood(needs, before, nudge, None, cfg)
        new_state = {**st, "sim_time": new_time, "needs": needs, "mood": mood,
                     "energy": character.energy(needs, cfg), "weather": self.world.weather,
                     "brain_online": online, "is_asleep": not wake}
        outcome = ("settles down to sleep." if onset else
                   "wakes up, rested." if wake else "sleeping.")
        row = {"kind": "asleep", "reaction": None, "thought": None, "action": None,
               "intent": None, "outcome": outcome, "gains": {}, "effort": None,
               "duration": minutes, "changes": [], "rejected": []}
        info = self._commit(row, fired, now, st["area"], new_state, None, [], online,
                            sleep_start=onset, wake=wake)
        self._try_sleep_cycle()
        return info

    def _try_sleep_cycle(self) -> None:
        """Run the nightly consolidation if one is pending and the brain is reachable. A failure
        writes nothing; it is retried on the next sleeping tick."""
        if db.get_meta(self.conn, "sleep_pending") != "1" or not self._online():
            return
        st = self.state
        try:
            plan = sleep.plan(self.cfg, self.backend, self.conn, self.memory,
                              st["tendencies"], st["sim_time"])
        except BackendError as e:
            log.warning("sleep cycle postponed (brain unavailable): %s", e)
            return
        new_state = {**st, "tendencies": plan.tendencies}
        real = _now_real()
        with db.transaction(self.conn):
            sleep.commit(self.conn, self.cfg, self.memory, plan, st["sim_time"], real,
                         sim_date=(db.get_meta(self.conn, "sleep_started") or st["sim_time"])[:10])
            db.save_state(self.conn, new_state, real)
        self.state = new_state
        self.memory._dirty = True
        log.info("sleep cycle done: %d memories, %d tendency changes, %d preferences",
                 len(plan.memories), len(plan.tendency_changes), len(plan.preferences))

    # ---------- recording ----------
    def _commit(self, row: dict, fired: list[dict], now: str, area: str, new_state: dict,
                short_mem: dict | None, recalled: list[dict], online: bool,
                sleep_start: bool = False, wake: bool = False) -> dict:
        """Record the tick: one transaction; a crash leaves the previous state intact."""
        for e in fired:  # event-spawned objects are world changes too (audit trail)
            if e["spawned"]:   # recorded from spawn time: the narrator may consume it this tick
                row["changes"] = [e["spawn_info"]] + row["changes"]
        needs, mood = new_state["needs"], new_state["mood"]
        real = _now_real()
        with db.transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO ticks(kind,sim_time,real_time,area,reaction,thought,action,intent,"
                "outcome,gains,effort,duration,changes,rejected_changes,needs_after,mood_after) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["kind"], now, real, area, row["reaction"], row["thought"],
                 row["action"], row["intent"], row["outcome"], dumps(row["gains"]), row["effort"],
                 row["duration"], dumps(row["changes"]), dumps(row["rejected"]), dumps(needs),
                 dumps(mood)))
            tick_id = cur.lastrowid
            for e in fired:
                self.conn.execute("INSERT INTO events(tick_id,sim_time,area,text) VALUES(?,?,?,?)",
                                  (tick_id, now, e["area"] or area, e["text"]))
            self.world.sync(self.conn)
            if short_mem:
                self.memory.save(short_mem, "short")
            if recalled:
                self.memory.mark_recalled([m["id"] for m in recalled], now)
            if sleep_start:
                db.set_meta(self.conn, "sleep_started", now)
                db.set_meta(self.conn, "sleep_tick_id", tick_id)
                db.set_meta(self.conn, "sleep_pending", 1)
            if wake:
                db.set_meta(self.conn, "sleep_started", "")
            db.save_state(self.conn, new_state, real)
            if online:
                db.set_meta(self.conn, "brain_last_seen", real)
            db.set_meta(self.conn, "last_alive_epoch", f"{self.time():.0f}")   # for downtime detection
            db.set_meta(self.conn, "clean_stop", 0)
        self.state = new_state
        self.last_kind = row["kind"]
        return {"id": tick_id, "kind": row["kind"], "sim_time": now, "area": area,
                "action": row["action"], "outcome": row["outcome"], "mood": mood["word"],
                "energy": new_state["energy"], "fired": [e["text"] for e in fired],
                "brain_online": online}

    # ---------- downtime ----------
    def mark_clean_stop(self) -> None:
        """Remember that we stopped on purpose, and when (so the next start measures the gap
        exactly). Never raises."""
        try:
            with db.transaction(self.conn):
                db.set_meta(self.conn, "last_alive_epoch", f"{self.time():.0f}")
                db.set_meta(self.conn, "clean_stop", 1)
        except Exception:
            log.exception("could not record clean stop")

    def startup_downtime(self) -> dict | None:
        """Apply `downtime_policy` for the time the engine was off. Call once, before the loop.
        Returns the diary tick info if a catch-up entry was written."""
        last = db.get_meta(self.conn, "last_alive_epoch")
        if last is None:                       # first ever start
            return None
        s = self.cfg.settings
        clean = db.get_meta(self.conn, "clean_stop") == "1"
        now = self.time()
        gap = now - float(last)
        if s["downtime_policy"] == "pause":
            if gap > 2 * s["tick_interval_seconds"]:
                log.info("was down for %.0f min; policy 'pause': sim time resumes where it stopped",
                         gap / 60)
            return None
        cu = downtime.plan(self.cfg, now, float(last), clean)
        if cu is None:
            return None
        log.info("was down for %.0f min; policy 'catch_up': applying %d sim minutes%s",
                 cu.gap_seconds / 60, cu.sim_minutes, " (capped)" if cu.capped else "")
        return self._apply_catch_up(cu)

    def _apply_catch_up(self, cu: "downtime.CatchUp") -> dict:
        cfg, st, s = self.cfg, self.state, self.cfg.settings
        now, minutes = st["sim_time"], cu.sim_minutes
        before = dict(st["needs"])
        if st["is_asleep"]:        # it was asleep when we went down: treat the gap as sleep
            sl = s["sleep"]
            needs = character.decay(before, minutes * sl["decay_multiplier"], cfg)
            needs = character.restore(needs, sl["restore_per_sim_hour"] * minutes / 60, cfg)
        else:
            needs = character.decay(before, minutes * s["catch_up_decay_multiplier"], cfg)
        mood = character.compute_mood(needs, before, 0.0, None, cfg)
        new_state = {**st, "sim_time": clock.advance(now, minutes), "needs": needs, "mood": mood,
                     "energy": character.energy(needs, cfg)}
        row = {"kind": "catchup", "reaction": None, "thought": None, "action": None, "intent": None,
               "outcome": downtime.description(now, minutes, s["phases"]), "gains": {},
               "effort": None, "duration": minutes, "changes": [], "rejected": []}
        return self._commit(row, [], now, st["area"], new_state, None, [], online=False)

    # ---------- the loop ----------
    def run(self, ticks: int | None = None, interval: float | None = None,
            on_tick: Callable[[dict], None] | None = None,
            sleep: Callable[[float], None] = time.sleep) -> int:
        """Run until `ticks` is reached or stop() is called. Never raises on a bad tick:
        errors are logged and the loop carries on."""
        interval = self.cfg.settings["tick_interval_seconds"] if interval is None else interval
        done = 0
        try:
            self.startup_downtime()
        except Exception:
            log.exception("downtime handling failed; continuing without it")
        write_heartbeat(self.cfg.heartbeat_path)
        while not self.stop_requested and (ticks is None or done < ticks):
            failed = False
            write_heartbeat(self.cfg.heartbeat_path)
            try:
                info = self.tick()
                if on_tick:
                    on_tick(info)
            except Exception:
                failed = True
                log.exception("tick failed; state reloaded, continuing")
            done += 1
            if interval and not self.stop_requested and (ticks is None or done < ticks):
                sleep(max(interval, self.cfg.settings["error_backoff_seconds"]) if failed else interval)
        self.mark_clean_stop()
        return done

    def install_signal_handlers(self) -> None:
        def _stop(signum, frame):
            log.info("signal %s: stopping after this tick", signum)
            self.stop_requested = True
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

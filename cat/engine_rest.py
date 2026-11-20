"""Between decisions: activities carried on or broken off, sleep and the night's work, downtime. Part of `cat.engine.Engine` (a mixin: the methods use the engine's state)."""
from __future__ import annotations

import json
import logging
import random
import signal
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from typing import Callable

from cat import brain, character, clock, db, downtime, effects, impulses, journal, logs, sleep
from cat import harms
from cat import moves as MV
from cat import drawings as DW
from cat import director as DR
from cat import habits as HB
from cat import projects as PJ
from cat import prompts as P
from cat.config import Config, load_config
from cat.devcontrol import DevControl
from cat.devqueue import CommandError
from cat.db import dumps
from cat.healthcheck import write_heartbeat
from cat.llm import BackendError, LLMBackend
from cat.memory import Memory, build_query, importance_for, notable_reasons
from cat.trace import Tracer, prune
from cat.world import WorldState

log = logging.getLogger("cat.engine")
_now_real = clock.now_real


class RestMixin:
    # ---------- sleeping ----------
    def _interrupt_reason(self, act: dict, fired: list[dict], st: dict) -> str | None:
        """What breaks into a move that is still running: an event, or a need newly in the empty band."""
        a = self.cfg.settings["activity"]
        if a["interrupt_on_events"] and fired:
            return "event"
        if a["interrupt_on_empty_need"]:
            empty = {k for k, v in st["needs"].items() if character.need_band(v, self.cfg) == "empty"}
            if empty - set(act.get("empty_at_start", [])):
                return "need"
        if a.get("interrupt_while_empty") and any(character.need_band(v, self.cfg) == "empty"
                                                  for v in st["needs"].values()):
            return "need"                               # an empty need ends whatever it was doing
        if a.get("interrupt_on_low_need"):              # a need newly low comes before what it was doing
            low = {k for k, v in st["needs"].items() if character.need_band(v, self.cfg) in ("low", "empty")}
            if low - set(act.get("low_at_start", act.get("empty_at_start", []))):
                return "need"
        return None

    def _end_activity(self, act: dict, reason: str) -> None:
        """It was interrupted: drop the rest of the move and tell the brain, in words, what was broken off."""
        self.state["activity"] = None
        self.last_outcome = P.render(self.cfg, f"brain.situation.interrupted_{reason}", label=act["label"])
        if act.get("drawing_object"):        # the sitting at the drawing lasted only this long
            self._drawing_cut = (act["drawing_object"], act["total"] - act["remaining"])
        self.tracer.set_activity({"state": "interrupted", "reason": reason, "label": act["label"],
                                  "remaining": act["remaining"]})

    def _carry_tick(self, fired: list[dict], nudge: float, act: dict) -> dict:
        """A tick inside a longer move: no recall, brain, journal or director call. Time passes and needs
        decay (plus any drains); the move's gains and effort were already paid when it began."""
        cfg, st, s = self.cfg, self.state, self.cfg.settings
        now = st["sim_time"]
        before = dict(st["needs"])
        minutes = min(s["activity"]["step_minutes"], act["remaining"])
        anx_prev = character.anxiety_of(st["mood"], cfg)
        needs = character.decay(before, minutes, cfg, anxiety=anx_prev)
        harsh = self._harsh(minutes)
        drains = harms.merge(character.event_drains(fired), harsh["drains"])
        needs = character.apply_drains(needs, drains)
        dis, _ = self._dislike_drains(minutes, act["label"])
        needs = character.apply_drains(needs, dis)
        self.tracer.set_need_math(character.explain_needs(before, minutes, None, cfg, needs, anxiety=anx_prev,
                                                          drains=drains, dislike_drains=dis))
        energy = character.clamp_energy(st["energy"] - harsh["energy"])      # soaked in the rain wears it down
        mood_inputs: dict = {"exposure": harsh} if harsh["texts"] else {}
        stir = self._anxiety_extra()                    # a place it hardly knows, disliked things nearby
        if stir["total"]:
            mood_inputs["stirred"] = stir
        anx = character.anxiety_next(anx_prev, needs, energy, minutes, cfg, extra=stir["total"])
        mood = character.compute_mood(needs, nudge, None, energy, cfg, trace=mood_inputs, anxiety=anx)
        self.tracer.set_mood(mood_inputs, mood)
        remaining = act["remaining"] - minutes
        new_act = None if remaining <= 0 else {**act, "remaining": remaining}
        new_state = {**st, "sim_time": clock.advance(now, minutes), "needs": needs, "mood": mood, "energy": energy,
                     "weather": self.world.weather, "activity": new_act}
        self.tracer.set_activity({"state": "finished" if new_act is None else "carried", "label": act["label"],
                                  "minutes": minutes, "remaining": max(0, remaining)})
        row = {"kind": "carry", "reaction": None, "thought": None, "action": act["label"], "intent": act["why"],
               "move_key": act["key"], "outcome": "carrying on" if new_act else "finished",
               "gains": {}, "effort": None, "duration": minutes, "changes": [], "rejected": []}
        return self._commit(row, fired, now, st["area"], new_state, None, [], online=False)

    def _rest_tick(self, fired: list[dict], nudge: float, onset: bool) -> dict:
        """One tick of sleep: no brain or journal. Restores rest, advances the clock, wakes
        when rested (and it is morning), retries the sleep cycle if it is still pending."""
        cfg, st, s = self.cfg, self.state, self.cfg.settings
        sl = s["sleep"]
        now = st["sim_time"]
        online = self._online()
        before = dict(st["needs"])
        minutes = sl["sim_minutes_per_tick"]
        anx_prev = character.anxiety_of(st["mood"], cfg)
        needs = character.decay(before, minutes * sl["decay_multiplier"], cfg, anxiety=anx_prev)
        harsh = self._harsh(minutes)                       # asleep in the rain in the open: soaked all the same
        drains = harms.merge(character.event_drains(fired), harsh["drains"])
        needs = character.apply_drains(needs, drains)
        calm = 1.0 - cfg.settings["anxiety"]["sleep_restore_penalty"] * anx_prev / 100 if cfg.settings["anxiety"]["enabled"] else 1.0
        nest = any("nest" in o["tags"] for o in self.world.here(st["area"]))     # its own place: it rests deeper
        deep = float(sl.get("nest_restore_multiplier", 1.0)) if nest else 1.0
        energy = character.restore_energy(st["energy"], sl["restore_per_sim_hour"] * minutes / 60 * calm * deep)   # an anxious sleep rests less
        energy = character.clamp_energy(energy - harsh["energy"])
        self.tracer.set_need_math(character.explain_needs(
            before, minutes, None, cfg, needs, decay_multiplier=sl["decay_multiplier"], anxiety=anx_prev, drains=drains))
        new_time = clock.advance(now, minutes)
        new_dt = clock.parse(new_time)
        wake = False
        if not onset:
            started = db.get_meta(self.conn, "sleep_started", now)
            slept_h = (new_dt - clock.parse(started)).total_seconds() / 3600
            rested = energy >= sl["wake_energy_at_least"]
            morning = (new_dt.hour >= sl["wake_not_before_hour"]
                       and not clock.is_night(new_dt, s["phases"]))
            wake = (rested and morning) or slept_h >= sl["max_hours"]
        mood_inputs: dict = {"sleep_restore": {"per_hour": sl["restore_per_sim_hour"], "anxiety_factor": round(calm, 3),
                                               "nest": nest, "nest_factor": deep},
                       **({"exposure": harsh} if harsh["texts"] else {})}
        anx = character.anxiety_next(anx_prev, needs, energy, minutes, cfg, asleep=True)
        mood = character.compute_mood(needs, nudge, None, energy, cfg, trace=mood_inputs, anxiety=anx)
        self.tracer.set_mood(mood_inputs, mood)
        new_state = {**st, "sim_time": new_time, "needs": needs, "mood": mood,
                     "energy": energy, "weather": self.world.weather,
                     "brain_online": online, "is_asleep": not wake, "activity": None}
        outcome = ("settles down to sleep." if onset else
                   "wakes up, rested." if wake else "sleeping.")
        row = {"kind": "asleep", "reaction": None, "thought": None, "action": None,
               "intent": None, "outcome": outcome, "gains": {}, "effort": None,
               "duration": minutes, "changes": [], "rejected": []}
        info = self._commit(row, fired, now, st["area"], new_state, None, [], online,
                            sleep_start=onset, wake=wake)
        self._try_sleep_cycle()
        return info

    def _cycle_due(self, now: str) -> bool:
        """The nightly cycle (consolidation, tendencies, preferences, self-image, projects, habits) runs when it falls
        asleep at night, or when `sleep.cycle_min_gap_hours` have passed without one. A daytime nap only rests."""
        if clock.phase(clock.parse(now), self.cfg.settings["phases"]) in ("dusk", "night"):
            return True
        last = db.get_meta(self.conn, "last_cycle_sim_time")
        gap = float(self.cfg.settings["sleep"].get("cycle_min_gap_hours", 20))
        return not last or (clock.parse(now) - clock.parse(last)).total_seconds() / 3600 >= gap

    def _try_sleep_cycle(self) -> None:
        """Run the nightly consolidation if one is pending and the brain is reachable. A failure
        writes nothing; it is retried on the next sleeping tick."""
        if db.get_meta(self.conn, "sleep_pending") != "1" or not self._online():
            return
        with self.tracer.stage("sleep"):
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
                db.set_meta(self.conn, "last_cycle_sim_time", st["sim_time"])
                sleep_log_id = sleep.commit(
                    self.conn, self.cfg, self.memory, plan, st["sim_time"], real,
                    sim_date=(db.get_meta(self.conn, "sleep_started") or st["sim_time"])[:10])
                db.save_state(self.conn, new_state, real)
            self.state = new_state
            self.memory._dirty = True
            self.tracer.set_sleep(plan.trace_steps, sleep_log_id)
            log.info("sleep cycle done: %d memories, %d tendency changes, %d preferences",
                     len(plan.memories), len(plan.tendency_changes), len(plan.preferences),
                     extra={"event": "sleep_cycle", "extra": {
                         "sleep_log_id": sleep_log_id, "memories": len(plan.memories),
                         "tendency_changes": plan.tendency_changes}})
            prune(self.conn, self.cfg)           # trace retention runs during sleep

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
            energy = character.restore_energy(st["energy"], sl["restore_per_sim_hour"] * minutes / 60)
        else:
            needs = character.decay(before, minutes * s["catch_up_decay_multiplier"], cfg)
            energy = st["energy"]
        anx_gap = character.anxiety_of(st["mood"], cfg)
        self.tracer.begin()
        self.tracer.set_need_math(character.explain_needs(
            before, minutes, None, cfg, needs,
            decay_multiplier=(s["sleep"]["decay_multiplier"] if st["is_asleep"] else s["catch_up_decay_multiplier"])))
        mood_inputs: dict = {}
        anx = character.anxiety_next(anx_gap, needs, energy, minutes, cfg, asleep=bool(st["is_asleep"]))
        mood = character.compute_mood(needs, 0.0, None, energy, cfg, trace=mood_inputs, anxiety=anx)
        self.tracer.set_mood(mood_inputs, mood)
        new_state = {**st, "sim_time": clock.advance(now, minutes), "needs": needs, "mood": mood,
                     "energy": energy, "activity": None}
        row = {"kind": "catchup", "reaction": None, "thought": None, "action": None, "intent": None,
               "outcome": downtime.description(now, minutes, s["phases"]), "gains": {},
               "effort": None, "duration": minutes, "changes": [], "rejected": []}
        info = self._commit(row, [], now, st["area"], new_state, None, [], online=False)
        self.tracer.flush(self.conn, info["id"], "ok")
        return info

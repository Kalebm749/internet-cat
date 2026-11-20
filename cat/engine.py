"""The tick loop. One tick = world events -> brain -> apply -> journal -> record (one DB
transaction for the last two; LLM calls happen outside it)."""
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

from cat.engine_dev import DevMixin            # the engine's parts (mixins): dashboard commands and the director,
from cat.engine_mind import MindMixin          # menu, prompts, recall and journal,
from cat.engine_rest import RestMixin          # activities, sleep and downtime,
from cat.engine_upkeep import UpkeepMixin      # backups, migrations and the world sync

log = logging.getLogger("cat.engine")
_now_real = clock.now_real


class Engine(UpkeepMixin, MindMixin, RestMixin, DevMixin):
    def __init__(self, cfg: Config, backend: LLMBackend, conn=None, rng: random.Random | None = None):
        self.cfg = cfg
        self.backend = backend
        self.rng = rng or random.Random()
        self.conn = conn or db.connect(cfg.db_path)
        self._protect()                 # a life is never lost to an update: back up before anything changes
        db.init_db(self.conn)
        self.time = time.time          # injectable for tests
        self.stop_requested = False
        self._online_cache: bool | None = None
        self.move_rng = random.Random(self.rng.getrandbits(32))        # durations of moves: its own stream
        # dev-dashboard state (see cat/devcontrol.py)
        self.paused = False
        self.step_pending = False
        self.step_cmd_id: int | None = None
        self.interval_override: float | None = None
        self.interval_override_ticks: int | None = None
        self.injected: list[dict] = []
        self.last_tick_id: int | None = None
        self.last_tick_ms: float | None = None
        self.started_at = time.time()
        self.tracer = Tracer(cfg)
        backend.recorder = self.tracer.record_llm        # every LLM attempt is traced
        self.memory = Memory(self.conn, cfg, backend, self.tracer)
        self._bootstrap()
        self._reconcile_config()
        db.set_meta(self.conn, "config_fingerprint", self._fingerprint())
        self._load()
        self._backfill_knowledge()                       # a life from before hidden properties keeps what it learned
        self.dev = DevControl(self)
        self.dev.publish_config()

    def _load(self) -> None:
        self.state = db.get_state(self.conn)
        self.state["is_asleep"] = bool(self.state["is_asleep"])
        self.world = WorldState.load(self.conn, self.cfg, self.state["weather"])
        self.first_dt = clock.parse(db.get_meta(self.conn, "first_sim_time"))
        # what the brain is told happened last: code's plain account (`result`; older databases kept prose in
        # `outcome` and have no `result`, so their last line falls back to it once)
        row = self.conn.execute(
            "SELECT COALESCE(result, outcome) AS o FROM ticks WHERE kind='tick' ORDER BY id DESC LIMIT 1").fetchone()
        self.last_outcome = row["o"] if row else None
        # what the previous non-sleep tick was: 'doze' => the first prompt notes the doze
        k = self.conn.execute(
            "SELECT kind FROM ticks ORDER BY id DESC LIMIT 1").fetchone()
        self.last_kind = k["kind"] if k else None

    # ---------- the tick ----------
    def _online(self) -> bool:
        """Brain health check (GET /api/tags), cached for the duration of one tick."""
        if self._online_cache is None:
            self._online_cache = self.backend.is_online()
        return self._online_cache

    def tick(self) -> dict:
        self._online_cache = None
        self.tracer.begin()
        t0 = time.perf_counter()
        logs.set_tick_id(self.conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM ticks").fetchone()[0])
        try:
            info = self._tick()
        except BaseException as e:
            # In-memory state may be ahead of the DB after a failed transaction: reload it.
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            self._load()
            self.tracer.flush(self.conn, None, "error", f"{type(e).__name__}: {e}")
            raise
        finally:
            ms = (time.perf_counter() - t0) * 1000
            logs.set_tick_id(None)            # log lines between ticks must not carry this tick's id
        self.last_tick_id, self.last_tick_ms = info["id"], round(ms, 1)
        self.tracer.flush(self.conn, info["id"], "doze" if info["kind"] == "doze" else "ok")
        if self.cfg.settings["log_tick_events"]:
            logging.getLogger("cat.tick").info(
                "tick %s (%s) in %.0f ms: %s", info["id"], info["kind"], ms, info["outcome"],
                extra={"event": "tick", "tick_id": info["id"],
                       "extra": {"kind": info["kind"], "area": info["area"], "ms": round(ms),
                                 "action": info["action"], "mood": info["mood"],
                                 "energy": info["energy"], "brain_online": info["brain_online"]}})
        return info

    def _tick(self) -> dict:
        cfg, st, s = self.cfg, self.state, self.cfg.settings
        now = st["sim_time"]
        dt = clock.parse(now)

        # 0. things the world brought that have had their time are gone, and sources put out what they make
        #    (both recorded with this tick's changes)
        self._expired = self.world.expire(now) + self._produce(now)
        # 1. world events (perceived at the start of the tick)
        rolls: list[dict] = []
        with self.tracer.stage("world"):
            fired = self.world.roll_events(self.rng, dt, st["area"], now, trace=rolls)
        fired += self._take_injected(now, rolls)

        # in the middle of a longer move: carry on without a brain call, unless something breaks in
        act = st.get("activity") if s["activity"]["enabled"] else None
        rest_now = st["is_asleep"] or (clock.is_night(dt, s["phases"])
                                       and st["energy"] < s["sleep"]["fall_asleep_energy_below"])
        if act and rest_now:                              # sleep ends whatever it was doing
            if act.get("drawing_object"):                 # a sitting at a drawing cut short by sleep
                self._drawing_cut = (act["drawing_object"], act["total"] - act["remaining"])
            self.state["activity"] = st["activity"] = act = None
        reason = self._interrupt_reason(act, fired, st) if act else None
        if act and not reason:
            self.tracer.set_event_rolls(rolls)
            nudge = st["mood"].get("event_nudge", 0.0) * s["mood"]["event_decay"] \
                + sum(e["mood"] for e in fired)
            return self._carry_tick(fired, nudge, act)
        if act and reason:
            self._end_activity(act, reason)

        fired += self._director_happening(fired, st, now, dt)
        self.tracer.set_event_rolls(rolls)
        nudge = st["mood"].get("event_nudge", 0.0) * s["mood"]["event_decay"] \
            + sum(e["mood"] for e in fired)

        # sleeping, or too exhausted at night to carry on: no brain call this tick
        if st["is_asleep"]:
            return self._rest_tick(fired, nudge, onset=False)
        if clock.is_night(dt, s["phases"]) and st["energy"] < s["sleep"]["fall_asleep_energy_below"]:
            return self._rest_tick(fired, nudge, onset=True)

        # 2+3. the decision (LLM call, outside any transaction), then code decides what the move does
        result = None
        impulse = None
        anx_prev = character.anxiety_of(st["mood"], cfg)
        recalled: list[dict] = []
        online = self._online()
        if online:
            try:
                # an impulse is the fast path: rolled BEFORE the brain, so when it fires there is no brain call
                # and nothing it would have thought or felt is recorded against a move it did not choose
                menu_now = self._menu()
                impulse = (impulses.rest_drive(cfg, self.world, st["energy"], menu_now, st["area"], f"{now}|{st['area']}")
                           or impulses.drive(cfg, self.conn, self.world, st["needs"], menu_now, st["area"],
                                             f"{now}|{st['area']}")
                           or impulses.take_over(cfg, anx_prev, menu_now, f"{now}|{st['area']}"))
                if impulse:                  # anxiety took the decision over: the world still only offers this menu
                    b = {"move": impulse["move"], "entry": impulse["entry"], "reaction": "", "thought": "",
                         "why": f"an impulse ({impulse['impulse']})", "feeling": None}
                else:
                    with self.tracer.stage("recall"):
                        recalled = self._recall(fired)
                    with self.tracer.stage("brain"):
                        ctx = self._brain_context(fired, recalled)
                        self.tracer.set_pressing({
                            **brain.select_needs(cfg, ctx.needs), "stale_here": ctx.stale_here,
                            "menu_shaping": getattr(self, "_menu_shaping", None),
                            "ticks_in_area": self._ticks_in_area(), "stale_after_ticks": s["stale_after_ticks"],
                            "stale_text": cfg.character.get("stale_here") if ctx.stale_here else None})
                        b = brain.think(self.backend, cfg, ctx)
                mv = b["move"]
                res = self._resolve_move(mv, b["why"], now)
                # the world can hurt: a move can go wrong (stale food, the bell when on edge, a slip, cold water)
                target = dict(self.world.objects[mv.target_id]) if mv.target_id in self.world.objects else None
                hurt = harms.move_hurt(cfg, mv.verb, target, anx_prev, st["energy"], now, f"{now}|{mv.key}")
                if hurt:
                    res["template"] = f"{res['template']} {hurt['outcome']}"
                # what the diary shows as having happened is code's plain account: no prose layer can add to it
                n = {"outcome": res["template"], "gains": res["gains"], "effort": res["effort"],
                     "duration": res["duration"], "changes": res["changes"], "project": res["project"],
                     "does": res["does"], "template": res["template"], "drawing": res.get("drawing"), "hurt": hurt,
                     "target": target}
                n["learned"] = self._learned(mv, n, now)          # what it found out about the thing
                n["expected"] = self._expected(mv.key)            # what its record led it to expect
                result = (b, n)
            except BackendError as e:
                log.warning("brain unavailable (%s): dozing", e)
                online = False
                self._online_cache = False

        # 4. apply
        t_apply = time.perf_counter()
        before = dict(st["needs"])
        new_activity = None
        if result:
            b, n = result
            total = n["duration"]
            step = s["activity"]["step_minutes"]
            sliced = s["activity"]["enabled"] and n["does"] != "sleep" and total > step
            minutes = step if sliced else total                  # a longer move continues in carry-on ticks
            fx = effects.apply_changes(n["changes"], self.world, st["area"], cfg, now)
            mult = self._repetition_multiplier(b["move"].key, st["area"])
            # project work is exempt from the repetition penalty: its progress (tapering) replaces it
            adv = PJ.plan_advance(cfg, PJ.active(self.conn), n.get("project"))
            if adv and adv.get("ok") and cfg.settings["projects"]["artifacts_enabled"] \
                    and (adv["milestones"] or adv["completed"]) and not adv["artifact"]:
                proj = PJ.get(self.conn, adv["id"])
                if proj and proj.get("medium") != "drawing":      # a drawing project's pieces are its real drawings
                    with self.tracer.stage("create"):
                        adv["artifact"] = PJ.create_artifact(self.backend, cfg, proj, adv)
            exempt = bool(adv and adv.get("ok") and adv["advanced"])
            if exempt:
                mult = 1.0
            needs = character.decay(before, minutes, cfg, anxiety=anx_prev)
            decayed = dict(needs)                                 # for how this went (experiences)
            hurt, harsh = n.get("hurt"), self._harsh(minutes)
            drains = harms.merge(character.event_drains(fired), (hurt or {}).get("drains"), harsh["drains"])
            needs = character.apply_drains(needs, drains)
            dis, _ = self._dislike_drains(minutes, b["move"].label)
            needs = character.apply_drains(needs, dis)
            scale = character.duration_scale(cfg, n["duration"])
            gf = float((hurt or {}).get("gain_factor", 1.0))
            needs = character.apply_gains(needs, n["gains"], cfg,
                                          consumed=fx.consumed, multiplier=mult, scale=scale, factor=gf)
            if adv and adv.get("ok") and adv["completed"]:        # finishing a project feels good (code pays it)
                needs = character.add_points(needs, cfg.settings["projects"]["completion_gains"])
            energy = character.spend_energy(st["energy"], n["effort"], cfg, scale=scale)
            energy = character.clamp_energy(energy - (hurt or {}).get("energy", 0.0) - harsh["energy"])
            self._harm_trace = {"hurt": hurt, "exposure": harsh}
            if adv is not None:
                self.tracer.set_project({**adv, "repetition_exempt": exempt})
            self.tracer.set_effects(fx.log)
            self.tracer.set_need_math(character.explain_needs(
                before, minutes, n["gains"], cfg, needs, consumed=fx.consumed, multiplier=mult, scale=scale,
                anxiety=anx_prev, drains=drains, dislike_drains=dis, factor=gf))
            feeling = b["feeling"]
            new_area = fx.area
            row = {"kind": "tick", "entry": None, "reaction": b["reaction"], "thought": b["thought"],
                   "action": b["move"].label, "intent": b["why"], "move_key": b["move"].key, "outcome": n["outcome"],
                   "result": n["template"],
                   "gains": n["gains"], "effort": n["effort"], "duration": minutes,
                   "changes": fx.applied, "rejected": fx.rejected,
                   "activity_minutes": total if sliced else None}
            if sliced:
                new_activity = {"label": b["move"].label, "key": b["move"].key, "why": b["why"], "total": total,
                                "remaining": total - step, "started_at": now,
                                "empty_at_start": [k for k, v in needs.items() if character.need_band(v, cfg) == "empty"],
                                "low_at_start": [k for k, v in needs.items() if character.need_band(v, cfg) in ("low", "empty")]}
                if n.get("drawing") and n["does"] in ("draw_new", "draw_more"):     # so an interruption can cut it
                    made = next((c.get("object_id") for c in fx.applied if c.get("op") in ("create", "set_state")), None)
                    new_activity["drawing_object"] = made
                self.tracer.set_activity({"state": "started", "label": new_activity["label"], "total": total,
                                          "step": step, "remaining": new_activity["remaining"]})
        else:
            adv = None
            minutes = s["doze"]["sim_minutes"]
            needs = character.decay(before, minutes, cfg, anxiety=anx_prev)
            drains = character.event_drains(fired)
            needs = character.apply_drains(needs, drains)
            dis, _ = self._dislike_drains(minutes, None)
            needs = character.apply_drains(needs, dis)
            energy = character.restore_energy(st["energy"], s["doze"]["energy_restore"])
            self.tracer.set_need_math(character.explain_needs(before, minutes, None, cfg, needs, anxiety=anx_prev,
                                                              drains=drains, dislike_drains=dis))
            feeling = None
            new_area = st["area"]
            row = {"kind": "doze", "reaction": None, "thought": None, "action": None,
                   "intent": None, "outcome": "dozing", "gains": {}, "effort": None,
                   "duration": minutes, "changes": [], "rejected": []}

        mood_inputs: dict = {k: v for k, v in (getattr(self, "_harm_trace", None) or {}).items() if v
                              and (k != "exposure" or v.get("texts"))}
        self._harm_trace = None
        if impulse:
            mood_inputs["impulse"] = {k: v for k, v in impulse.items() if k != "move"} | {"move": impulse["move"].key}
        stir = self._anxiety_extra()                      # a place it hardly knows, disliked things nearby
        if stir["total"]:
            mood_inputs["stirred"] = stir
        anx = character.anxiety_next(anx_prev, needs, energy, minutes, cfg, extra=stir["total"])
        if result:                                        # it went worse or better than its record led it to expect
            sur = self._surprise(result[1].get("expected"), needs, decayed)
            if sur:
                mood_inputs["surprise"] = sur
                anx = round(min(100.0, anx + sur["anxiety"]), 2) if cfg.settings["anxiety"]["enabled"] else anx
                nudge += sur["mood"]
        mood_inputs["anxiety_before"] = round(anx_prev, 2)
        mood = character.compute_mood(needs, nudge, feeling, energy, cfg, trace=mood_inputs, anxiety=anx)
        if result:                                        # how this went, for what it comes to like and dislike
            row["experiences"] = self._experiences(result[1], row, decayed, needs, anx - anx_prev)
            mood_inputs["experience"] = row["experiences"][0]["score"] if row["experiences"] else None
            row["learned"] = result[1].get("learned") or []
            if row["learned"]:
                mood_inputs["learned"] = row["learned"]
        self.tracer.set_mood(mood_inputs, mood)
        new_state = {**st, "area": new_area, "sim_time": clock.advance(now, minutes),
                     "needs": needs, "mood": mood, "energy": energy,
                     "weather": self.world.weather, "brain_online": online, "activity": new_activity}
        self.tracer.add_stage("apply", t_apply)

        # its own journal entry, written now that code has decided what happened (LLM call, outside the txn)
        if result:
            dplan = result[1].get("drawing")
            row["entry"], title = self._journal(result[0], row, fired, before, needs, mood, energy, recalled, impulse,
                                                finishing=dplan["name"] if dplan and dplan["does"] == "finish_drawing" else None)
            if dplan:          # recorded with the tick: the sitting carries how it was when it sat down to draw
                sitting = {"minutes": result[1]["duration"], "planned": result[1]["duration"],
                           "mood": st["mood"].get("value", 0.0), "anxiety": character.anxiety_of(st["mood"], cfg),
                           "energy": st["energy"]}
                if dplan["does"] in ("draw_new", "draw_more"):
                    sitting.update(self._sketch(dplan, result[1]["duration"]) or {})
                row["drawing"] = {"plan": dplan, "title": title, "sitting": sitting}

        # the creature chose to sleep (honoured when tired enough, or at night)
        falls_asleep = bool(result) and result[1]["does"] == "sleep"      # offered only when allowed
        new_state["is_asleep"] = falls_asleep

        # notable moment -> short-term memory (embedding is an HTTP call: do it before the txn)
        short_mem = None
        if result:
            why = notable_reasons(cfg, before, needs, feeling,
                                  row["changes"], row["rejected"])
            if adv and adv.get("ok"):
                if adv["completed"]:
                    why.append(f"finished a project: {adv['title']}")
                elif adv["milestones"]:
                    why.append(f"a milestone in a project: {adv['title']}")
                if adv["artifact"]:
                    why.append("made something")
            if impulse and cfg.settings["notable"].get("impulse", True):
                why.append(f"acted on an impulse ({impulse['impulse']})")
            up = character.anxiety_rose(anx_prev, anx, cfg)
            if up and cfg.settings["notable"].get("anxiety_rise", True):
                why.append(f"anxiety rose to {up}")
            if why:
                t = clock.time_label(dt)
                # what is remembered is what the world did (code's plain account) and what the creature itself
                # wrote about it afterwards (marked as its words, not as fact)
                text = (f"{t}, {cfg.world['areas'][st['area']]['name']}: {row['action']}. "
                        f"{result[1].get('template') or ''}").strip()
                if row.get("entry") or row["reaction"]:
                    text += f' You wrote: "{row.get("entry") or row["reaction"]}"'
                with self.tracer.stage("record"):
                    short_mem = self.memory.prepare(text, importance_for(cfg, len(why)), now)

        info = self._commit(row, fired, now, st["area"], new_state, short_mem, recalled,
                            online, sleep_start=falls_asleep, project_adv=adv)
        if result:
            self.last_outcome = row["result"]          # code's plain account of what happened
        if falls_asleep:
            self._try_sleep_cycle()
        return info

    # ---------- recording ----------
    def _commit(self, row: dict, fired: list[dict], now: str, area: str, new_state: dict,
                short_mem: dict | None, recalled: list[dict], online: bool,
                sleep_start: bool = False, wake: bool = False, project_adv: dict | None = None) -> dict:
        """Record the tick: one transaction; a crash leaves the previous state intact."""
        for e in fired:  # event-spawned objects are world changes too (audit trail)
            if e["spawned"]:   # recorded from spawn time: the move may consume it this tick
                row["changes"] = [e["spawn_info"]] + row["changes"]
        expired, self._expired = getattr(self, "_expired", []), []
        row["changes"] = expired + row["changes"]          # objects that expired at the start of this tick
        needs, mood = new_state["needs"], new_state["mood"]
        real = _now_real()
        t_rec = time.perf_counter()
        with db.transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO ticks(kind,sim_time,real_time,area,reaction,thought,action,intent,"
                "outcome,gains,effort,duration,changes,rejected_changes,needs_after,mood_after,"
                "energy_after,move_key,activity_minutes,entry,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["kind"], now, real, area, row["reaction"], row["thought"],
                 row["action"], row["intent"], row["outcome"], dumps(row["gains"]), row["effort"],
                 row["duration"], dumps(row["changes"]), dumps(row["rejected"]), dumps(needs),
                 dumps(mood), new_state["energy"], row.get("move_key"), row.get("activity_minutes"),
                 row.get("entry"), row.get("result")))
            tick_id = cur.lastrowid
            for e in fired:
                self.conn.execute("INSERT INTO events(tick_id,sim_time,area,text) VALUES(?,?,?,?)",
                                  (tick_id, now, e["area"] or area, e["text"]))
            self.world.sync(self.conn)
            if short_mem:
                self.memory.save(short_mem, "short", tick_id=tick_id)
            if recalled:
                self.memory.mark_recalled([m["id"] for m in recalled], now)
            if sleep_start:
                db.set_meta(self.conn, "sleep_started", now)
                db.set_meta(self.conn, "sleep_tick_id", tick_id)
                if self._cycle_due(now):        # a night's sleep (or a day gone by without one): the night's work
                    db.set_meta(self.conn, "sleep_pending", 1)
            for x in row.get("experiences") or []:
                self.conn.execute("INSERT INTO experiences(tick_id,sim_time,subject_kind,subject_key,subject_name,score,"
                                  "note,needs_score) VALUES(?,?,?,?,?,?,?,?)",
                                  (tick_id, now, x["kind"], x["key"], x["name"], x["score"], x["note"], x.get("needs_score")))
            for k in row.get("learned") or []:      # what it now knows about a thing (shown to it from now on)
                self.conn.execute("INSERT OR IGNORE INTO known_properties(name,tag,learned_sim_time,how,tick_id)"
                                  " VALUES(?,?,?,?,?)", (k["name"], k["tag"], now, k["how"], tick_id))
            if row.get("drawing"):          # the drawing's history, if its world change was applied
                dr = row["drawing"]
                DW.commit(self.conn, dr["plan"], row["changes"], tick_id, now, dr["sitting"], dr["title"])
            for sid, when in getattr(self, "_made", {}).items():     # when each source last made something
                db.set_meta(self.conn, f"produced:{sid}", when)
            self._made = {}
            cut, self._drawing_cut = getattr(self, "_drawing_cut", None), None
            if cut:                         # a sitting that was interrupted lasted only this long
                DW.cut_sitting(self.conn, cut[0], cut[1])
            if project_adv and project_adv.get("ok"):
                aid = PJ.commit_advance(self.conn, project_adv, tick_id, new_state["sim_time"])
                if aid:
                    project_adv["artifact_id"] = aid
            if row["kind"] == "tick":      # time spent in this place (a move resets it)
                stayed = new_state["area"] == area
                db.set_meta(self.conn, "director_gap", int(db.get_meta(self.conn, "director_gap", "0") or 0) + 1)
                working = bool(project_adv and project_adv.get("ok") and project_adv["advanced"])
                if not stayed:
                    db.set_meta(self.conn, "ticks_in_area", 0)
                elif not working:          # working on a project is not a place going stale
                    db.set_meta(self.conn, "ticks_in_area", self._ticks_in_area() + 1)
            if wake:
                db.set_meta(self.conn, "sleep_started", "")
            db.save_state(self.conn, new_state, real)
            if online:
                db.set_meta(self.conn, "brain_last_seen", real)
            db.set_meta(self.conn, "last_alive_epoch", f"{self.time():.0f}")   # for downtime detection
            db.set_meta(self.conn, "clean_stop", 0)
        self.tracer.add_stage("record", t_rec)
        self.state = new_state
        self.last_kind = row["kind"]
        return {"id": tick_id, "kind": row["kind"], "sim_time": now, "area": area,
                "action": row["action"], "outcome": row["outcome"], "mood": mood["word"],
                "energy": new_state["energy"], "fired": [e["text"] for e in fired],
                "brain_online": online}

    # ---------- the loop ----------
    def effective_interval(self, base: float | None = None) -> float:
        """Seconds between ticks: a dev `set_interval` override, else the argument, else settings."""
        if self.interval_override is not None:
            return float(self.interval_override)
        return float(self.cfg.settings["tick_interval_seconds"] if base is None else base)

    def _count_interval_override(self) -> None:
        if self.interval_override_ticks:
            self.interval_override_ticks -= 1
            if self.interval_override_ticks <= 0:
                self.interval_override = self.interval_override_ticks = None

    def _wait(self, interval: float | None, sleep: Callable[[float], None], floor: float = 0.0) -> None:
        """Wait between ticks. With the dev queue enabled the wait is sliced so pause / step /
        set_interval commands take effect within `dev_poll_seconds`."""
        poll = self.cfg.settings["dev_poll_seconds"]
        if not self.dev.enabled or poll <= 0:
            sleep(max(self.effective_interval(interval), floor))
            return
        elapsed = 0.0
        while not self.stop_requested:
            target = max(self.effective_interval(interval), floor)
            if elapsed >= target:
                break
            chunk = min(poll, target - elapsed)
            sleep(chunk)
            elapsed += chunk
            self.dev.process()
            if self.paused or self.step_pending:
                break

    def _refresh_alive(self) -> None:
        """Say 'I am alive' in the DB (used while paused, so a pause is never counted as downtime)."""
        try:
            with db.transaction(self.conn):
                db.set_meta(self.conn, "last_alive_epoch", f"{self.time():.0f}")
                db.set_meta(self.conn, "clean_stop", 0)
        except Exception:
            log.warning("could not refresh last-alive", exc_info=True)

    def run(self, ticks: int | None = None, interval: float | None = None,
            on_tick: Callable[[dict], None] | None = None,
            sleep: Callable[[float], None] = time.sleep) -> int:
        """Run until `ticks` is reached or stop() is called. Never raises on a bad tick: errors
        are logged and the loop carries on. While paused (dev dashboard) the loop idles, polling
        for commands; paused iterations do not count as ticks."""
        done = 0
        try:
            self.startup_downtime()
        except Exception:
            log.exception("downtime handling failed; continuing without it")
        write_heartbeat(self.cfg.heartbeat_path)
        self.dev.publish(force=True)
        last_alive = self.time()
        while not self.stop_requested and (ticks is None or done < ticks):
            write_heartbeat(self.cfg.heartbeat_path)
            self.dev.process()
            if self.paused and not self.step_pending:
                if self.time() - last_alive >= self.cfg.settings["paused_alive_seconds"]:
                    self._refresh_alive()
                    last_alive = self.time()
                self.dev.publish()
                sleep(self.cfg.settings["dev_poll_seconds"])
                continue
            step_cmd, self.step_pending, self.step_cmd_id = self.step_cmd_id, False, None
            failed, info = False, None
            try:
                info = self.tick()
                if on_tick:
                    on_tick(info)
            except Exception as ex:
                failed = True
                log.exception("tick failed; state reloaded, continuing")
                self.dev.finish_step(step_cmd, None, f"{type(ex).__name__}: {ex}")
            else:
                self.dev.finish_step(step_cmd, info)
            done += 1
            self.dev.publish(force=True)
            wait_for = interval
            if self.state.get("activity") and self.cfg.settings["activity"]["enabled"]:
                # a move is still running: the next tick needs no brain call, so it comes sooner
                base = self.cfg.settings["tick_interval_seconds"] if interval is None else interval
                wait_for = min(base, self.cfg.settings["activity"]["carry_interval_seconds"])
            if (self.effective_interval(wait_for) and not self.stop_requested and not self.paused
                    and not self.step_pending and (ticks is None or done < ticks)):
                self._wait(wait_for, sleep,
                           floor=self.cfg.settings["error_backoff_seconds"] if failed else 0.0)
            self._count_interval_override()          # a "for N ticks" override covers N waits
        self.mark_clean_stop()
        self.dev.publish(force=True, state="stopped")
        return done

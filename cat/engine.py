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


def _now_real() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Engine:
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
        self.dev = DevControl(self)
        self.dev.publish_config()

    # ---------- keeping a life across updates ----------
    def _fingerprint(self) -> str:
        """What the world and the data look like to this version: the schema version and every config file."""
        import hashlib
        c = self.cfg
        blob = json.dumps([db.SCHEMA_VERSION, c.world, c.character, c.moves, c.settings], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def _protect(self) -> None:
        """Before an update touches an existing life (a schema migration, or a changed config that the world sync
        and reconcile will apply), copy the database to data/backups/cat-<time>-pre-update.db. A brand-new world
        needs no backup. If the backup fails, the engine refuses to go on rather than risk the life."""
        try:
            lived = self.conn.execute("SELECT 1 FROM ticks LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            return                                            # a new database: nothing to protect yet
        if not lived or str(self.cfg.db_path) == ":memory:":
            return
        changed = db.schema_version(self.conn) < db.SCHEMA_VERSION \
            or db.get_meta(self.conn, "config_fingerprint") != self._fingerprint()
        if changed:
            dest = db.backup(self.conn, Path(self.cfg.db_path).parent / "backups", "pre-update",
                             keep=int(self.cfg.settings.get("backup_keep", 14)))
            log.warning("code or config changed since the last run: backed up the world to %s first", dest)

    def _produce(self, now: str) -> list[dict]:
        """World sources (world.yaml `produces`): the bread stall puts out a loaf each dawn, the tree drops fruit
        every few hours, whether or not the creature is there, never more than `max` waiting. Products are made by
        'source' and expire (settings object_lifetime_hours.source). When each source last made something is kept
        in meta (written with the tick). Returns change records."""
        out: list[dict] = []
        t = clock.parse(now)
        self._made = getattr(self, "_made", {})
        for spec in self.cfg.world["objects"]:
            pr = spec.get("produces")
            src = self.world.objects.get(spec["id"])
            if not pr or not src or src["area"] == "inventory":
                continue
            last = self._made.get(spec["id"]) or db.get_meta(self.conn, f"produced:{spec['id']}")
            if "at_hour" in pr:
                due = t.hour >= int(pr["at_hour"]) and (not last or last[:10] < now[:10])
            else:
                due = not last or (t - clock.parse(last)).total_seconds() / 3600 >= float(pr["every_hours"])
            waiting = sum(1 for o in self.world.here(src["area"]) if o["created_by"] == "source" and o["name"] == pr["name"])
            if not due or waiting >= int(pr.get("max", 1)) \
                    or len(self.world.here(src["area"])) >= self.cfg.settings["max_objects_per_area"]:
                continue
            o = self.world.add_object(pr["name"], list(pr["tags"]), src["area"], "source", now)
            self._made[spec["id"]] = now
            out.append({"op": "create", "source": "source", "object_id": o["id"], "area": o["area"],
                        "name": o["name"], "label": o["name"], "from": spec["id"]})
        return out

    def _sync_world(self, sim_time: str) -> list[str]:
        """Keep the world's permanent things in step with world.yaml (they are the world's, so the file decides):
        a changed object is updated (name, tags, its place unless carried; its state keeps what the world has done
        to it), a new one is added, one no longer in the file is taken out. Never touched: anything the creature
        made (its drawings) or is carrying, and food from sources (it expires). Objects the director made are taken
        out (it no longer makes objects). Returns notes of what changed."""
        cfg = self.cfg
        want = {o["id"]: o for o in cfg.world["objects"]}
        rows = {r["id"]: dict(r) for r in self.conn.execute("SELECT * FROM objects")}
        notes: list[str] = []
        with db.transaction(self.conn):
            for oid, r in rows.items():
                carried = r["area"] == "inventory"
                if r["created_by"] == "world" and oid not in want and not carried:
                    self.conn.execute("DELETE FROM objects WHERE id=?", (oid,))
                    notes.append(f"removed '{oid}'")
                elif r["created_by"] == "director" and not carried:
                    self.conn.execute("DELETE FROM objects WHERE id=?", (oid,))
                    notes.append(f"removed the director's '{oid}'")
                elif r["created_by"] == "world" and oid in want:
                    o = want[oid]
                    st = {**(o.get("state") or {}), **json.loads(r["state"] or "{}")}
                    area = r["area"] if carried else o["area"]
                    new = (o["name"], dumps(list(o["tags"])), dumps(st), area)
                    if new != (r["name"], r["tags"], r["state"], r["area"]):
                        self.conn.execute("UPDATE objects SET name=?, tags=?, state=?, area=? WHERE id=?", (*new, oid))
                        notes.append(f"updated '{oid}'")
            for oid, o in want.items():
                if oid not in rows:
                    self.conn.execute(
                        "INSERT INTO objects(id,name,area,tags,state,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (oid, o["name"], o["area"], dumps(list(o["tags"])), dumps(dict(o.get("state") or {})),
                         "world", sim_time))
                    notes.append(f"added '{oid}'")
        if notes:
            log.warning("world.yaml changed: the world was brought in step: %s", "; ".join(notes))
        return notes

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
                 "energy": float(cfg.settings["energy_start"]), "weather": world.weather,
                 "tendencies": dict(cfg.character["tendencies"]), "is_asleep": False,
                 "brain_online": True}
        with db.transaction(self.conn):
            world.sync(self.conn)
            db.save_state(self.conn, state, _now_real())
            db.set_meta(self.conn, "first_sim_time", start)
            db.set_meta(self.conn, "energy_scale", "v2")          # energy is -50..50
            db.set_meta(self.conn, "brain_model", cfg.settings["brain_model"])

    def _reconcile_config(self) -> None:
        """The YAML can be edited while a database already exists (it is mounted, not baked in).
        Bring the saved state in line with it so a changed config never makes every tick fail:
        new needs/tendencies start at their configured value, removed needs are dropped (learned tendencies
        are never dropped), and a
        creature standing in an area that no longer exists goes back to the start area, and the world's permanent
        objects follow world.yaml (`_sync_world`). Nothing the creature lived or made is reset."""
        st = db.get_state(self.conn)
        cfg, notes = self.cfg, []
        needs = {k: v for k, v in st["needs"].items() if k in cfg.character["needs"]}
        for k, spec in cfg.character["needs"].items():
            if k not in needs:
                needs[k] = float(spec["start"])
                notes.append(f"new need '{k}' starts at {spec['start']}")
        notes += [f"dropped need '{k}'" for k in st["needs"] if k not in needs]
        tend = dict(st["tendencies"])                        # learned tendencies are the creature's own: kept
        for k, v in cfg.character["tendencies"].items():     # ones seeded in the config are added if missing
            if k not in tend:
                tend[k] = float(v)
                notes.append(f"new tendency '{k}' starts at {v}")
        energy = float(st["energy"])
        if db.get_meta(self.conn, "energy_scale") != "v2":    # older databases kept energy as 0..100
            energy = character.clamp_energy(energy - 50)
            db.set_meta(self.conn, "energy_scale", "v2")
            notes.append("energy moved from the 0..100 scale to -50..50")
        area = st["area"]
        if area not in cfg.world["areas"]:
            area = cfg.character["start_area"]
            notes.append(f"area '{st['area']}' no longer exists: moved to '{area}'")
        self._sync_world(st["sim_time"])                     # the world's permanent things follow world.yaml
        if not notes:
            return
        log.warning("config changed since the last run; adjusting saved state: %s", "; ".join(notes))
        st = {**st, "needs": needs, "tendencies": tend, "area": area,
              "energy": energy, "is_asleep": bool(st["is_asleep"]),
              "brain_online": bool(st["brain_online"])}
        with db.transaction(self.conn):
            db.save_state(self.conn, st, _now_real())

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

    # ---------- prompts ----------
    def _self_image_lines(self) -> list[str]:
        """The config seed (if any), then the lines learned in sleep, oldest first."""
        rows = self.conn.execute("SELECT text FROM self_image ORDER BY formed_day, id").fetchall()
        return brain.seed_self_image(self.cfg) + [r["text"] for r in rows]

    def _brain_context(self, fired: list[dict], long_mems: list[dict] | None = None,
                       menu: list | None = None) -> brain.BrainContext:
        cfg, st, s = self.cfg, self.state, self.cfg.settings
        menu = self._menu() if menu is None else menu
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
            recent_actions=recent, preferences=prefs, self_image=self._self_image_lines(),
            stale_here=self._stale_here(), habits=HB.shown(self.conn, cfg), projects=PJ.brain_view(cfg, self.conn, st["sim_time"]),
            menu=menu, untried=self._untried(menu),
            just_woke_from_doze=self.last_kind in ("doze", "catchup"),
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
        recalled = self.memory.recall(self.memory.embed_one(q, "recall_query"), st["sim_time"])
        self.tracer.set_recall(q, self.memory.last_candidates)
        return recalled

    def _menu(self, st: dict | None = None) -> list:
        """The moves on offer right now: built from what is possible (seeded order), never by need."""
        cfg, s = self.cfg, self.cfg.settings
        st = st or self.state
        dt = clock.parse(st["sim_time"])
        inv = self.world.inventory()
        here = self.world.here(st["area"])
        can_sleep = (st["energy"] < s["sleep"]["wants_sleep_energy_below"]
                     or clock.is_night(dt, s["phases"]))
        seed = f"{st['sim_time']}|{st['area']}"
        drawing = DW.enabled(cfg) and any("paper" in o["tags"] for o in here)
        return MV.offered(cfg, area_id=st["area"], here=here, inventory=inv,
                          projects=PJ.active(self.conn), can_sleep=can_sleep,
                          inventory_full=len(inv) >= s["inventory_max"], seed=seed,
                          subjects=DW.offered_subjects(cfg, self.conn, self.world, seed) if drawing else [],
                          blocked=DW.blocked(cfg, self.world))

    def _untried(self, menu: list) -> set[str]:
        """Verbs on this menu it has never made (a fact of its own history; `moves.mark_untried`). The prompt marks
        them "(never tried)": what a move does, it can only find out by doing it. Not a ranking by need."""
        if not self.cfg.settings["moves"].get("mark_untried", True):
            return set()
        tried = {(r[0] or "").split(":")[0] for r in self.conn.execute(
            "SELECT DISTINCT move_key FROM ticks WHERE kind='tick' AND move_key IS NOT NULL")}
        return {m.verb for m in menu} - tried

    def _is_new_area(self, area_id: str, now: str) -> bool:
        """Somewhere not visited for `moves.novelty_hours` sim hours (or never) counts as new."""
        r = self.conn.execute("SELECT MAX(sim_time) AS t FROM ticks WHERE area=?", (area_id,)).fetchone()
        if not r or not r["t"]:
            return True
        hours = (clock.parse(now) - clock.parse(r["t"])).total_seconds() / 3600
        return hours >= self.cfg.settings["moves"]["novelty_hours"]

    def _resolve_move(self, mv, why: str, now: str) -> dict:
        """What the chosen move does, decided by code (cat.moves.resolve)."""
        cfg = self.cfg
        pool = self.world.here(self.state["area"]) + self.world.inventory()
        tags = next((o["tags"] for o in pool if o["id"] == mv.target_id), []) if mv.target_id else []
        novel = bool(cfg.moves["verbs"][mv.verb].get("novelty")) and self._is_new_area(mv.target_id, now)
        bucket = cfg.settings["projects"]["work_bucket"]
        res = MV.resolve(cfg, mv, target_tags=tags, rng=self.move_rng, novel=novel, project_progress_bucket=bucket)
        res["project"] = ({"id": int(mv.target_id), "advance": bucket, "note": why}
                          if res["does"] == "work_on" and str(mv.target_id).isdigit() else None)
        if res["does"] in MV.DRAWING_DOES:
            proj = DW.active_project(PJ.active(self.conn))
            res["drawing"] = DW.plan(cfg, self.conn, self.world, mv, res["does"], proj)
            res["changes"] = res["drawing"]["changes"]
            if proj and res["does"] in ("draw_new", "draw_more"):    # a sitting is the drawing project's work
                res["project"] = {"id": proj["id"], "advance": bucket, "note": why}
        return res

    def _repetition_multiplier(self, key: str, area: str) -> float:
        """Shrink the gains of the repetition-penalised needs if this same move (same verb and target) was
        made in this same place within the last `window` moves. Deterministic and kept in the database,
        so a restart does not forget it."""
        rp = self.cfg.settings["repetition_penalty"]
        rows = self.conn.execute("SELECT move_key, area FROM ticks WHERE kind='tick' ORDER BY id DESC LIMIT ?",
                                 (rp["window"],)).fetchall()
        return rp["multiplier"] if any(r["area"] == area and r["move_key"] == key for r in rows) else 1.0

    def _dislike_drains(self, minutes: float, action: str | None) -> tuple[dict[str, float], list[dict]]:
        """What the world drains this tick because something disliked is here."""
        st = self.state
        dislikes = [dict(r) for r in self.conn.execute(
            "SELECT subject, valence FROM preferences WHERE valence<0")]
        if not dislikes:
            return {}, []
        present = [self.cfg.world["areas"][st["area"]]["name"], self.world.weather, action or ""] \
            + [o["name"] for o in self.world.here(st["area"])]
        return character.dislike_drains(dislikes, present, minutes, self.cfg)

    def _ticks_in_area(self) -> int:
        return int(db.get_meta(self.conn, "ticks_in_area", "0") or 0)

    def _stale_here(self) -> bool:
        """True once it has spent `stale_after_ticks` ticks in the current area (0 turns this off).
        Only a flag for the prompt: it changes no need values."""
        n = self.cfg.settings["stale_after_ticks"]
        return n > 0 and self._ticks_in_area() >= n

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
                impulse = (impulses.drive(cfg, self.conn, self.world, st["needs"], menu_now, st["area"],
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
                            "ticks_in_area": self._ticks_in_area(), "stale_after_ticks": s["stale_after_ticks"],
                            "stale_text": cfg.character.get("stale_here") if ctx.stale_here else None})
                        b = brain.think(self.backend, cfg, ctx)
                mv = b["move"]
                res = self._resolve_move(mv, b["why"], now)
                # what the diary shows as having happened is code's plain account: no prose layer can add to it
                n = {"outcome": res["template"], "gains": res["gains"], "effort": res["effort"],
                     "duration": res["duration"], "changes": res["changes"], "project": res["project"],
                     "does": res["does"], "template": res["template"], "drawing": res.get("drawing")}
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
            drains = character.event_drains(fired)
            needs = character.apply_drains(needs, drains)
            dis, _ = self._dislike_drains(minutes, b["move"].label)
            needs = character.apply_drains(needs, dis)
            scale = character.duration_scale(cfg, n["duration"])
            needs = character.apply_gains(needs, n["gains"], cfg,
                                          consumed=fx.consumed, multiplier=mult, scale=scale)
            if adv and adv.get("ok") and adv["completed"]:        # finishing a project feels good (code pays it)
                needs = character.add_points(needs, cfg.settings["projects"]["completion_gains"])
            energy = character.spend_energy(st["energy"], n["effort"], cfg, scale=scale)
            if adv is not None:
                self.tracer.set_project({**adv, "repetition_exempt": exempt})
            self.tracer.set_effects(fx.log)
            self.tracer.set_need_math(character.explain_needs(
                before, minutes, n["gains"], cfg, needs, consumed=fx.consumed, multiplier=mult, scale=scale,
                anxiety=anx_prev, drains=drains, dislike_drains=dis))
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

        mood_inputs: dict = {}
        if impulse:
            mood_inputs["impulse"] = {k: v for k, v in impulse.items() if k != "move"} | {"move": impulse["move"].key}
        anx = character.anxiety_next(anx_prev, needs, energy, minutes, cfg)
        mood_inputs["anxiety_before"] = round(anx_prev, 2)
        mood = character.compute_mood(needs, nudge, feeling, energy, cfg, trace=mood_inputs, anxiety=anx)
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

    def _sketch(self, dplan: dict, minutes: float) -> dict | None:
        """Its own marks for this sitting (cat/drawings.py `sketch`, one LLM call, outside the transaction), placed
        knowing the subject's real facts, what is already on this sheet and its earlier drawings. None: code's
        marks are drawn for the sitting instead."""
        cfg, st = self.cfg, self.state
        if dplan["does"] == "draw_new":
            sub, sittings, exclude = dplan["subject"], [], None
        else:
            d = DW.row_for(self.conn, dplan["object_id"])
            if not d:
                return None
            sub = {"kind": d["subject_kind"], "key": d["subject_key"], "name": d["subject_name"],
                   "tags": d["subject_tags"]}
            sittings, exclude = d["sittings"], d["id"]
        with self.tracer.stage("sketch"):
            return DW.sketch(self.backend, cfg, self.conn, sub=sub, minutes=minutes,
                             colours=DW.colours_here(cfg, self.world.here(st["area"])), sittings=sittings,
                             exclude_id=exclude, mood=st["mood"], energy=st["energy"])

    def _journal(self, b: dict, row: dict, fired: list[dict], before: dict, needs: dict, mood: dict,
                 energy: float, recalled: list[dict], impulse: dict | None,
                 finishing: str | None = None) -> tuple[str | None, str | None]:
        """The entity's own entry for this moment, from facts only (cat/journal.py), and the title it gave a drawing
        it has just finished. Without an entry (journaling off or the call failed) an impulse keeps its fixed
        first-person line; otherwise the moment has no entry."""
        cfg, st = self.cfg, self.state
        prefs = [dict(r) for r in self.conn.execute(
            "SELECT subject, valence FROM preferences ORDER BY abs(valence) DESC LIMIT ?",
            (2 * cfg.settings["preferences_shown"],))]
        with self.tracer.stage("journal"):
            out = journal.write_full(
                self.backend, cfg, sim_time=st["sim_time"], area_id=st["area"], weather=self.world.weather,
                action=row["action"], why=b.get("why") or "", impulse=bool(impulse), result=row["result"],
                events=[e["text"] for e in fired], before=before, after=needs, mood=mood, energy=energy,
                self_image=self._self_image_lines(), tendencies=st["tendencies"], preferences=prefs,
                projects=PJ.brain_view(cfg, self.conn, st["sim_time"]), memories=[m["text"] for m in recalled],
                finishing=finishing)
        if out["entry"] is None and impulse:
            return impulse["entry"], out["title"]
        return out["entry"], out["title"]

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
        drains = character.event_drains(fired)
        needs = character.apply_drains(needs, drains)
        dis, _ = self._dislike_drains(minutes, act["label"])
        needs = character.apply_drains(needs, dis)
        self.tracer.set_need_math(character.explain_needs(before, minutes, None, cfg, needs, anxiety=anx_prev,
                                                          drains=drains, dislike_drains=dis))
        mood_inputs: dict = {}
        anx = character.anxiety_next(anx_prev, needs, st["energy"], minutes, cfg)
        mood = character.compute_mood(needs, nudge, None, st["energy"], cfg, trace=mood_inputs, anxiety=anx)
        self.tracer.set_mood(mood_inputs, mood)
        remaining = act["remaining"] - minutes
        new_act = None if remaining <= 0 else {**act, "remaining": remaining}
        new_state = {**st, "sim_time": clock.advance(now, minutes), "needs": needs, "mood": mood,
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
        drains = character.event_drains(fired)
        needs = character.apply_drains(needs, drains)
        calm = 1.0 - cfg.settings["anxiety"]["sleep_restore_penalty"] * anx_prev / 100 if cfg.settings["anxiety"]["enabled"] else 1.0
        nest = any("nest" in o["tags"] for o in self.world.here(st["area"]))     # its own place: it rests deeper
        deep = float(sl.get("nest_restore_multiplier", 1.0)) if nest else 1.0
        energy = character.restore_energy(st["energy"], sl["restore_per_sim_hour"] * minutes / 60 * calm * deep)   # an anxious sleep rests less
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
                                               "nest": nest, "nest_factor": deep}}
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
                db.set_meta(self.conn, "sleep_pending", 1)
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

    # ---------- dev commands that need engine internals ----------
    def force_sleep_cycle(self) -> str:
        """Run the sleep cycle now (consolidate, drift tendencies, learn preferences) without
        waiting for the creature to be tired. Needs the brain online; attaches to the latest tick."""
        last = self.conn.execute("SELECT MAX(id) FROM ticks").fetchone()[0]
        if not last:
            raise RuntimeError("no ticks yet: nothing to consolidate")
        self._online_cache = None
        if not self._online():
            raise RuntimeError("brain is offline: a sleep cycle needs it")
        self.tracer.begin()
        with db.transaction(self.conn):
            db.set_meta(self.conn, "sleep_pending", 1)
            db.set_meta(self.conn, "sleep_tick_id", last)
            if not db.get_meta(self.conn, "sleep_started"):
                db.set_meta(self.conn, "sleep_started", self.state["sim_time"])
        self._try_sleep_cycle()
        done = db.get_meta(self.conn, "sleep_pending") == "0"
        self.tracer.flush(self.conn, last, "ok")
        if not done:
            raise RuntimeError("postponed: the brain call failed (see the LLM calls page); it stays pending")
        row = self.conn.execute("SELECT id, summary, new_memories, tendency_changes FROM sleep_log "
                                "ORDER BY id DESC LIMIT 1").fetchone()
        return (f"sleep cycle done (night #{row['id']}): {len(json.loads(row['new_memories']))} new memories, "
                f"{len(json.loads(row['tendency_changes']))} tendency changes")

    def reload_settings(self) -> list[str]:
        """Re-read the YAML (all four files are validated). Settings and prompts are applied live; a
        change to character.yaml / world.yaml needs a restart. Invalid YAML raises and changes nothing."""
        new = load_config(self.cfg.config_dir, self.cfg.overrides)
        old = dict(self.cfg.settings)
        changed = sorted(k for k in set(old) | set(new.settings) if old.get(k) != new.settings.get(k))
        self.cfg.settings.clear()
        self.cfg.settings.update(new.settings)             # same dict object: every component sees it
        self.cfg.prompts.clear()
        self.cfg.prompts.update(new.prompts)               # prompt text is live too
        self.cfg.moves.clear()
        self.cfg.moves.update(new.moves)                   # and so are the moves
        self.dev.publish_config(("settings", "prompts", "moves"))
        log.info("settings reloaded; changed: %s", changed, extra={"event": "settings_reloaded",
                                                                   "extra": {"changed": changed}})
        return changed

    def snapshot_db(self) -> Path:
        """Back up the database now (SQLite online backup API, safe while running)."""
        dest_dir = Path(self.cfg.db_path).parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"cat-{datetime.now():%Y-%m-%d-%H%M%S}-snapshot.db"
        out = sqlite3.connect(dest)
        try:
            self.conn.backup(out)
            if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("snapshot failed its integrity check")
        finally:
            out.close()
        return dest

    def queue_injected_event(self, args: dict) -> None:
        """Add a custom world event to the next tick (text, optional object to spawn)."""
        areas = self.cfg.world["areas"]
        if args.get("area") and args["area"] not in areas:
            raise CommandError(f"unknown area '{args['area']}' (areas: {', '.join(areas)})")
        sp = args.get("spawn")
        if sp:
            sp = {**sp, "area": sp.get("area") or args.get("area") or self.state["area"]}
            if sp["area"] not in areas:
                raise CommandError(f"unknown spawn area '{sp['area']}'")
            allowed = self.cfg.settings["allowed_tags"]
            sp["tags"] = [t for t in sp["tags"] if t in allowed]
            if len(sp["name"]) > self.cfg.settings["name_max_length"]:
                raise CommandError("spawn name too long")
        self.injected.append({"text": args["text"], "area": args.get("area"), "spawn": sp})

    def _director_happening(self, fired: list[dict], st: dict, now: str, dt) -> list[dict]:
        """The world director: now and then an LLM proposes one small happening in this area. It runs
        every `director.every_ticks` action ticks, or `stale_every_ticks` when the place has gone stale;
        never while asleep or exhausted, never with the brain offline, and it counts toward
        `events_max_per_tick`. A failed call is only a missed happening: the tick carries on.
        It never sees the entity's needs or mood. Code validates the proposal and applies it as an event."""
        cfg, s = self.cfg, self.cfg.settings
        d = s["director"]
        if not d["enabled"] or len(fired) >= s["events_max_per_tick"]:
            return []
        if st["is_asleep"] or (clock.is_night(dt, s["phases"]) and st["energy"] < s["sleep"]["fall_asleep_energy_below"]):
            return []
        gap = int(db.get_meta(self.conn, "director_gap", "0") or 0)
        due, why = DR.due(cfg, gap, self._stale_here())
        if not due or not self._online():
            return []
        area = st["area"]
        rec: dict = {"trigger": why, "gap": gap}
        out: dict = {"action": "failed"}
        with self.tracer.stage("director"):
            recent = [r["text"] for r in self.conn.execute(
                "SELECT text FROM events ORDER BY id DESC LIMIT ?", (d["avoid_repeat"],))][::-1] \
                if d["avoid_repeat"] else []
            acts = [r["action"] for r in self.conn.execute(
                "SELECT action FROM ticks WHERE kind='tick' ORDER BY id DESC LIMIT ?",
                (d["recent_actions_shown"],))][::-1] if d["recent_actions_shown"] else []
            msgs = DR.messages(cfg, area_id=area, sim_time=now, weather=self.world.weather,
                               objects=[o["name"] for o in self.world.here(area)],
                               recent_events=recent, recent_actions=acts, stale=self._stale_here())
            try:
                raw = DR.propose(self.backend, cfg, msgs)
                out = DR.validate(cfg, raw, recent, len(self.world.here(area)))
                rec["raw"] = raw
            except BackendError as e:
                log.warning("world director unavailable (%s): no happening this time", e)
                out = {"action": "failed", "reason": str(e)}
        db.set_meta(self.conn, "director_gap", 0)
        self.tracer.set_director({**rec, **out})
        if out["action"] != "happened":
            return []
        record = {"text": out["text"], "area": area, "mood": out["mood"], "spawned": None,
                  "drains": out["drains"], "director": True}
        if out["weather"] and out["weather"] != self.world.weather:
            self.world.weather = out["weather"]
        if out["spawn"]:
            o = self.world.add_object(out["spawn"]["name"], out["spawn"]["tags"], area, "director", now)
            record["spawned"] = o["id"]
            record["spawn_info"] = {"op": "create", "source": "director", "object_id": o["id"],
                                    "area": o["area"], "name": o["name"], "label": o["name"]}
        return [record]

    def _take_injected(self, now: str, rolls: list[dict]) -> list[dict]:
        """Turn queued dev events into fired events (same shape roll_events returns)."""
        out = []
        while self.injected:
            ev = self.injected.pop(0)
            rec = {"text": ev["text"], "area": ev["area"], "mood": 0.0, "spawned": None, "injected": True}
            sp = ev["spawn"]
            if sp and len(self.world.here(sp["area"])) < self.cfg.settings["max_objects_per_area"]:
                o = self.world.add_object(sp["name"], sp["tags"], sp["area"], "event", now)
                rec["spawned"] = o["id"]
                rec["spawn_info"] = {"op": "create", "source": "event", "object_id": o["id"],
                                     "area": o["area"], "name": o["name"], "label": o["name"]}
            rolls.append({"event_text": ev["text"], "chance": None, "roll": None, "fired": 1,
                          "filtered_reason": "injected from the dev dashboard"
                          + ("" if not sp or rec["spawned"] else " (spawn skipped: area full)")})
            out.append(rec)
        return out

    def install_signal_handlers(self) -> None:
        def _stop(signum, frame):
            log.info("signal %s: stopping after this tick", signum)
            self.stop_requested = True
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

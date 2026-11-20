"""Keeping a life and its world across updates: backups, migrations, the world sync, sources, first start. Part of `cat.engine.Engine` (a mixin: the methods use the engine's state)."""
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


class UpkeepMixin:
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

    def _backfill_knowledge(self) -> None:
        """Once, for a life that began before things' properties were hidden: what it already did taught it
        something. Rebuilt from its past moves (verb:target) against what those things are now; looking closely
        taught everything, other moves what they needed. Never removes anything it knows."""
        if db.get_meta(self.conn, "knowledge_backfilled"):
            return
        verbs, rows = self.cfg.moves["verbs"], []
        for r in self.conn.execute("SELECT move_key, MIN(sim_time) AS t, MIN(id) AS id FROM ticks "
                                   "WHERE kind='tick' AND move_key LIKE '%:%' GROUP BY move_key"):
            verb, _, tid = r["move_key"].partition(":")
            obj, v = self.world.objects.get(tid), verbs.get(verb)
            if not obj or not v:
                continue
            tags = set(obj.get("tags") or [])
            found = tags if v.get("reveals") else tags & set(v.get("any_tags") or [])
            rows += [(obj["name"].lower(), g, r["t"], "remembered from before", r["id"]) for g in sorted(found)]
        with db.transaction(self.conn):
            self.conn.executemany("INSERT OR IGNORE INTO known_properties(name,tag,learned_sim_time,how,tick_id)"
                                  " VALUES(?,?,?,?,?)", rows)
            db.set_meta(self.conn, "knowledge_backfilled", 1)
        if rows:
            log.info("rebuilt %d known properties from its past moves", len(rows))

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

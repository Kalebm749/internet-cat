"""World state: objects, weather, world events. Held in memory, synced to the DB in the
tick's transaction. (Validated narrator effects arrive in M3 via effects.py.)"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime
from sqlite3 import Connection
from typing import Any

from cat import clock
from cat.config import Config
from cat.db import dumps


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    for art in ("a_", "an_", "the_"):
        if s.startswith(art):
            s = s[len(art):]
    return s or "thing"


class WorldState:
    def __init__(self, cfg: Config, objects: dict[str, dict], weather: str):
        self.cfg = cfg
        self.objects = objects
        self.weather = weather
        self._dirty: set[str] = set()
        self._deleted: set[str] = set()

    # --- construction ---
    @classmethod
    def seed(cls, conn: Connection, cfg: Config, now: str) -> "WorldState":
        objs = {}
        for o in cfg.world["objects"]:
            objs[o["id"]] = {"id": o["id"], "name": o["name"], "area": o["area"],
                             "tags": list(o["tags"]), "state": dict(o.get("state") or {}),
                             "created_by": "world", "created_at": now}
        w = cls(cfg, objs, (cfg.world.get("weather") or ["clear"])[0])
        w._dirty = set(objs)
        return w

    @classmethod
    def load(cls, conn: Connection, cfg: Config, weather: str) -> "WorldState":
        objs = {}
        for r in conn.execute("SELECT * FROM objects"):
            objs[r["id"]] = {"id": r["id"], "name": r["name"], "area": r["area"],
                             "tags": json.loads(r["tags"]), "state": json.loads(r["state"]),
                             "created_by": r["created_by"], "created_at": r["created_at"]}
        return cls(cfg, objs, weather)

    # --- queries ---
    def here(self, area: str) -> list[dict]:
        return [o for o in self.objects.values() if o["area"] == area]

    def inventory(self) -> list[dict]:
        return self.here("inventory")

    def in_reach(self, area: str) -> list[dict]:
        return self.here(area) + self.inventory()

    # --- mutation (all go through these so dirty tracking stays right) ---
    def new_id(self, name: str) -> str:
        base = _slug(name)
        if base not in self.objects and base not in self._deleted:
            return base
        i = 2
        while f"{base}_{i}" in self.objects or f"{base}_{i}" in self._deleted:
            i += 1
        return f"{base}_{i}"

    def add_object(self, name: str, tags: list[str], area: str, created_by: str, now: str,
                   state: dict | None = None) -> dict:
        oid = self.new_id(name)
        o = {"id": oid, "name": name, "area": area, "tags": list(tags),
             "state": dict(state or {}), "created_by": created_by, "created_at": now}
        self.objects[oid] = o
        self._dirty.add(oid)
        return o

    def remove_object(self, oid: str) -> None:
        self.objects.pop(oid, None)
        self._dirty.discard(oid)
        self._deleted.add(oid)

    def touch(self, oid: str) -> None:
        if oid in self.objects:
            self._dirty.add(oid)

    def sync(self, conn: Connection) -> None:
        """Write only what changed. Call inside the tick transaction."""
        for oid in self._deleted:
            conn.execute("DELETE FROM objects WHERE id=?", (oid,))
        for oid in self._dirty:
            o = self.objects[oid]
            conn.execute(
                "INSERT INTO objects(id,name,area,tags,state,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "area=excluded.area, tags=excluded.tags, state=excluded.state",
                (o["id"], o["name"], o["area"], dumps(o["tags"]), dumps(o["state"]),
                 o["created_by"], o["created_at"]))
        self._dirty.clear()
        self._deleted.clear()

    # --- events ---
    def roll_events(self, rng: random.Random, dt: datetime, creature_area: str,
                    now: str) -> list[dict[str, Any]]:
        """Roll world events for this tick. `area` on an event means the creature must be there
        to perceive it. Applies weather changes and object spawns; returns what fired."""
        s = self.cfg.settings
        fired: list[dict] = []
        for ev in self.cfg.world.get("events", []):
            if len(fired) >= s["events_max_per_tick"]:
                break
            if "area" in ev and ev["area"] != creature_area:
                continue
            if "hours" in ev and not clock.hour_in_range(dt, ev["hours"][0], ev["hours"][1]):
                continue
            if "only_weather" in ev and ev["only_weather"] != self.weather:
                continue
            if "set_weather" in ev and ev["set_weather"] == self.weather:
                continue
            if rng.random() >= ev["chance"]:
                continue
            record = {"text": ev["text"], "area": ev.get("area"), "mood": ev.get("mood", 0.0),
                      "spawned": None}
            if "set_weather" in ev:
                self.weather = ev["set_weather"]
            if "spawn" in ev:
                sp = ev["spawn"]
                if len(self.here(sp["area"])) < s["max_objects_per_area"]:
                    o = self.add_object(sp["name"], sp["tags"], sp["area"], "event", now,
                                        sp.get("state"))
                    record["spawned"] = o["id"]
            fired.append(record)
        return fired

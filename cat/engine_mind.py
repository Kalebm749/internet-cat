"""What it is shown and how it is answered: the menu, the prompts, recall, moves resolved, sketch and journal. Part of `cat.engine.Engine` (a mixin: the methods use the engine's state)."""
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


class MindMixin:
    def _experiences(self, n: dict, row: dict, decayed: dict, needs: dict, anx_rise: float) -> list[dict]:
        """One score for this decision (need points beyond plain decay: gains minus drains and hurts, minus the
        anxiety rise), recorded for each of its subjects: the place, the thing it acted on, and an activity without
        an object. Sleep turns the ones that keep going one way into likes and dislikes (cat/sleep.py evidence)."""
        cfg, st = self.cfg, self.state
        w = float((cfg.settings.get("experience") or {}).get("anxiety_weight", 0.5))
        ns = round(sum(needs[k] - decayed.get(k, needs[k]) for k in needs), 2)
        score = round(ns - w * anx_rise, 2)
        note = (n.get("hurt") or {}).get("outcome") or ""
        out = [{"kind": "move", "key": row["move_key"], "name": row["action"], "score": score, "note": note,
                "needs_score": ns},
               {"kind": "place", "key": st["area"], "name": cfg.world["areas"][st["area"]]["name"], "score": score,
                "note": note}]
        t = n.get("target")
        if t:
            out.append({"kind": "thing", "key": t["id"], "name": t["name"], "score": score, "note": note})
        elif n["does"] == "none":
            out.append({"kind": "activity", "key": row["move_key"], "name": MV.doing_phrase(cfg, row["action"]),
                        "score": score, "note": note})
        return out

    def _move_record(self, keys: list[str]) -> dict[str, list[dict]]:
        """Its latest experiences of these moves (newest first), at most experience.lookback each."""
        if not keys:
            return {}
        n = int((self.cfg.settings.get("experience") or {}).get("lookback", 12))
        q = ",".join("?" * len(keys))
        out: dict[str, list[dict]] = {}
        for r in self.conn.execute(f"SELECT subject_key, score, note, needs_score FROM experiences WHERE subject_kind='move' "
                                   f"AND subject_key IN ({q}) ORDER BY id DESC", keys):
            g = out.setdefault(r["subject_key"], [])
            if len(g) < n:
                g.append(dict(r))
        return out

    def _history_notes(self, menu: list) -> dict[str, str]:
        """What its own record says about moves on the menu, in words: a hurt the last time ("last time: you
        slipped"), or how it has gone ("it went badly 3 of the last 4 times", "it did you good ..."). Only with real
        evidence (experience.note_min times); never a ranking by need."""
        cfg = self.cfg
        ex = cfg.settings.get("experience") or {}
        R = lambda key, **kw: P.render(cfg, "brain.situation." + key, **kw)
        out = {}
        for key, rows in self._move_record([m.key for m in menu]).items():
            hurt = next((r["note"] for r in rows[:3] if r["note"]), "")
            if hurt:
                out[key] = R("note_hurt", what=hurt.rstrip(".").lower())
                continue
            if len(rows) < int(ex.get("note_min", 2)):
                continue
            bad = sum(1 for r in rows if r["score"] < 0)
            good = sum(1 for r in rows if r["score"] >= float(ex.get("note_good_at", 8)))
            if bad * 2 >= len(rows):
                out[key] = R("note_bad", k=bad, n=len(rows))
            elif good * 2 >= len(rows):
                out[key] = R("note_good", k=good, n=len(rows))
        return out

    def _expected(self, key: str) -> float | None:
        """What its record leads it to expect from a move (mean needs score of its latest times; None if it has
        too little record to expect anything)."""
        rows = [r for r in self._move_record([key]).get(key, []) if r["needs_score"] is not None]
        if len(rows) < int((self.cfg.settings.get("experience") or {}).get("note_min", 2)):
            return None
        return sum(r["needs_score"] for r in rows) / len(rows)

    def _known(self) -> dict[str, set[str]]:
        """What it knows about things: {thing name (lower): {tags}}. The prompt shows only these."""
        out: dict[str, set[str]] = {}
        for r in self.conn.execute("SELECT name, tag FROM known_properties"):
            out.setdefault(r["name"], set()).add(r["tag"])
        return out

    def _learned(self, mv, n: dict, now: str) -> list[dict]:
        """What this move taught it about the thing it acted on: looking closely shows all its properties; doing
        something with it shows the ones the move needed (eating: edible; climbing: climbable); a hurt shows what
        hurt (the puddle: cold). [{name, tag, how}] for properties it did not know yet."""
        t = n.get("target")
        if not t:
            return []
        verb = self.cfg.moves["verbs"][mv.verb]
        tags = set(t.get("tags") or [])
        found = set(tags) if verb.get("reveals") else tags & set(verb.get("any_tags") or [])
        for h in verb.get("hurts") or []:
            if n.get("hurt") and n["hurt"]["outcome"] == h["outcome"]:
                found |= tags & set(h.get("target_tags") or [])
        known = self._known().get(t["name"].lower(), set())
        how = "looked closely" if verb.get("reveals") else f"from {mv.label}"
        return [{"name": t["name"].lower(), "tag": g, "how": how} for g in sorted(found - known)]

    def _anxiety_extra(self) -> dict:
        """What stirs anxiety besides needs and energy: a place it hardly knows yet, and disliked things here."""
        cfg, st = self.cfg, self.state
        e = cfg.settings.get("emotion") or {}
        visits = self.conn.execute("SELECT COUNT(*) FROM ticks WHERE kind='tick' AND area=?", (st["area"],)).fetchone()[0]
        fam = float(e.get("familiar_after", 8))
        unfamiliar = float(e.get("unfamiliar_points", 0)) * max(0.0, 1 - visits / fam) if fam else 0.0
        _, hits = self._dislike_drains(60, None)
        disliked = min(float(e.get("max_disliked_points", 25)),
                       float(e.get("disliked_points", 0)) * sum(abs(h["valence"]) for h in hits))
        return {"unfamiliar": round(unfamiliar, 2), "disliked": round(disliked, 2),
                "total": round(unfamiliar + disliked, 2), "disliked_here": [h["subject"] for h in hits]}

    def _surprise(self, expected: float | None, needs: dict, decayed: dict) -> dict | None:
        """How far this move went from what its record led it to expect (settings `emotion`): worse by more than
        `surprise_threshold` need points raises anxiety at once; better lifts mood a little. None when it had no
        expectation or it went about as expected."""
        if expected is None:
            return None
        e = self.cfg.settings.get("emotion") or {}
        got = sum(needs[k] - decayed.get(k, needs[k]) for k in needs)
        diff, thr = got - expected, float(e.get("surprise_threshold", 5))
        if abs(diff) <= thr:
            return None
        beyond = abs(diff) - thr
        worse = diff < 0
        anx = min(float(e.get("max_surprise_anxiety", 25)), beyond * float(e.get("surprise_anxiety_per_point", 0))) if worse else 0.0
        mood = 0.0 if worse else min(float(e.get("max_surprise_mood", 0.5)), beyond * float(e.get("surprise_mood_per_point", 0)))
        return {"expected": round(expected, 2), "got": round(got, 2), "worse": worse,
                "anxiety": round(anx, 2), "mood": round(mood, 3)}

    def _harsh(self, minutes: float) -> dict:
        """What the place and the weather take from it over these minutes (rain in the open, a noisy market)."""
        st = self.state
        return harms.exposure(self.cfg, st["area"], self.world.weather, st["sim_time"], minutes)

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
            menu=menu, untried=self._untried(menu), notes=self._history_notes(menu), known=self._known(),
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
        blocked, effort = set(DW.blocked(cfg, self.world)), set()
        ex = s.get("exhaustion") or {}
        if character.energy_word(st["energy"], cfg) == "exhausted":       # the body can't: no effort, no long work
            effort |= set(ex.get("blocks_effort") or [])
            blocked |= set(ex.get("blocks_does") or [])
        if any(character.need_band(v, cfg) == "empty" for v in st["needs"].values()):
            blocked |= set(s.get("empty_blocks_does") or [])             # no starting projects while a need is empty
        disliked = [r[0] for r in self.conn.execute("SELECT subject FROM preferences WHERE valence<0")]
        menu = MV.offered(cfg, area_id=st["area"], here=here, inventory=inv,
                          projects=PJ.active(self.conn), can_sleep=can_sleep,
                          inventory_full=len(inv) >= s["inventory_max"], seed=seed,
                          subjects=DW.offered_subjects(cfg, self.conn, self.world, seed) if drawing else [],
                          blocked=blocked, disliked=disliked, blocked_effort=effort)
        menu, dropped = MV.shape_by_mood(cfg, menu, (st.get("mood") or {}).get("word", ""), seed)
        self._menu_shaping = {"mood": (st.get("mood") or {}).get("word"), "left_off": dropped,
                              "exhausted": bool(effort), "blocked": sorted(blocked)}
        return menu

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

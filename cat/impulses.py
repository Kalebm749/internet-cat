"""Impulses: under enough anxiety a decision can be taken over before the creature has thought it through.

Code, seeded and deterministic (same time and place, same outcome), picks one of the configured impulses
(`anxiety.impulses`: a weight and the verbs it acts through) and one matching move from the CURRENT menu, so
an impulse can only ever do what the world offers. It never looks at which move would feed a need. Every
takeover is traced and the diary says so in the creature's own voice (prompts.yaml `impulse`)."""
from __future__ import annotations

import random

from cat import prompts as P
from cat.config import Config


def feeds(cfg: Config, move, tags: list[str]) -> dict[str, float]:
    """Points a move would add to each need (its verb's effects and by_tag buckets; novelty not counted)."""
    v = cfg.moves["verbs"][move.verb]
    gv = cfg.settings["gain_values"]
    out: dict[str, float] = {}
    for g in [v.get("effects") or {}] + [(v.get("by_tag") or {}).get(t) or {} for t in tags]:
        for nid, b in g.items():
            out[nid] = max(out.get(nid, 0.0), float(gv.get(str(b), 0)))
    return out


def _route(cfg: Config, frm: str, to: str) -> str | None:
    """The exit from `frm` on a shortest way to `to` (breadth-first over the areas' exits)."""
    areas = cfg.world["areas"]
    prev: dict[str, str] = {frm: frm}
    queue = [frm]
    while queue:
        a = queue.pop(0)
        if a == to:
            break
        for e in areas[a]["exits"]:
            if e not in prev:
                prev[e] = a
                queue.append(e)
    if to not in prev or to == frm:
        return None
    step = to
    while prev[step] != frm:
        step = prev[step]
    return step


def forage_target(cfg: Config, conn, world, area: str) -> str | None:
    """Where it goes looking for food, from its own history: the nearest place where it has eaten, or where it has
    been and something edible is (it saw it there). Knowing nowhere, the neighbouring place it has not been to
    for longest. Returns the next area to go to."""
    eat = {v for v, sp in cfg.moves["verbs"].items() if sp.get("does") == "consume"}
    rows = conn.execute("SELECT area, move_key FROM ticks WHERE kind='tick' AND move_key IS NOT NULL").fetchall()
    known = {r[0] for r in rows if r[1].split(":")[0] in eat}
    visited = {r[0] for r in conn.execute("SELECT DISTINCT area FROM ticks")}
    known |= {o["area"] for o in world.objects.values() if "edible" in o["tags"] and o["area"] in visited}
    known.discard(area)
    paths = [(_path(cfg, area, k), k) for k in sorted(known)]
    paths = [(pth, k) for pth, k in paths if pth and None not in pth]
    if paths:
        return min(paths, key=lambda pk: (len(pk[0]), pk[1]))[0][0]       # the first step towards the nearest
    last = {r[0]: r[1] for r in conn.execute("SELECT area, MAX(id) FROM ticks GROUP BY area")}
    exits = cfg.world["areas"][area]["exits"]
    return min(exits, key=lambda e: (last.get(e, -1), e)) if exits else None


def _path(cfg: Config, frm: str, to: str) -> list:
    """The areas on a shortest way from `frm` to `to` (excluding `frm`); [None] if there is none."""
    out, cur = [], frm
    while cur != to:
        cur = _route(cfg, cur, to)
        if cur is None:
            return [None]
        out.append(cur)
    return out


def drive(cfg: Config, conn, world, needs: dict[str, float], menu: list, area: str, seed: str) -> dict | None:
    """Innate drives (settings `drives`): the one place code picks a move because of a need. Only the most
    pressing need in a low or empty band may act, with the band's chance: it takes the move on the menu that feeds
    that need most. Hungry with nothing to eat here, it forages (`forage_target`). Rolled before the brain; traced
    like an impulse and shown in the diary. None when no drive acts."""
    from cat import character
    d = cfg.settings.get("drives") or {}
    if not d.get("enabled") or not menu:
        return None
    order = {"empty": 0, "low": 1}
    cands = sorted(((order[character.need_band(float(needs[n]), cfg)], float(needs[n]), n) for n in d["needs"]
                    if n in needs and character.need_band(float(needs[n]), cfg) in order))
    if not cands:
        return None
    _, _, nid = cands[0]
    band = character.need_band(float(needs[nid]), cfg)
    rng = random.Random(f"drive|{seed}")
    p = float(d["chance"][band])
    if rng.random() >= p:
        return None
    pool = {o["id"]: o["tags"] for o in world.here(area) + world.inventory()}
    scored = [(feeds(cfg, m, pool.get(m.target_id, [])).get(nid, 0.0), m) for m in menu]
    best = max((g for g, _ in scored), default=0.0)
    name, move = nid, None
    if best > 0:
        move = rng.choice([m for g, m in scored if g == best])
    elif nid == cfg.settings["sustenance_need"] and d.get("forage"):
        to = forage_target(cfg, conn, world, area)
        move = next((m for m in menu if cfg.moves["verbs"][m.verb].get("does") == "move_self" and m.target_id == to), None)
        name = "forage"
    if move is None:
        return None
    key = next((k for k in (f"impulse.drive_{name}", "impulse.drive") if P.lookup(cfg.prompts, k)), "impulse.default")
    return {"impulse": f"drive: {name}", "move": move, "chance": p, "entry": P.render(cfg, key), "drive": nid,
            "band": band}


def chance(cfg: Config, level: float) -> float:
    a = cfg.settings["anxiety"]
    if not a["enabled"] or level <= a["impulse_above"]:
        return 0.0
    return a["impulse_max_chance"] * min(1.0, (level - a["impulse_above"]) / max(1e-9, 100.0 - a["impulse_above"]))


def take_over(cfg: Config, level: float, menu: list, seed: str) -> dict | None:
    """None, or {"impulse": name, "move": a Move from `menu`, "chance": p, "entry": first-person text}."""
    p = chance(cfg, level)
    if p <= 0 or not menu:
        return None
    rng = random.Random(f"impulse|{seed}")
    if rng.random() >= p:
        return None
    options = []
    for name, spec in cfg.settings["anxiety"]["impulses"].items():
        moves = [m for m in menu if m.verb in spec["verbs"]]
        if moves:
            options.append((name, float(spec["weight"]), moves))
    if not options:
        return None
    name, _, moves = rng.choices(options, weights=[w for _, w, _ in options])[0]
    move = rng.choice(moves)
    key = f"impulse.{name}"
    entry = P.render(cfg, key if P.lookup(cfg.prompts, key) else "impulse.default")
    return {"impulse": name, "move": move, "chance": round(p, 3), "entry": entry}

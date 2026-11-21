"""When hunger takes over (docs/rewrite-contracts.md §2.5): the one place code, not the model, picks the move.

From `body.starving_at` hunger it walks toward the place with food, one step at a time, and eats there (world
`starving`: the eating action and what the world leaves if the food is gone). No model call, no menu: a decision
the loop records like any other, marked `forced: starving`, so the diary and the dev dashboard can say so.
"""
from __future__ import annotations

from collections import deque

from cat.decision import Decision

NOTE = "forced: starving"


def _first_step(content, here: str, goal: str) -> str | None:
    """The exit to take from `here` on the shortest way to `goal` (None if there is none)."""
    seen, queue = {here}, deque([(here, None)])
    while queue:
        place, first = queue.popleft()
        for x in content.locations[place].exits:
            if x in seen or x not in content.locations:
                continue
            seen.add(x)
            step = first or x
            if x == goal:
                return step
            queue.append((x, step))
    return None


def decide(life, executor, tuning: dict) -> tuple[Decision, list] | None:
    """(decision, world ops to apply first) when hunger has taken over, else None."""
    spec = life.content.world.get("starving")
    if not spec or life.asleep or life.bodystate.hunger < tuning["body"]["starving_at"]:
        return None
    food = life.content.actions[spec["eat"]]
    running = executor.running
    if life.location == food.location:
        restock: list = []
        if executor.check(food.id, {}) is not None:
            restock = list(spec.get("restock") or [])           # nothing to eat: the world leaves something
        if running is not None and running.action.id == food.id:
            return None                                          # already eating
        return Decision(kind="full", action=food.id, notes=[NOTE]), restock
    step = _first_step(life.content, life.location, food.location)
    if step is None:
        return None
    if running is not None and running.action.builtin == "move" and running.inputs.get("to") == step:
        return Decision(kind="check_in", action="continue", notes=[NOTE]), []
    return Decision(kind="full", action="move", inputs={"to": step}, notes=[NOTE]), []

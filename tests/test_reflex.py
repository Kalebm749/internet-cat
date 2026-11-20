"""Drives (settings `drives`): when a need is low or empty, its drive may take the move that feeds it most, or
forage for food; the one place code picks by need. Plus the menu shuffle and the "(never tried)" marks."""
import json
import random

from cat import impulses, moves as M
from cat.engine import Engine
from cat.llm import MockBackend
from tests.helpers import pick


def on(cfg, low=1.0, empty=1.0):
    cfg.settings["drives"].update(enabled=True, chance={"low": low, "empty": empty})


def make(cfg):
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    calls = []
    b.handlers["brain"] = lambda m, s: (calls.append(1), pick(m))[1]
    e = Engine(cfg, b, rng=random.Random(1))
    return e, calls


def test_a_low_need_drives_the_move_that_feeds_it_and_a_fine_one_never_does(cfg):
    on(cfg)
    e, _ = make(cfg)
    sus = cfg.settings["sustenance_need"]
    e.state["area"] = "kitchen"
    fine = {k: 80.0 for k in e.state["needs"]}
    assert impulses.drive(cfg, e.conn, e.world, fine, e._menu(), "kitchen", "s") is None
    d = impulses.drive(cfg, e.conn, e.world, {**fine, sus: 5.0}, e._menu(), "kitchen", "s")
    assert d["drive"] == sus and d["move"].verb == "eat" and d["band"] == "empty"
    cfg.settings["drives"]["chance"]["low"] = 0.0
    assert impulses.drive(cfg, e.conn, e.world, {**fine, sus: 30.0}, e._menu(), "kitchen", "s") is None   # low: chance 0


def test_hungry_with_nothing_here_it_forages_towards_food(cfg):
    on(cfg)
    e, _ = make(cfg)
    sus = cfg.settings["sustenance_need"]
    e.state["area"] = "garden"
    with e.conn:                                   # it has been in the kitchen, where the fish is
        e.conn.execute("INSERT INTO ticks(kind, sim_time, area) VALUES('tick', ?, 'kitchen')", (e.state["sim_time"],))
    d = impulses.drive(cfg, e.conn, e.world, {**{k: 80.0 for k in e.state["needs"]}, sus: 5.0}, e._menu(), "garden", "s")
    assert d["impulse"] == "drive: forage" and d["move"].verb == "go"
    path = impulses._path(cfg, "garden", "kitchen")
    assert d["move"].target_id == path[0]


def test_in_the_engine_a_drive_needs_no_brain_and_is_traced(cfg):
    on(cfg)
    cfg.settings["journal"]["enabled"] = False
    e, calls = make(cfg)
    e.state["area"] = "kitchen"
    e.state["needs"][cfg.settings["sustenance_need"]] = 5.0
    e.tick()
    row = e.conn.execute("SELECT intent, move_key, entry FROM ticks WHERE kind='tick'").fetchone()
    assert calls == [] and row["move_key"].startswith("eat:") and row["intent"].startswith("an impulse (drive")
    mi = json.loads(e.conn.execute("SELECT inputs FROM mood_math").fetchone()["inputs"])
    assert mi["impulse"]["drive"] == cfg.settings["sustenance_need"]


def test_a_need_newly_low_interrupts_what_it_was_doing(cfg):
    e, _ = make(cfg)
    act = {"label": "x", "remaining": 40, "total": 60, "empty_at_start": [], "low_at_start": []}
    st = {"needs": {**{k: 80.0 for k in e.state["needs"]}, "joy": 30.0}}
    assert e._interrupt_reason(act, [], st) == "need"
    assert e._interrupt_reason({**act, "low_at_start": ["joy"]}, [], st) is None      # it was already low


def test_the_menu_shuffle_is_seeded_and_changes_the_order(cfg):
    cfg.settings["moves"]["shuffle"] = True
    here = [{"id": f"o{i}", "name": f"thing {i}", "tags": ["toy", "movable"], "state": {}} for i in range(4)]
    a = M.offered(cfg, area_id="kitchen", here=here, inventory=[], projects=[], can_sleep=False, seed="x")
    b = M.offered(cfg, area_id="kitchen", here=here, inventory=[], projects=[], can_sleep=False, seed="x")
    c = M.offered(cfg, area_id="kitchen", here=here, inventory=[], projects=[], can_sleep=False, seed="y")
    cfg.settings["moves"]["shuffle"] = False
    plain = M.offered(cfg, area_id="kitchen", here=here, inventory=[], projects=[], can_sleep=False, seed="x")
    keys = lambda ms: [m.key for m in ms]
    assert keys(a) == keys(b) and sorted(keys(a)) == sorted(keys(plain))      # same moves, same seed same order
    assert keys(a) != keys(plain) or keys(c) != keys(plain)                   # but not file order
    assert [m.id for m in a] == [f"m{i}" for i in range(1, len(a) + 1)]       # ids follow the shown order


def test_moves_it_has_never_made_are_marked_until_it_makes_them(cfg):
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    prompts = []
    b.handlers["brain"] = lambda m, s: (prompts.append(m[-1]["content"]), pick(m, "look out"))[1]
    e = Engine(cfg, b, rng=random.Random(1))
    e.tick()
    e.tick()
    assert "look out and watch the world for a while (never tried)" in prompts[0]
    assert "look out and watch the world for a while (never tried)" not in prompts[1]
    assert "(never tried)" in prompts[1]                                      # other verbs still are
    cfg.settings["moves"]["mark_untried"] = False
    e.tick()
    assert "(never tried)" not in prompts[2]

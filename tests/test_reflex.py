"""The feeding reflex (settings reflexes.feeding): the one innate link from a need to a move. It fires only when
sustenance is in a configured band and something edible is on the menu; it is rolled before the brain, traced
like an impulse and shown in the diary. Plus the menu shuffle and the "(never tried)" marks."""
import json
import random

from cat import impulses, moves as M
from cat.engine import Engine
from cat.llm import MockBackend
from tests.helpers import pick


def on(cfg, chance=1.0):
    cfg.settings["reflexes"]["feeding"].update(enabled=True, chance=chance, bands=["empty"], verbs=["eat"])


def menu(cfg, food=True):
    here = [{"id": "fish", "name": "a bowl of fish", "tags": ["edible"], "state": {}}] if food else []
    return M.offered(cfg, area_id="kitchen", here=here, inventory=[], projects=[], can_sleep=False)


def needs(cfg, sus):
    return {k: (sus if k == cfg.settings["sustenance_need"] else 80.0) for k in cfg.character["needs"]}


def test_the_reflex_needs_an_empty_sustenance_and_food_on_the_menu(cfg):
    on(cfg)
    assert impulses.feeding_reflex(cfg, needs(cfg, 5.0), menu(cfg), "s")["move"].verb == "eat"
    assert impulses.feeding_reflex(cfg, needs(cfg, 30.0), menu(cfg), "s") is None        # low, not empty
    assert impulses.feeding_reflex(cfg, needs(cfg, 5.0), menu(cfg, food=False), "s") is None
    cfg.settings["reflexes"]["feeding"]["enabled"] = False
    assert impulses.feeding_reflex(cfg, needs(cfg, 5.0), menu(cfg), "s") is None


def test_the_reflex_is_seeded_and_fires_at_about_its_chance(cfg):
    on(cfg, chance=0.35)
    hits = [impulses.feeding_reflex(cfg, needs(cfg, 5.0), menu(cfg), f"s{i}") for i in range(200)]
    assert 40 < sum(1 for h in hits if h) < 100
    assert impulses.feeding_reflex(cfg, needs(cfg, 5.0), menu(cfg), "s3") == \
        impulses.feeding_reflex(cfg, needs(cfg, 5.0), menu(cfg), "s3")


def test_in_the_engine_the_reflex_eats_without_a_brain_call_and_is_traced(cfg):
    on(cfg)
    cfg.settings["journal"]["enabled"] = False
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    calls = []
    b.handlers["brain"] = lambda m, s: (calls.append(1), pick(m))[1]
    e = Engine(cfg, b, rng=random.Random(1))
    e.state["area"] = "kitchen"
    e.state["needs"][cfg.settings["sustenance_need"]] = 5.0
    e.tick()
    row = e.conn.execute("SELECT action, intent, entry, move_key FROM ticks WHERE kind='tick'").fetchone()
    assert calls == [] and row["move_key"].startswith("eat:") and row["intent"] == "an impulse (feeding)"
    assert row["entry"] == "The hollow decided before I did."
    assert e.state["needs"][cfg.settings["sustenance_need"]] > 5.0
    mi = json.loads(e.conn.execute("SELECT inputs FROM mood_math").fetchone()["inputs"])
    assert mi["impulse"]["impulse"] == "feeding" and mi["impulse"]["reflex"] is True


def test_reflex_settings_are_validated(tmp_path):
    from tests.test_config import _problems
    msg = _problems(tmp_path, "settings.yaml", lambda s: s["reflexes"]["feeding"].update(
        verbs=["wait"], chance=2, bands=["starving"]))
    assert "'wait' is not a consume verb" in msg and "chance: must be a number from 0 to 1" in msg \
        and "bands: must be a non-empty list" in msg


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

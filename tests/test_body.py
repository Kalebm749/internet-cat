"""The body and the world have consequences: hurts, exhaustion, empty needs, mood on the menu, likes and dislikes
earned from what happened, turning away from what it dislikes, and a journal that starts from how it is.
Runs on the default town world and moves."""
import random
import shutil

import pytest

from cat import brain, harms, impulses, journal, moves as M, sleep
from cat.config import CONFIG_DIR, load_config
from cat.engine import Engine
from cat.llm import MockBackend


@pytest.fixture
def town(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    c = load_config(d)
    c.settings["db_path"] = str(tmp_path / "cat.db")
    c.settings["director"]["enabled"] = False
    c.world["events"] = []
    return c


def engine(town):
    b = MockBackend(seed=1)
    b.need_ids = list(town.character["needs"])
    e = Engine(town, b, rng=random.Random(1))
    e.world.events = None
    return e


def test_the_world_can_hurt(town):
    bread = {"id": "b", "name": "a fresh loaf of bread", "tags": ["edible"], "created_by": "source",
             "created_at": "2026-11-18T06:00"}
    assert harms.move_hurt(town, "eat", bread, 0, 30, "2026-11-18T09:00", "s") is None          # fresh
    stale = harms.move_hurt(town, "eat", bread, 0, 30, "2026-11-18T19:00", "s")                # 13 h later
    assert stale["gain_factor"] == 0.5 and stale["drains"] == {"joy": 6} and "stale" in stale["outcome"]
    assert harms.move_hurt(town, "ring", {"tags": ["rings"]}, 10, 30, "2026-11-18T09:00", "s") is None
    assert harms.move_hurt(town, "ring", {"tags": ["rings"]}, 60, 30, "2026-11-18T09:00", "s")["gain_factor"] == 0
    wet = harms.exposure(town, "garden", "rain", "2026-11-18T09:00", 60)
    assert wet["drains"]["peace"] > 0 and wet["energy"] > 0 and wet["texts"]
    assert harms.exposure(town, "nook", "rain", "2026-11-18T09:00", 60)["drains"] == {}       # sheltered
    assert harms.exposure(town, "market", "clear", "2026-11-18T10:00", 60)["drains"]["peace"] > 0   # busy
    assert harms.exposure(town, "market", "clear", "2026-11-18T20:00", 60)["drains"] == {}


def test_exhausted_it_cannot_climb_or_work_and_gives_in_to_rest(town):
    e = engine(town)
    e.state.update(area="clock_tower", energy=-40.0)
    menu = e._menu()
    assert not any(town.moves["verbs"][m.verb]["effort"] in ("high", "medium") for m in menu)
    d = impulses.rest_drive(town, e.world, -40.0, menu, "clock_tower", "s")
    assert d["drive"] == "energy" and d["move"].target_id == "nook"              # one step to its nest
    e.state["area"] = "nook"
    d = impulses.rest_drive(town, e.world, -40.0, e._menu(), "nook", "s")
    assert town.moves["verbs"][d["move"].verb]["does"] == "sleep"
    assert impulses.rest_drive(town, e.world, 20.0, e._menu(), "nook", "s") is None


def test_an_empty_need_ends_what_it_was_doing_and_blocks_new_work(town):
    e = engine(town)
    act = {"label": "x", "remaining": 40, "total": 60, "empty_at_start": ["joy"], "low_at_start": ["joy"]}
    st = {"needs": {**{k: 80.0 for k in e.state["needs"]}, "joy": 5.0}}
    assert e._interrupt_reason(act, [], st) == "need"                            # empty, even if it already was
    e.state.update(area="studio", needs=st["needs"])
    assert not any(m.verb in ("start_drawing", "work_on") for m in e._menu())


def test_mood_changes_what_is_on_offer_and_says_so(town):
    menu = M.offered(town, area_id="garden", here=[{"id": "yarn", "name": "yarn", "tags": ["toy", "movable"], "state": {}}],
                     inventory=[], projects=[], can_sleep=False)
    playful = {m.key for m in menu if town.moves["verbs"][m.verb].get("mood") == "playful"}
    town.settings["mood_menu"] = {"low": {"playful": 1.0}}
    low, dropped = M.shape_by_mood(town, list(menu), "low", "s")
    assert set(dropped) == playful and not any(m.key in playful for m in low)
    same, none = M.shape_by_mood(town, list(menu), "calm", "s")
    assert none == [] and len(same) == len(menu)


def test_dislikes_are_earned_from_what_happened_and_it_can_turn_away(town):
    e = engine(town)
    with e.conn:
        for i in range(4):                                                       # the puddle kept going badly
            e.conn.execute("INSERT INTO experiences(tick_id,sim_time,subject_kind,subject_key,subject_name,score,note)"
                           " VALUES(?,?,?,?,?,?,?)", (i, "2026-11-18T09:00", "thing", "puddle", "a deep puddle", -8, ""))
    earned = sleep.evidence(e.conn, town)
    assert earned["a deep puddle"]["valence"] < 0
    prefs = sleep.merge_preferences(town, {}, [{"subject": "sunsets", "valence": 0.9, "reason": "pretty"}], earned)
    by = {p["subject"]: p["valence"] for p in prefs}
    assert by["a deep puddle"] < 0                                               # added even though it was not named
    assert by["sunsets"] <= town.settings["experience"]["opinion_cap"] * town.settings["sleep"]["preference_blend"]
    with e.conn:
        e.conn.execute("INSERT INTO preferences(subject,valence,reason,updated_at) VALUES('a deep puddle',-0.6,'x','')")
    e.state["area"] = "alley"
    labels = [m.label for m in e._menu()]
    assert "turn away from a deep puddle" in labels and "leave a deep puddle alone" in labels


def test_the_journal_starts_from_how_it_is(town):
    needs = {k: 80.0 for k in town.character["needs"]}
    needs.update(sustenance=5.0, joy=30.0)
    user = journal.user_prompt(town, sim_time="2026-11-18T15:00", area_id="fountain", weather="clear",
                               action="look out", why="", impulse=False, result="You watched.", events=[],
                               before=needs, after=needs, mood={"word": "low", "anxiety": 60.0}, energy=-40.0,
                               self_image=[], tendencies={}, preferences=[], projects=[], memories=[])
    assert "Right now you are: starving, glum, exhausted, on edge." in user
    assert town.character["needs"]["joy"]["expressions"]["low"].strip() in user  # every low need, not only changes
    assert "comes before anything else" in user
    words, severe = brain.state_words(town, {k: 80.0 for k in needs}, 30.0, {"anxiety": 0.0})
    assert words == [] and not severe

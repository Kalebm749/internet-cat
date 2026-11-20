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


def test_a_nap_rests_but_the_nights_work_waits_for_night_or_a_long_gap(town):
    e = engine(town)
    from cat import db
    assert e._cycle_due("2026-11-18T13:00")                                     # never had one: due
    db.set_meta(e.conn, "last_cycle_sim_time", "2026-11-18T02:00")
    assert not e._cycle_due("2026-11-18T13:00")                                 # an afternoon nap
    assert e._cycle_due("2026-11-18T22:00")                                     # night
    assert e._cycle_due("2026-11-19T13:00")                                     # a day gone by without one


def test_properties_are_hidden_until_found_and_its_record_is_shown_on_the_menu(town):
    e = engine(town)
    yarn = dict(e.world.objects["yarn"])
    assert "toy" not in brain._obj(yarn, e._known())                            # not known yet
    mv = M.Move("m1", "examine:yarn", "examine", "yarn", yarn["name"], "examine a loose ball of yarn")
    learned = e._learned(mv, {"target": yarn}, "2026-11-18T09:00")
    assert {k["tag"] for k in learned} == set(yarn["tags"])                     # looking closely shows all
    with e.conn:
        for k in learned:
            e.conn.execute("INSERT INTO known_properties(name,tag,learned_sim_time,how) VALUES(?,?,?,?)",
                           (k["name"], k["tag"], "2026-11-18T09:00", k["how"]))
        for i in range(3):                                                       # playing with it went badly
            e.conn.execute("INSERT INTO experiences(tick_id,sim_time,subject_kind,subject_key,subject_name,score,"
                           "note,needs_score) VALUES(?,?,?,?,?,?,?,?)", (i, "s", "move", "play:yarn", "x", -4, "", -4))
    assert "toy" in brain._obj(yarn, e._known())
    play = M.Move("m2", "play:yarn", "play", "yarn", yarn["name"], "play with a loose ball of yarn")
    assert e._history_notes([play])["play:yarn"] == "it went badly 3 of the last 3 times"
    assert e._expected("play:yarn") == -4


def test_what_it_did_before_hidden_properties_still_counts(town):
    e = engine(town)
    from cat import db
    with e.conn:
        e.conn.execute("INSERT INTO ticks(kind,sim_time,real_time,area,move_key) VALUES('tick','s','r','garden','play:yarn')")
        db.set_meta(e.conn, "knowledge_backfilled", "")
    e._backfill_knowledge()
    assert e._known()["a loose ball of yarn"] == {"toy"}                         # what playing needed, not all of it


def test_surprise_and_strange_or_disliked_places_stir_it(town):
    e = engine(town)
    needs = {k: 50.0 for k in e.state["needs"]}
    worse = e._surprise(10.0, needs, {**needs, "joy": 60.0})                    # expected +10, got -10
    assert worse["worse"] and worse["anxiety"] > 0 and worse["mood"] == 0
    better = e._surprise(0.0, {**needs, "joy": 70.0}, needs)
    assert not better["worse"] and better["mood"] > 0
    assert e._surprise(0.0, needs, needs) is None and e._surprise(None, needs, needs) is None
    e.state["area"] = "alley"
    calm = e._anxiety_extra()
    assert calm["unfamiliar"] > 0                                                # never been here
    with e.conn:
        e.conn.execute("INSERT INTO preferences(subject,valence,reason,updated_at) VALUES('a deep puddle',-0.6,'x','')")
    assert e._anxiety_extra()["disliked"] > 0
    from cat import character
    assert character.anxiety_next(0, needs, 30.0, 600, town, extra=20) > character.anxiety_next(0, needs, 30.0, 600, town)

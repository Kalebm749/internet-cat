"""Closed moves: the menu, choosing from it, resolving by code, and validating moves.yaml."""
import copy
import random

import pytest
import yaml

from cat import db, moves as M
from cat.config import ConfigError, load_config
from cat.world import WorldState


def menu(cfg, conn, area="living_room", inv=None, projects=(), can_sleep=False):
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    return M.offered(cfg, area_id=area, here=w.here(area), inventory=inv or [], projects=list(projects),
                     can_sleep=can_sleep), w


def keys(m):
    return [x.key for x in m]


# ---------- the menu ----------
def test_the_menu_lists_what_is_possible_here_in_a_fixed_order(cfg, conn):
    m, _ = menu(cfg, conn)
    assert keys(m)[:2] == ["go:kitchen", "go:hallway"]                         # exits first (file order)
    assert "rest_on:sofa" in keys(m) and "rest_on:cushion" in keys(m)           # soft/warm things
    assert "climb:sofa" in keys(m) and "climb:window" in keys(m)
    assert "examine:books" in keys(m) and "take:cushion" in keys(m)
    assert "look_out" in keys(m) and "wait" in keys(m)
    assert keys(m) == keys(menu(cfg, conn)[0])                                 # stable across builds
    assert [x.id for x in m] == [f"m{i}" for i in range(1, len(m) + 1)]


def test_moves_that_do_not_apply_are_not_offered(cfg, conn):
    m, _ = menu(cfg, conn)
    assert not any(k.startswith(("eat", "play", "drop", "work_on", "sleep")) for k in keys(m))   # no food, toy, carried, project
    k, w = menu(cfg, conn, area="kitchen")
    assert "eat:bowl" in keys(k) and not any(x.startswith("rest_on") for x in keys(k))


def test_carried_objects_can_be_dropped_and_still_used(cfg, conn):
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    ball = next(o for o in w.objects.values() if o["id"] == "ball")
    m = M.offered(cfg, area_id="hallway", here=[], inventory=[ball], projects=[], can_sleep=False)
    assert "drop:ball" in keys(m) and "play:ball" in keys(m) and "examine:ball" in keys(m)
    assert not any(x.startswith("take") for x in keys(m))


def test_sleep_is_offered_only_when_allowed(cfg, conn):
    assert "sleep" not in keys(menu(cfg, conn)[0])
    m, _ = menu(cfg, conn, can_sleep=True)
    assert keys(m)[-1] == "sleep" and m[-1].label == "go to sleep"


def test_active_projects_are_offered_to_work_on(cfg, conn):
    m, _ = menu(cfg, conn, projects=[{"id": 3, "title": "drawing the old tree"}])
    wk = [x for x in m if x.verb == "work_on"]
    assert [(x.key, x.label) for x in wk] == [("work_on:3", "work on drawing the old tree")]


def test_the_menu_is_cut_to_max_offered_keeping_earlier_verbs(cfg, conn):
    cfg.settings["moves"]["max_offered"] = 4
    m, _ = menu(cfg, conn)
    assert len(m) == 4 and keys(m)[:2] == ["go:kitchen", "go:hallway"]


def test_the_menu_never_depends_on_needs(cfg, conn):
    import inspect
    assert "needs" not in inspect.signature(M.offered).parameters           # nothing to rank or mark by need


# ---------- choosing ----------
def test_choose_accepts_the_id_the_key_or_a_number_and_nothing_else(cfg, conn):
    m, _ = menu(cfg, conn)
    assert M.choose(m, "m2") is m[1] and M.choose(m, " M2 ") is m[1]
    assert M.choose(m, "go:kitchen") is m[0] and M.choose(m, "1") is m[0]
    for bad in (None, "", "m999", "0", "eat:bowl", "go:the_moon", "tidy up", 7.5):
        assert M.choose(m, bad) is None


# ---------- resolving ----------
def rs(cfg, conn, key, tags=(), novel=False, seed=1, area="living_room", inv=None):
    m, _ = menu(cfg, conn, area=area, inv=inv)
    mv = next(x for x in m if x.key == key)
    return M.resolve(cfg, mv, target_tags=list(tags), rng=random.Random(seed), novel=novel), mv


def test_resolution_is_decided_by_code(cfg, conn):
    r, mv = rs(cfg, conn, "rest_on:sofa", ["soft", "warm", "climbable"])
    assert r["gains"] == {"peace": "++"} and r["effort"] == "low" and 20 <= r["duration"] <= 60
    assert r["changes"] == [] and r["template"] == "You rested on the sofa."


def test_the_same_seed_gives_the_same_duration(cfg, conn):
    assert rs(cfg, conn, "wait", seed=5)[0]["duration"] == rs(cfg, conn, "wait", seed=5)[0]["duration"]
    assert len({rs(cfg, conn, "wait", seed=s)[0]["duration"] for s in range(30)}) > 3     # and it varies by seed


def test_tag_effects_merge_to_the_largest_bucket_never_stacking(cfg, conn):
    r, _ = rs(cfg, conn, "examine:button", ["shiny", "movable"], area="hallway")
    assert r["gains"] == {"curiosity": "+", "wonder": "+"}
    cfg.moves["verbs"]["examine"]["by_tag"]["shiny"] = {"curiosity": "++"}
    r, _ = rs(cfg, conn, "examine:button", ["shiny"], area="hallway")
    assert r["gains"]["curiosity"] == "++"                                         # the larger one wins, not "+" + "++"


def test_novelty_only_applies_to_going_somewhere_new(cfg, conn):
    assert rs(cfg, conn, "go:kitchen", novel=False)[0]["gains"] == {}
    assert rs(cfg, conn, "go:kitchen", novel=True)[0]["gains"] == {"curiosity": "+", "wonder": "+"}


def test_world_changes_come_from_the_verb_and_use_the_validated_ops(cfg, conn):
    assert rs(cfg, conn, "go:kitchen")[0]["changes"] == [{"op": "move_self", "area": "kitchen"}]
    assert rs(cfg, conn, "take:cushion")[0]["changes"] == [{"op": "take", "object_id": "cushion"}]
    r, _ = rs(cfg, conn, "eat:bowl", ["edible"], area="kitchen")
    assert r["changes"] == [{"op": "consume", "object_id": "bowl"}] and r["gains"] == {"sustenance": "++"}


def test_only_eating_can_feed_sustenance(cfg, conn):
    for key, area in (("examine:books", "living_room"), ("wait", "living_room"), ("look_out", "living_room")):
        assert "sustenance" not in rs(cfg, conn, key, area=area)[0]["gains"]


def test_every_move_in_the_placeholder_world_resolves_and_stays_in_bounds(cfg, conn):
    w = WorldState.seed(conn, cfg, "2026-11-18T19:40")
    for area in cfg.world["areas"]:
        for mv in M.offered(cfg, area_id=area, here=w.here(area), inventory=[], projects=[{"id": 1, "title": "t"}],
                            can_sleep=True):
            tags = next((o["tags"] for o in w.objects.values() if o["id"] == mv.target_id), [])
            r = M.resolve(cfg, mv, target_tags=tags, rng=random.Random(0), novel=True)
            assert r["duration"] <= cfg.settings["sim_minutes_max"] and r["effort"] in cfg.settings["effort_energy_cost"]
            assert all(n in cfg.character["needs"] and b in cfg.settings["gain_values"] for n, b in r["gains"].items())


# ---------- validating moves.yaml ----------
def edited(tmp_path, fn):
    import shutil
    from cat.config import CONFIG_DIR
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d)
    p = d / "moves.yaml"
    data = yaml.safe_load(p.read_text())
    fn(data)
    p.write_text(yaml.safe_dump(data))
    return d


def problems(tmp_path, fn):
    with pytest.raises(ConfigError) as e:
        load_config(edited(tmp_path, fn))
    return str(e.value)


def test_the_default_moves_validate():
    assert load_config().moves["verbs"]["go"]["target"] == "area"


@pytest.mark.parametrize("fn,expect", [
    (lambda m: m["verbs"]["eat"]["effects"].update(hunger="++"), "unknown need 'hunger'"),
    (lambda m: m["verbs"]["play"]["effects"].update(joy="+++"), "bucket '+++' is not in gain_values"),
    (lambda m: m["verbs"]["wait"]["effects"].update(sustenance="+"), "only a verb with does: consume may feed 'sustenance'"),
    (lambda m: m["verbs"]["examine"]["by_tag"]["shiny"].update(sustenance="+"), "only a verb with does: consume"),
    (lambda m: m["verbs"]["wait"].update(effort="huge"), "effort must be one of"),
    (lambda m: m["verbs"]["wait"].update(minutes=[30, 10]), "minutes must be [min, max]"),
    (lambda m: m["verbs"]["wait"].update(minutes=[10, 9999]), "above settings sim_minutes_max"),
    (lambda m: m["verbs"]["wait"].update(target="planet"), "target must be one of"),
    (lambda m: m["verbs"]["wait"].update(does="explode"), "does must be one of"),
    (lambda m: m["verbs"]["go"].update(does="take"), "does: take needs target: object"),
    (lambda m: m["verbs"]["wait"].update(label="  "), "label must be non-empty text"),
    (lambda m: m["verbs"]["wait"].update(label="wait {where}"), "label may only use {target}"),
    (lambda m: m["verbs"]["wait"].update(outcome=""), "outcome must be non-empty text"),
    (lambda m: m["verbs"]["wait"].update(sparkle=True), "unknown field 'sparkle'"),
    (lambda m: m["verbs"]["sleep"].pop("only_when"), "must set only_when: can_sleep"),
    (lambda m: m["verbs"]["go"].update(any_tags=["x"]), "any_tags only makes sense for target: object"),
    (lambda m: m["verbs"]["wait"].update(novelty={"joy": "+"}), "novelty only makes sense for target: area"),
])
def test_moves_yaml_is_validated(tmp_path, fn, expect):
    assert expect in problems(tmp_path, fn)


def test_there_must_always_be_something_to_do(tmp_path):
    def strip(m):
        m["verbs"] = {k: v for k, v in m["verbs"].items() if v["target"] != "none" or v["does"] != "none"}
    assert "always something to do" in problems(tmp_path, strip)


def test_every_problem_in_moves_yaml_is_reported_at_once(tmp_path):
    def bad(m):
        m["verbs"]["wait"].update(effort="huge", minutes=[3, 1])
        m["verbs"]["eat"]["effects"].update(hunger="++")
    msg = problems(tmp_path, bad)
    assert msg.startswith("3 config problems")


def test_moves_settings_are_validated(tmp_path):
    from tests.test_config import _problems
    assert "moves.max_offered: must be an integer >= 1" in _problems(
        tmp_path, "settings.yaml", lambda s: s["moves"].update(max_offered=0))
    assert "moves.narrate: must be true or false" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["moves"].update(narrate="yes"))


def test_unknown_top_level_moves_field_is_a_warning(tmp_path, caplog):
    d = edited(tmp_path, lambda m: m.update(extras=1))
    with caplog.at_level("WARNING", logger="cat.config"):
        load_config(d)
    assert "moves.yaml: unknown top-level field 'extras'" in caplog.text

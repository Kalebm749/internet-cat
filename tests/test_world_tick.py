"""World tick for needs: per-need decay, event and dislike drains, repetition by place, stale flag."""
import random

import pytest

from cat import character, db
from cat.engine import Engine
from cat.llm import MockBackend
from tests.helpers import pick


def make(cfg, seed=1, events=None):
    """An engine whose brain always does the quiet 'wait' move, set to take exactly 60 minutes and feed
    nothing, so a test sees only decay, drains and whatever it adds."""
    if events is not None:
        cfg.world["events"] = events
    cfg.moves["verbs"]["wait"].update(effects={}, minutes=[60, 60])
    cfg.settings["director"]["enabled"] = False
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.handlers["brain"] = lambda m, s: pick(m)
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok"}
    e = Engine(cfg, b, rng=random.Random(seed))
    e.world.events = None
    return e, b


# ---------- decay and drains ----------
def test_each_need_decays_per_sim_hour_and_is_clamped(cfg):
    e, b = make(cfg, events=[])
    before = dict(e.state["needs"])
    e.tick()                                            # the narrator says 60 minutes
    for k, spec in cfg.character["needs"].items():
        assert e.state["needs"][k] == pytest.approx(max(0, before[k] - spec["decay_per_hour"]))
    e.state["needs"] = {k: 0.5 for k in before}
    e.tick()
    assert all(v == 0 for v in e.state["needs"].values())      # clamped at 0, never negative


def test_event_drains_apply_when_the_event_fires(cfg):
    e, b = make(cfg, events=[{"text": "A loud noise.", "chance": 1.0, "drains": {"peace": 20}}])
    before = dict(e.state["needs"])
    e.tick()
    decay = cfg.character["needs"]["peace"]["decay_per_hour"]
    assert e.state["needs"]["peace"] == pytest.approx(before["peace"] - decay - 20)
    other = cfg.character["needs"]["joy"]["decay_per_hour"]
    assert e.state["needs"]["joy"] == pytest.approx(before["joy"] - other)           # untouched by it


def test_event_drain_is_clamped(cfg):
    e, b = make(cfg, events=[{"text": "Awful.", "chance": 1.0, "drains": {"peace": 500}}])
    e.tick()
    assert e.state["needs"]["peace"] == 0


def test_an_event_that_does_not_fire_drains_nothing(cfg):
    e, _ = make(cfg, events=[{"text": "Never.", "chance": 0.0, "drains": {"peace": 500}}])
    e.tick()
    assert e.state["needs"]["peace"] > 0


def test_event_drains_are_traced_in_need_math(cfg):
    e, b = make(cfg, events=[{"text": "A loud noise.", "chance": 1.0, "drains": {"peace": 20}}])
    e.tick()
    rows = {r["need"]: r for r in e.conn.execute("SELECT * FROM need_math")}
    assert rows["peace"]["event_drain"] == 20 and rows["joy"]["event_drain"] == 0
    r = rows["peace"]
    assert r["after"] == pytest.approx(r["before"] - r["decay"] - r["event_drain"] + r["clamp_adjust"])


def test_the_narrator_cannot_drain_or_feed(cfg):
    e, b = make(cfg, events=[])
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok", "gains": {"joy": "-", "peace": "--5"}}   # ignored
    before = dict(e.state["needs"])
    e.tick()
    for k in ("joy", "peace"):
        assert e.state["needs"][k] == pytest.approx(before[k] - cfg.character["needs"][k]["decay_per_hour"])


def test_dislikes_drain_while_the_disliked_thing_is_present(cfg):
    ds = character.dislike_drains
    d, hits = ds([{"subject": "rain", "valence": -1.0}], ["the living room", "rain"], 60, cfg)
    assert d == {"peace": 4.0, "joy": 4.0} and hits[0]["subject"] == "rain"
    assert ds([{"subject": "rain", "valence": -1.0}], ["the living room", "clear"], 60, cfg) == ({}, [])
    assert ds([{"subject": "rain", "valence": 0.8}], ["rain"], 60, cfg) == ({}, [])          # a like never drains
    half, _ = ds([{"subject": "Rain", "valence": -0.5}], ["heavy RAIN today"], 30, cfg)
    assert half == {"peace": 1.0, "joy": 1.0}                                                 # scaled by strength and time
    many = [{"subject": s, "valence": -v} for s, v in (("a", 0.2), ("b", 0.9), ("c", 0.5))]
    _, hits = ds(many, ["a b c"], 60, cfg)
    assert [h["subject"] for h in hits] == ["b", "c"]                                         # max_matches strongest


def test_dislike_drain_in_the_engine(cfg):
    e, b = make(cfg, events=[])
    e.conn.execute("INSERT INTO preferences(subject,valence,reason,updated_at) VALUES('the living room',-1.0,'','x')")
    before = dict(e.state["needs"])
    e.tick()
    decay = cfg.character["needs"]["peace"]["decay_per_hour"]
    assert e.state["needs"]["peace"] == pytest.approx(before["peace"] - decay - 4)
    assert e.state["needs"]["curiosity"] == pytest.approx(
        before["curiosity"] - cfg.character["needs"]["curiosity"]["decay_per_hour"])
    row = e.conn.execute("SELECT dislike_drain FROM need_math WHERE need='peace'").fetchone()
    assert row["dislike_drain"] == 4


# ---------- repetition ----------
def test_repetition_counts_the_same_move_in_the_same_place(cfg):
    e, b = make(cfg, events=[])
    mult = cfg.settings["repetition_penalty"]["multiplier"]
    assert e._repetition_multiplier("look_out", "living_room") == 1.0                # nothing yet
    e.tick()                                                                         # waits in the living room
    assert e._repetition_multiplier("wait", "living_room") == mult                   # the same move, same place
    assert e._repetition_multiplier("wait", "kitchen") == 1.0                        # a different place: fresh
    assert e._repetition_multiplier("look_out", "living_room") == 1.0                # a different move: fresh


def test_repetition_is_remembered_across_a_restart(cfg):
    e, b = make(cfg, events=[])
    e.tick()
    e2 = Engine(cfg, b, rng=random.Random(1))
    assert e2._repetition_multiplier("wait", "living_room") == cfg.settings["repetition_penalty"]["multiplier"]


def test_repetition_only_looks_back_a_window(cfg):
    cfg.settings["repetition_penalty"]["window"] = 2
    e, b = make(cfg, events=[])
    e.tick()
    b.handlers["brain"] = lambda m, s: pick(m, "look out and watch the world for a while")
    e.tick()
    e.tick()
    assert e._repetition_multiplier("wait", "living_room") == 1.0                    # the wait fell out of the window


def test_repetition_only_shrinks_the_listed_needs(cfg):
    e, b = make(cfg, events=[])
    cfg.moves["verbs"]["look_out"]["effects"] = {"wonder": "++", "joy": "++"}
    cfg.moves["verbs"]["look_out"]["minutes"] = [10, 10]
    b.handlers["brain"] = lambda m, s: pick(m, "look out and watch the world for a while")
    e.state["needs"].update(wonder=10.0, joy=10.0)
    e.tick()
    w1, j1 = e.state["needs"]["wonder"], e.state["needs"]["joy"]
    e.tick()                                                                         # the same move, same place
    w2, j2 = e.state["needs"]["wonder"], e.state["needs"]["joy"]
    assert (w2 - w1) < 15 and (j2 - j1) > 25
    assert cfg.settings["repetition_penalty"]["needs"] == ["wonder", "curiosity"]


# ---------- stale flag ----------
def test_stale_flag_after_enough_ticks_in_one_area_and_a_move_resets_it(cfg):
    cfg.settings["stale_after_ticks"] = 3
    e, b = make(cfg, events=[])
    flags = []
    for _ in range(4):
        flags.append(e._stale_here())
        e.tick()
    assert flags == [False, False, False, True] and e._ticks_in_area() == 4
    b.handlers["brain"] = lambda m, s: pick(m, "go to the kitchen")
    e.tick()
    assert e.state["area"] == "kitchen" and e._ticks_in_area() == 0 and not e._stale_here()


def test_stale_flag_changes_no_need_values(cfg, tmp_path):
    def run(n):
        cfg.settings["stale_after_ticks"] = n
        cfg.settings["db_path"] = str(tmp_path / f"run{n}.db")
        e, _ = make(cfg, seed=5, events=[])
        for _ in range(6):
            e.tick()
        return e.state["needs"], e.state["energy"]
    assert run(1) == run(10_000)                                                      # stale from tick 1 vs never


def test_stale_after_zero_turns_it_off(cfg):
    cfg.settings["stale_after_ticks"] = 0
    e, _ = make(cfg, events=[])
    for _ in range(5):
        e.tick()
    assert not e._stale_here()


def test_stale_flag_reaches_the_brain_context_and_survives_a_restart(cfg):
    cfg.settings["stale_after_ticks"] = 2
    e, b = make(cfg, events=[])
    for _ in range(2):
        e.tick()
    assert e._brain_context([]).stale_here is True
    e2 = Engine(cfg, b, rng=random.Random(1))
    assert e2._ticks_in_area() == 2 and e2._brain_context([]).stale_here is True

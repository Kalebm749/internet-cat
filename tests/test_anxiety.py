"""Anxiety: the pressure low needs and tiredness put on it; it lowers mood, presses on low needs, shows as a
word, and under enough of it an impulse can take over a decision (seeded, traced, only from the menu)."""
import random

from fastapi.testclient import TestClient

from cat import character as ch, impulses, moves as M
from cat.engine import Engine
from cat.llm import MockBackend
from tests.helpers import pick


def on(cfg):
    cfg.settings["anxiety"]["enabled"] = True
    return cfg


def needs(cfg, **kw):
    n = {k: 80.0 for k in cfg.character["needs"]}
    n.update(kw)
    return n


def test_anxiety_climbs_with_empty_needs_and_eases_once_they_are_fed(cfg):
    on(cfg)
    bad = needs(cfg, **{k: 2.0 for k in cfg.character["needs"]})
    a = 10.0
    for _ in range(6):
        a = ch.anxiety_next(a, bad, 30, 60, cfg)
    assert a > 40
    for _ in range(12):
        a = ch.anxiety_next(a, needs(cfg), 30, 60, cfg)
    assert a < 10 and a >= 0


def test_tiredness_adds_to_the_target_and_sleep_eases_faster(cfg):
    on(cfg)
    assert ch.anxiety_target(needs(cfg), -40, cfg) > ch.anxiety_target(needs(cfg), 30, cfg)
    awake = ch.anxiety_next(60.0, needs(cfg), 30, 60, cfg)
    asleep = ch.anxiety_next(60.0, needs(cfg), 30, 60, cfg, asleep=True)
    assert asleep < awake < 60


def test_anxiety_lowers_mood_and_names_itself_in_words(cfg):
    on(cfg)
    calm = ch.compute_mood(needs(cfg), 0, None, 30, cfg, anxiety=0)
    tense = ch.compute_mood(needs(cfg), 0, None, 30, cfg, anxiety=80)
    assert tense["value"] < calm["value"] and tense["anxiety"] == 80
    assert ch.anxiety_word(10, cfg) == "" and ch.anxiety_word(40, cfg) == "uneasy" and ch.anxiety_word(60, cfg) == "on edge" and ch.anxiety_word(90, cfg) == "frantic"


def test_anxiety_presses_only_on_needs_that_are_already_low(cfg):
    on(cfg)
    n = needs(cfg, joy=10.0)
    calm = ch.decay(n, 60, cfg)
    tense = ch.decay(n, 60, cfg, anxiety=100)
    assert tense["joy"] < calm["joy"]
    assert tense["peace"] == calm["peace"]                       # a need that is fine is not pressed
    assert ch.decay_factor(10.0, cfg, 100) == 1 + cfg.settings["anxiety"]["decay_pressure"]   # capped


def test_off_means_nothing_changes(cfg):
    cfg.settings["anxiety"]["enabled"] = False
    assert ch.anxiety_next(50, needs(cfg, joy=1.0), -40, 60, cfg) == 0.0
    assert ch.decay_factor(1.0, cfg, 100) == 1.0
    assert ch.anxiety_word(99, cfg) == ""


def test_the_prompt_says_how_anxious_it_feels(cfg, conn):
    from cat import brain
    from tests.test_prompts import ctx
    on(cfg)
    c = ctx(cfg, conn)
    c.mood = {**c.mood, "anxiety": 60.0}
    p = brain.situation_prompt(cfg, c)
    assert "Your thoughts keep circling, braced for something." in p
    assert "on edge" not in p and "You feel" not in p        # an impression, never a named state or "you feel"
    c.mood = {**c.mood, "anxiety": 5.0}
    assert "keep circling" not in brain.situation_prompt(cfg, c)


def _menu(cfg):
    here = [{"id": "o1", "name": "a toy", "tags": ["toy"], "state": {}}]
    return M.offered(cfg, area_id="living_room", here=here, inventory=[], projects=[], can_sleep=False)


def test_no_impulse_below_the_threshold_and_never_off_the_menu(cfg):
    on(cfg)
    menu = _menu(cfg)
    assert impulses.take_over(cfg, 40, menu, "x") is None
    hits = [impulses.take_over(cfg, 100, menu, f"s{i}") for i in range(60)]
    got = [h for h in hits if h]
    assert 10 < len(got) < 50                                    # about the configured 50% at full anxiety
    assert all(h["move"] in menu for h in got) and {h["impulse"] for h in got} <= {"restless", "freeze"}
    assert impulses.take_over(cfg, 100, menu, "s3") == impulses.take_over(cfg, 100, menu, "s3")   # seeded


def test_an_impulse_takes_over_the_decision_and_is_traced(cfg):
    on(cfg)
    cfg.settings["anxiety"].update(impulse_above=0, impulse_max_chance=1.0, start=100)
    cfg.settings["anxiety"]["impulses"] = {"freeze": {"weight": 1, "verbs": ["wait"]}}
    cfg.settings["activity"]["enabled"] = False
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.handlers["brain"] = lambda m, s: pick(m, "look out and watch the world for a while", why="curious", entry="I looked out.")
    e = Engine(cfg, b, rng=random.Random(1))
    e.tick()
    row = e.conn.execute("SELECT action, intent, entry FROM ticks WHERE kind='tick'").fetchone()
    assert row["action"] == "sit quietly and do nothing for a while" and "impulse (freeze)" in row["intent"]
    assert "held still" in row["entry"]
    import json
    mi = json.loads(e.conn.execute("SELECT inputs FROM mood_math").fetchone()["inputs"])
    assert mi["impulse"]["impulse"] == "freeze" and "anxiety" in mi


def test_the_website_shows_an_anxiety_bar_between_mood_and_energy(cfg):
    from cat.web.app import create_app
    on(cfg)
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    Engine(cfg, b, rng=random.Random(1)).tick()
    c = TestClient(create_app(cfg))
    st = c.get("/api/state").json()["anxiety"]
    assert st["enabled"] and 0 <= st["value"] <= 100 and st["word"]
    html = c.get("/").text
    assert 'id="anxiety-bar"' in html and html.index("anxiety-bar") < html.index("energy-bar")


def test_anxiety_settings_are_validated(tmp_path):
    from tests.test_config import _problems
    assert "anxiety.impulse_max_chance: must be 0..1" in _problems(
        tmp_path, "settings.yaml", lambda s: s["anxiety"].update(impulse_max_chance=2))
    assert "anxiety.impulses.restless: needs a weight > 0" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["anxiety"]["impulses"]["restless"].update(weight=0))

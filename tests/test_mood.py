"""Mood is two numbers, valence and arousal, each -50..50, computed from need bands."""
import pytest

from cat import character as ch

LOW = {"sustenance": 60, "wonder": 60, "joy": 60, "peace": 60, "curiosity": 60}     # all 'okay'


def mood(cfg, needs=None, nudge=0.0, feeling=None, energy=0.0, trace=None, **levels):
    n = {**LOW, **(needs or {}), **levels}
    return ch.compute_mood(n, nudge, feeling, energy, cfg, trace=trace)


def test_both_axes_are_within_minus_50_and_50_at_the_extremes(cfg):
    best = mood(cfg, {k: 100 for k in LOW}, nudge=5, feeling="positive", energy=50)
    worst = mood(cfg, {k: 0 for k in LOW}, nudge=-5, feeling="negative", energy=-50)
    for m in (best, worst):
        assert -50 <= m["valence"] <= 50 and -50 <= m["arousal"] <= 50
    assert best["valence"] == 50 and worst["valence"] == -50          # clamped, not wrapped


def test_joy_and_peace_raise_valence(cfg):
    base = mood(cfg)["valence"]
    assert mood(cfg, joy=95)["valence"] > base
    assert mood(cfg, peace=95)["valence"] > base
    assert mood(cfg, joy=5)["valence"] < base and mood(cfg, peace=5)["valence"] < base


def test_valence_follows_bands_not_raw_levels(cfg):
    assert mood(cfg, joy=50)["valence"] == mood(cfg, joy=70)["valence"]       # both 'okay'
    nb = cfg.settings["need_bands"]
    assert mood(cfg, joy=nb["low_below"] - 1)["valence"] != mood(cfg, joy=nb["low_below"])["valence"]


def test_sustenance_empty_lowers_valence(cfg):
    assert mood(cfg, sustenance=5)["valence"] < mood(cfg, sustenance=60)["valence"]
    assert mood(cfg, sustenance=95)["valence"] == mood(cfg, sustenance=60)["valence"]     # full adds nothing


def test_unlisted_needs_do_not_move_valence(cfg):
    assert mood(cfg, wonder=1)["valence"] == mood(cfg, wonder=99)["valence"]
    assert mood(cfg, curiosity=1)["valence"] == mood(cfg, curiosity=99)["valence"]


def test_curiosity_high_with_peace_low_raises_arousal(cfg):
    base = mood(cfg)["arousal"]
    both = mood(cfg, curiosity=95, peace=20)["arousal"]
    assert both == pytest.approx(base + 25)
    assert mood(cfg, curiosity=95, peace=60)["arousal"] == base            # peace not low: no rule
    assert mood(cfg, curiosity=60, peace=20)["arousal"] == base            # curiosity not high: no rule
    assert mood(cfg, curiosity=95, peace=5)["arousal"] == both             # peace empty counts too


def test_energy_moves_arousal(cfg):
    assert mood(cfg, energy=40)["arousal"] > mood(cfg, energy=-40)["arousal"]


def test_event_nudge_and_feeling_shift_valence_in_points(cfg):
    base = mood(cfg)["valence"]
    assert mood(cfg, nudge=0.5)["valence"] == pytest.approx(base + 0.5 * cfg.settings["mood"]["event_points"])
    assert mood(cfg, feeling="positive")["valence"] == pytest.approx(base + cfg.settings["mood"]["feeling_values"]["positive"])
    assert mood(cfg, feeling="negative")["valence"] < base


def test_word_and_description(cfg):
    calm = mood(cfg)
    assert calm["word"] == "calm" and calm["description"].startswith("calm and ")
    low = mood(cfg, {"joy": 5, "peace": 5, "sustenance": 5})
    assert low["word"] in ("low", "tense") and "though" in low["description"]
    happy = mood(cfg, {"joy": 95, "peace": 95})
    assert happy["word"] in ("content", "excited")


def test_description_names_the_most_pressing_need_by_band(cfg):
    m = mood(cfg, {"wonder": 30, "peace": 5})                  # peace empty outranks wonder low
    assert m["description"].endswith("though peace is empty")


def test_trace_explains_every_term(cfg):
    t = {}
    m = mood(cfg, {"joy": 95, "curiosity": 95, "peace": 20}, nudge=0.1, feeling="positive", energy=10, trace=t)
    assert t["bands"]["joy"] == "full" and t["bands"]["peace"] == "low"
    assert t["valence_terms"]["joy"] == 15 and t["valence_terms"]["peace"] == -5
    assert t["valence_unclamped"] == pytest.approx(sum(t["valence_terms"].values()) + t["event_points"] + t["feeling_points"])
    assert t["arousal_terms"]["rules"] == [{"when": {"curiosity": ["full"], "peace": ["low", "empty"]}, "add": 25}]
    assert t["arousal_unclamped"] == pytest.approx(
        t["arousal_terms"]["base"] + t["arousal_terms"]["energy"] + 25)
    assert t["range"] == [-50, 50] and m["arousal"] == pytest.approx(t["arousal_unclamped"], abs=0.06)


def test_initial_mood_uses_the_starting_energy(cfg):
    start = {k: float(v["start"]) for k, v in cfg.character["needs"].items()}
    assert ch.initial_mood(start, cfg) == ch.compute_mood(start, 0.0, None, cfg.settings["energy_start"], cfg)


def test_mood_in_the_engine_stays_in_range(cfg):
    import random
    from cat.engine import Engine
    from cat.llm import MockBackend
    b = MockBackend(seed=2)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    e = Engine(cfg, b, rng=random.Random(2))
    for _ in range(40):
        e.tick()
        m = e.state["mood"]
        assert -50 <= m["valence"] <= 50 and -50 <= m["arousal"] <= 50

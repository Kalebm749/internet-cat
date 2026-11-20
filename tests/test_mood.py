"""Mood is ONE number, a slider from -50 (negative) to 50 (positive), computed from need bands."""
import pytest

from cat import character as ch

OKAY = {"sustenance": 60, "wonder": 60, "joy": 60, "peace": 60, "curiosity": 60}


def mood(cfg, needs=None, nudge=0.0, feeling=None, energy=0.0, trace=None, **levels):
    n = {**OKAY, **(needs or {}), **levels}
    return ch.compute_mood(n, nudge, feeling, energy, cfg, trace=trace)


def test_mood_is_a_single_number_in_range(cfg):
    m = mood(cfg)
    assert set(m) == {"value", "word", "description", "event_nudge", "anxiety"}
    assert "valence" not in m and "arousal" not in m
    best = mood(cfg, {k: 100 for k in OKAY}, nudge=5, feeling="positive")
    worst = mood(cfg, {k: 0 for k in OKAY}, nudge=-5, feeling="negative")
    assert best["value"] == 50 and worst["value"] == -50                   # clamped, not wrapped


def test_joy_and_peace_raise_mood(cfg):
    base = mood(cfg)["value"]
    assert mood(cfg, joy=95)["value"] > base and mood(cfg, peace=95)["value"] > base
    assert mood(cfg, joy=5)["value"] < base and mood(cfg, peace=5)["value"] < base


def test_mood_follows_bands_not_raw_levels(cfg):
    assert mood(cfg, joy=50)["value"] == mood(cfg, joy=70)["value"]        # both 'okay'
    nb = cfg.settings["need_bands"]
    assert mood(cfg, joy=nb["low_below"] - 1)["value"] != mood(cfg, joy=nb["low_below"])["value"]


def test_sustenance_empty_lowers_mood(cfg):
    assert mood(cfg, sustenance=5)["value"] < mood(cfg, sustenance=60)["value"]
    assert mood(cfg, sustenance=95)["value"] == mood(cfg, sustenance=60)["value"]      # full adds nothing


def test_unlisted_needs_do_not_move_mood(cfg):
    assert mood(cfg, wonder=1)["value"] == mood(cfg, wonder=99)["value"]
    assert mood(cfg, curiosity=1)["value"] == mood(cfg, curiosity=99)["value"]


def test_energy_does_not_change_the_mood_number(cfg):
    assert mood(cfg, energy=-50)["value"] == mood(cfg, energy=50)["value"]
    assert "tired" in mood(cfg, energy=-10)["description"]               # only the description mentions it


def test_event_nudge_and_feeling_shift_mood_in_points(cfg):
    base = mood(cfg)["value"]
    assert mood(cfg, nudge=0.5)["value"] == pytest.approx(base + 0.5 * cfg.settings["mood"]["event_points"])
    assert mood(cfg, feeling="positive")["value"] == pytest.approx(base + cfg.settings["mood"]["feeling_values"]["positive"])
    assert mood(cfg, feeling="negative")["value"] < base


def test_three_words_from_the_threshold(cfg):
    thr = cfg.settings["mood"]["word_threshold"]
    assert mood(cfg)["word"] == "calm"
    assert mood(cfg, {"joy": 95, "peace": 95})["word"] == "content"
    assert mood(cfg, {"joy": 5, "peace": 5, "sustenance": 5})["word"] == "low"
    assert 0 < thr < 50


def test_description_names_the_most_pressing_need_by_band(cfg):
    assert mood(cfg, {"wonder": 30, "peace": 5})["description"].endswith("though peace is empty")
    assert mood(cfg)["description"].startswith("calm and ")


def test_trace_explains_every_term(cfg):
    t = {}
    m = mood(cfg, {"joy": 95, "peace": 20}, nudge=0.1, feeling="positive", trace=t)
    assert t["bands"]["joy"] == "full" and t["bands"]["peace"] == "low"
    assert t["terms"]["joy"] == 15 and t["terms"]["peace"] == -5
    assert t["unclamped"] == pytest.approx(sum(t["terms"].values()) + t["event_points"] + t["feeling_points"])
    assert t["range"] == [-50, 50] and m["value"] == pytest.approx(t["unclamped"], abs=0.06)
    assert not any("arousal" in k for k in t)


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
        assert -50 <= e.state["mood"]["value"] <= 50 and "arousal" not in e.state["mood"]

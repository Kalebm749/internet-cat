from cat import character as ch


def test_decay_scales_with_time(cfg):
    n = ch.initial_needs(cfg)
    d = ch.decay(n, 60, cfg)
    assert d["food"] == n["food"] - cfg.character["needs"]["food"]["decay"]
    assert ch.decay({k: 1 for k in n}, 600, cfg) == {k: 0 for k in n}  # clamped


def test_energy_is_weighted_average_of_needs(cfg):
    n = {k: 0.0 for k in cfg.character["needs"]}
    assert ch.energy(n, cfg) == 0
    n["rest"] = 100
    assert ch.energy(n, cfg) == 75.0   # weights rest 3, food 1
    n["food"] = 100
    assert ch.energy(n, cfg) == 100.0


def test_gains_buckets_effort_clamp(cfg):
    n = {k: 50.0 for k in cfg.character["needs"]}
    out = ch.apply_gains(n, {"fun": "++", "curiosity": "+"}, "high", cfg)
    assert out["fun"] == 80 and out["curiosity"] == 65
    assert out["rest"] == 50 - cfg.settings["effort_energy_cost"]["high"]
    assert ch.apply_gains({**n, "fun": 95}, {"fun": "++"}, "low", cfg)["fun"] == 100


def test_requires_consume_and_unknowns_ignored(cfg):
    n = {k: 50.0 for k in cfg.character["needs"]}
    assert ch.apply_gains(n, {"food": "++"}, "low", cfg)["food"] == 50
    assert ch.apply_gains(n, {"food": "++"}, "low", cfg, consumed=True)["food"] == 80
    out = ch.apply_gains(n, {"nope": "++", "fun": "???"}, "low", cfg)
    assert out["fun"] == 50 and "nope" not in out


def test_words(cfg):
    assert ch.need_word("fun", 10, cfg) == "desperate"
    assert ch.need_word("fun", 90, cfg) == "content"
    assert ch.energy_word(10, cfg) == "exhausted"
    cfg.character["needs"]["food"]["words"] = {15: "starving", 101: "fed"}
    assert ch.need_word("food", 5, cfg) == "starving"
    assert ch.need_word("food", 50, cfg) == "fed"


def test_mood_trend_outweighs_level(cfg):
    low = {k: 20.0 for k in cfg.character["needs"]}
    rising = {k: 30.0 for k in low}
    falling = {k: 10.0 for k in low}
    up = ch.compute_mood(rising, low, 0, None, cfg)
    down = ch.compute_mood(falling, {k: 30.0 for k in low}, 0, None, cfg)
    assert up["valence"] > down["valence"]
    assert -1 <= down["valence"] <= 1 and 0 <= up["arousal"] <= 1
    assert up["word"] and up["description"]


def test_mood_reaction_blended_lightly(cfg):
    n = {k: 50.0 for k in cfg.character["needs"]}
    pos = ch.compute_mood(n, n, 0, "positive", cfg)["valence"]
    neg = ch.compute_mood(n, n, 0, "negative", cfg)["valence"]
    assert 0 < pos < 0.5 and -0.5 < neg < 0


def test_consume_gives_floor_and_penalty_spares_physical(cfg):
    n = {k: 50.0 for k in cfg.character["needs"]}
    out = ch.apply_gains(n, {}, "low", cfg, consumed=True)
    assert out["food"] == 65                               # consume_min_gain "+"
    out = ch.apply_gains(n, {"food": "++", "fun": "++"}, "low", cfg, consumed=True, multiplier=0.4)
    assert out["food"] == 80 and out["fun"] == 62          # food unpenalised, fun x0.4

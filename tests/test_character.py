from cat import character as ch


def test_decay_scales_with_time(cfg):
    n = ch.initial_needs(cfg)
    d = ch.decay(n, 60, cfg)
    assert d["sustenance"] == n["sustenance"] - cfg.character["needs"]["sustenance"]["decay_per_hour"]
    assert ch.decay({k: 1 for k in n}, 600, cfg) == {k: 0 for k in n}  # clamped


def test_every_need_decays_at_its_own_rate(cfg):
    n = {k: 100.0 for k in cfg.character["needs"]}
    d = ch.decay(n, 120, cfg)
    for k, spec in cfg.character["needs"].items():
        assert d[k] == 100 - 2 * spec["decay_per_hour"]


def test_energy_is_a_separate_resource_spent_by_effort_and_restored_by_sleep(cfg):
    assert ch.spend_energy(10, "high", cfg) == 10 - cfg.settings["effort_energy_cost"]["high"]
    assert ch.spend_energy(10, None, cfg) == 10
    assert ch.spend_energy(-49, "high", cfg) == -50            # clamped at -50
    assert ch.restore_energy(45, 20) == 50                     # clamped at 50
    assert not hasattr(ch, "energy")                           # never derived from the needs


def test_gains_buckets_and_clamp(cfg):
    n = {k: 50.0 for k in cfg.character["needs"]}
    out = ch.apply_gains(n, {"joy": "++", "peace": "+"}, cfg)
    assert out["joy"] == 80 and out["peace"] == 65
    assert ch.apply_gains({**n, "joy": 95}, {"joy": "++"}, cfg)["joy"] == 100


def test_the_narrator_never_subtracts(cfg):
    n = {k: 50.0 for k in cfg.character["needs"]}
    cfg.settings["gain_values"]["--"] = -20          # even a configured negative bucket cannot drain
    assert ch.apply_gains(n, {"joy": "--"}, cfg)["joy"] == 50


def test_sustenance_only_rises_when_something_edible_is_consumed(cfg):
    n = {k: 50.0 for k in cfg.character["needs"]}
    assert ch.apply_gains(n, {"sustenance": "++"}, cfg)["sustenance"] == 50
    assert ch.apply_gains(n, {"sustenance": "++"}, cfg, consumed=True)["sustenance"] == 80
    out = ch.apply_gains(n, {"nope": "++", "joy": "???"}, cfg)
    assert out["joy"] == 50 and "nope" not in out


def test_bands_follow_the_thresholds(cfg):
    nb = cfg.settings["need_bands"]
    assert ch.need_band(nb["empty_below"] - 1, cfg) == "empty"
    assert ch.need_band(nb["empty_below"], cfg) == "low"
    assert ch.need_band(nb["low_below"] - 1, cfg) == "low"
    assert ch.need_band(nb["low_below"], cfg) == "okay"
    assert ch.need_band(nb["okay_below"], cfg) == "full"
    assert ch.need_word("joy", 5, cfg) == "empty" and ch.need_word("joy", 99, cfg) == "full"
    assert ch.energy_word(-40, cfg) == "exhausted" and ch.energy_word(40, cfg) == "wide awake"


def test_mood_trend_outweighs_level(cfg):
    low = {k: 20.0 for k in cfg.character["needs"]}
    rising = {k: 30.0 for k in low}
    falling = {k: 10.0 for k in low}
    up = ch.compute_mood(rising, low, 0, None, 0, cfg)
    down = ch.compute_mood(falling, {k: 30.0 for k in low}, 0, None, 0, cfg)
    assert up["valence"] > down["valence"]
    assert -1 <= down["valence"] <= 1 and 0 <= up["arousal"] <= 1
    assert up["word"] and up["description"]


def test_mood_reaction_blended_lightly(cfg):
    n = {k: 50.0 for k in cfg.character["needs"]}
    pos = ch.compute_mood(n, n, 0, "positive", 0, cfg)["valence"]
    neg = ch.compute_mood(n, n, 0, "negative", 0, cfg)["valence"]
    assert 0 < pos < 0.5 and -0.5 < neg < 0


def test_consume_gives_floor_and_penalty_only_hits_the_listed_needs(cfg):
    n = {k: 50.0 for k in cfg.character["needs"]}
    out = ch.apply_gains(n, {}, cfg, consumed=True)
    assert out["sustenance"] == 65                         # consume_min_gain "+"
    out = ch.apply_gains(n, {"sustenance": "++", "wonder": "++", "joy": "++"}, cfg,
                         consumed=True, multiplier=0.4)
    assert out["sustenance"] == 80                         # never penalised
    assert out["wonder"] == 62                             # in repetition_penalty.needs: x0.4
    assert out["joy"] == 80                                # not listed: unpenalised

"""The balance script runs the real engine without an LLM and reports time per need band."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("simulate_balance", Path(__file__).parent.parent / "scripts" / "simulate_balance.py")
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


def test_bands_cover_all_the_time_and_runs_are_repeatable():
    a = sim.run(None, "random", 1, 40)
    b = sim.run(None, "random", 1, 40)
    assert a == b
    for d in a["bands"].values():
        assert abs(sum(d.values()) - 1) < 1e-9
    assert a["decisions"] > 0 and 0 <= a["any_empty"] <= 1


def test_the_report_names_every_need_and_policy():
    text = sim.report("first", sim.merge([sim.run(None, "first", 1, 30)]))
    assert text.startswith("policy: first") and "some need empty" in text and "sustenance" in text


def test_the_default_balance_does_not_leave_the_creature_deprived_or_anxious():
    """The yardstick (`needy`: feeds the lowest need, sleeps when tired) must be able to keep the needs up with
    the default config. If it cannot, the decay/gain numbers are wrong and the brain would live in permanent
    deprivation, with anxiety and its impulses on all the time. (Baseline, docs/analysis-2026-11.md roadmap 1.4.)"""
    m = sim.merge([sim.run(None, "needy", seed, 400) for seed in (1, 2)])
    assert m["any_empty"] < 0.15
    assert m["anxious"] < 0.15
    r = sim.merge([sim.run(None, "random", seed, 400) for seed in (1, 2)])
    assert r["anxious"] < 0.35          # even a mindless brain is not anxious most of the time

"""What the creature perceives and remembers is what happened, not what any LLM wrote about it; and an impulse is a
real fast path. (docs/analysis-2026-11.md, roadmap items 1.1 and 1.4b.)"""
import random

from cat import character as ch
from cat.engine import Engine
from cat.llm import MockBackend
from tests.helpers import pick

INVENTED = "A silver key I had never seen glinted in the corner."


def engine(cfg, seed=1):
    b = MockBackend(seed=seed)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.handlers["journal"] = lambda m, s: {"entry": INVENTED}             # its journal makes something up
    return Engine(cfg, b, rng=random.Random(seed)), b


def test_the_diary_world_line_is_codes_account_and_the_brain_is_told_only_that(cfg):
    e, b = engine(cfg)
    prompts = []
    b.handlers["brain"] = lambda m, s: (prompts.append(m[-1]["content"]), pick(m))[1]
    e.tick()
    e.tick()
    row = e.conn.execute("SELECT outcome, result, entry FROM ticks WHERE kind='tick' ORDER BY id").fetchone()
    assert row["outcome"] == row["result"]                  # no prose layer: what the diary says happened is code's
    assert row["entry"] == INVENTED                         # the entry is its own words, shown as such
    assert row["result"] in prompts[1]                      # the brain is told what happened ...
    assert "silver key" not in prompts[1].split("## Memories")[0]   # ... never what its journal claimed


def test_after_a_restart_the_brain_still_gets_the_plain_account(cfg):
    e, b = engine(cfg)
    b.handlers["brain"] = lambda m, s: pick(m)
    e.tick()
    e2 = Engine(cfg, b, conn=e.conn, rng=random.Random(2))
    assert e2.last_outcome and "silver key" not in e2.last_outcome


def test_memories_mark_its_own_words_as_its_words(cfg):
    e, b = engine(cfg)
    b.handlers["brain"] = lambda m, s: pick(m, feeling="positive")      # a strong feeling: notable
    e.tick()
    text = e.conn.execute("SELECT text FROM memories").fetchone()["text"]
    result = e.conn.execute("SELECT result FROM ticks").fetchone()["result"]
    assert result in text and f'You wrote: "{INVENTED}"' in text      # what happened, then what it wrote


def _impulsive(cfg):
    cfg.settings["anxiety"]["enabled"] = True
    cfg.settings["anxiety"].update(impulse_above=0, impulse_max_chance=1.0, start=100)
    cfg.settings["anxiety"]["impulses"] = {"freeze": {"weight": 1, "verbs": ["wait"]}}


def test_an_impulse_skips_the_brain_call_entirely(cfg):
    _impulsive(cfg)
    e, b = engine(cfg)
    calls = []
    b.handlers["brain"] = lambda m, s: (calls.append(1), pick(m, "look out", thought="I want to look out"))[1]
    e.tick()
    row = e.conn.execute("SELECT intent, thought, reaction, result FROM ticks WHERE kind='tick'").fetchone()
    assert calls == []                                       # no brain call was made (or paid for)
    assert row["intent"].startswith("an impulse") and not row["thought"] and not row["reaction"]
    assert not e.conn.execute("SELECT 1 FROM llm_calls WHERE kind='brain'").fetchone()


def test_an_impulse_is_remembered_and_marked_in_the_day_log(cfg):
    from cat import sleep
    _impulsive(cfg)
    e, b = engine(cfg)
    e.tick()
    mem = e.conn.execute("SELECT text FROM memories WHERE kind='short'").fetchone()
    assert mem is not None                                   # acting on an impulse is a notable moment
    lines, _ = sleep.day_log(e.conn, cfg)
    assert "on an impulse" in lines[-1] and "silver key" not in lines[-1]   # the day log never carries its words


def test_anxiety_climbing_a_band_is_noticed(cfg):
    cfg.settings["anxiety"]["enabled"] = True
    assert ch.anxiety_rose(20, 30, cfg) == "uneasy"          # into the first worded band
    assert ch.anxiety_rose(30, 45, cfg) == ""                # still the same band
    assert ch.anxiety_rose(45, 60, cfg) == "on edge"
    assert ch.anxiety_rose(60, 40, cfg) == ""                # easing is not a rise
    cfg.settings["anxiety"]["enabled"] = False
    assert ch.anxiety_rose(0, 99, cfg) == ""

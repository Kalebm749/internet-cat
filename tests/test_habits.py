"""Habits: code finds the pattern, the sleeping mind words it, the brain hears it as 'you tend to...'."""
import random

from fastapi.testclient import TestClient

from cat import brain, db, habits as HB
from cat.engine import Engine
from cat.llm import MockBackend


def seed(cfg, n_sofa=6, n_other=1, part_time="2026-11-18T18:{m:02d}"):
    from cat.db import connect
    conn = connect(cfg.db_path)
    db.init_db(conn)
    i = 0
    for key, count in (("rest_on:sofa", n_sofa), ("look_out", n_other)):
        for _ in range(count):
            conn.execute("INSERT INTO ticks(kind,sim_time,real_time,area,action,move_key,needs_after,duration) "
                         "VALUES('tick',?,?,?,?,?,?,20)",
                         (part_time.format(m=i), "t", "living_room", f"do {key}", key, "{}"))
            i += 1
    conn.commit()
    return conn


def test_a_move_that_recurs_in_a_part_of_the_day_is_a_habit(cfg):
    conn = seed(cfg)
    c = HB.mine(conn, cfg)
    assert [x["key"] for x in c] == ["rest_on:sofa|evening"]
    assert c[0]["count"] == 6 and c[0]["total"] == 7 and c[0]["share"] > 0.8


def test_too_few_repeats_or_too_small_a_share_is_not_a_habit(cfg):
    assert HB.mine(seed(cfg, n_sofa=3, n_other=0), cfg) == []
    cfg.settings["db_path"] += "2"
    cfg.settings["habits"]["min_share"] = 0.9
    assert HB.mine(seed(cfg, n_sofa=5, n_other=1), cfg) == []


def test_wording_comes_from_the_model_else_the_old_line_else_the_template(cfg):
    cand = [{"key": "a|evening", "move_key": "a", "part": "evening", "label": "rest", "count": 5, "total": 6, "share": .83},
            {"key": "b|evening", "move_key": "b", "part": "evening", "label": "look out", "count": 4, "total": 6, "share": .67}]
    old = {"b|evening": {"text": "You like to watch the street.", "formed_day": "2026-11-10"}}
    out = HB.decide(cfg, old, cand, [{"id": 0, "text": "  You curl up on the sofa. "}, {"id": 9, "text": "x"}], "2026-11-18")
    assert out[0]["text"] == "You curl up on the sofa." and out[0]["formed_day"] == "2026-11-18"
    assert out[1]["text"] == "You like to watch the street." and out[1]["formed_day"] == "2026-11-10"
    out = HB.decide(cfg, {}, cand, None, "d")
    assert out[0]["text"] == "You tend to rest in the evening."


def test_a_sleep_stores_habits_and_the_brain_prompt_mentions_them(cfg):
    conn = seed(cfg)
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.handlers["habits"] = lambda m, s: {"habits": [{"id": 0, "text": "You settle on the sofa at dusk."}]}
    e = Engine(cfg, b, rng=random.Random(1))
    e.state["sim_time"] = "2026-11-18T23:00"
    e.state["energy"] = -45.0
    e.tick()
    rows = [r["text"] for r in e.conn.execute("SELECT text FROM habits")]
    assert rows == ["You settle on the sofa at dusk."]
    prompt = brain.situation_prompt(cfg, e._brain_context([]))
    assert "You settle on the sofa at dusk." in prompt


def test_habits_off_means_no_step(cfg):
    cfg.settings["habits"]["enabled"] = False
    conn = seed(cfg)
    b = MockBackend(seed=1)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    e = Engine(cfg, b, rng=random.Random(1))
    e.state["sim_time"] = "2026-11-18T23:00"
    e.state["energy"] = -45.0
    e.tick()
    assert e.conn.execute("SELECT COUNT(*) FROM habits").fetchone()[0] == 0
    assert not any(t == "habits" for t, _ in b.calls)


def test_the_site_lists_habits(cfg):
    from cat.web.app import create_app
    conn = seed(cfg)
    HB.commit(conn, [{"key": "k", "text": "You nap in the evening.", "part": "evening", "count": 5,
                      "share": .8, "formed_day": "2026-11-18"}], "2026-11-18T22:00")
    conn.commit()
    st = TestClient(create_app(cfg)).get("/api/state").json()
    assert st["habits"]["rows"][0]["text"] == "You nap in the evening."
    assert "You nap in the evening." in TestClient(create_app(cfg)).get("/").text


def test_habit_settings_are_validated(tmp_path):
    from tests.test_config import _problems
    assert "habits.min_count: must be an integer >= 1" in _problems(
        tmp_path, "settings.yaml", lambda s: s["habits"].update(min_count=0))
    assert "habits.min_share: must be a number above 0 and up to 1" in _problems(
        tmp_path / "b", "settings.yaml", lambda s: s["habits"].update(min_share=0))

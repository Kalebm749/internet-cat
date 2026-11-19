"""Dev dashboard: the brain prompt view shows which need expressions were selected, and why."""
import random
import re

import pytest
from fastapi.testclient import TestClient

from cat import db
from cat.devdash import data
from cat.devdash.app import create_app
from cat.engine import Engine
from cat.llm import MockBackend
from tests.helpers import pick


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("DEVDASH_BIND", "DEVDASH_USER", "DEVDASH_PASS"):
        monkeypatch.delenv(k, raising=False)


def make(cfg):
    b = MockBackend(seed=4)
    b.need_ids = list(cfg.character["needs"])
    b.tendency_names = list(cfg.character["tendencies"])
    b.handlers["brain"] = lambda m, s: pick(m)
    b.handlers["narrator"] = lambda m, s: {"outcome": "ok", "gains": {}, "effort": "low", "duration": 10,
                                           "changes": []}
    return Engine(cfg, b, rng=random.Random(4)), b


@pytest.fixture
def ran(cfg):
    e, b = make(cfg)
    e.state["needs"].update(sustenance=3.0, joy=30.0, peace=80.0, wonder=60.0, curiosity=60.0)
    info = e.tick()
    return e, info["id"]


def test_the_selection_and_reasons_are_stored_per_tick(cfg, ran):
    e, tid = ran
    d = data.tick_detail(cfg, tid)
    p = d["pressing"]
    assert [r["need"] for r in p["rows"]] == list(cfg.character["needs"])
    rows = {r["need"]: r for r in p["rows"]}
    assert rows["sustenance"]["selected"] and rows["sustenance"]["band"] == "empty"
    assert rows["joy"]["selected"] and rows["joy"]["band"] == "low"
    assert not rows["peace"]["selected"] and "not pressing" in rows["peace"]["reason"]
    assert p["max"] == cfg.settings["max_pressing_needs"] and p["stale_here"] is False


def test_the_page_highlights_the_expressions_that_were_in_the_prompt(cfg, ran):
    e, tid = ran
    html = TestClient(create_app(cfg)).get(f"/ticks/{tid}").text
    sus = cfg.character["needs"]["sustenance"]["expressions"]
    assert f"<mark>{sus['empty']}</mark>" in html                         # highlighted inside the real prompt
    assert 'id="pressing"' in html and "in the prompt?" in html
    assert "<mark>" + cfg.character["needs"]["peace"]["expressions"]["full"] not in html   # not selected: not marked
    assert re.search(r'<tr class="sel"><td>sustenance</td>', html)


def test_stale_text_is_highlighted_when_the_flag_was_on(cfg):
    cfg.settings["stale_after_ticks"] = 1
    e, b = make(cfg)
    e.tick()
    tid = e.tick()["id"]
    d = data.tick_detail(cfg, tid)
    assert d["pressing"]["stale_here"] is True and cfg.character["stale_here"] in d["pressing_marks"]
    html = TestClient(create_app(cfg)).get(f"/ticks/{tid}").text
    assert f"<mark>{cfg.character['stale_here']}</mark>" in html


def test_the_prompt_text_is_escaped_in_the_highlighted_view(cfg, ran):
    e, tid = ran
    cfg.character["needs"]["sustenance"]["expressions"]["empty"] = "<b>x</b>"
    from cat.devdash.app import create_app as ca
    html = TestClient(ca(cfg)).get(f"/ticks/{tid}").text
    assert "<b>x</b>" not in html


def test_need_math_has_one_row_per_need(cfg, ran):
    e, tid = ran
    d = data.tick_detail(cfg, tid)
    assert [r["need"] for r in d["need_math"]] == list(cfg.character["needs"])
    assert len(d["need_math"]) == 5


def test_ticks_without_a_brain_call_have_no_selection(cfg):
    e, b = make(cfg)
    b.online = False
    tid = e.tick()["id"]
    assert data.tick_detail(cfg, tid)["pressing"] is None

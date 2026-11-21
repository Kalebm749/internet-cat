"""Conditions (docs/rewrite-contracts.md §4.3)."""
import random

import pytest

from cat import conditions, content
from cat.conditions import Ctx, evaluate


def ctx(**kw):
    base = dict(creature={"mood": 0.0, "energy": 0.5, "hunger": 0.3},
                facts={"world.weather": "clear", "market.offerings": 2,
                       "market.mailbox": [{"id": "1", "status": "unread"}, {"id": "2", "status": "read"}]})
    base.update(kw)
    return Ctx(**base)


@pytest.mark.parametrize("cond, want", [
    ({"energy_gte": 0.5}, True), ({"energy_gte": 0.6}, False), ({"energy_lte": 0.5}, True),
    ({"hunger_gte": 0.4}, False), ({"hunger_lte": 0.3}, True),
    ({"mood_gte": -0.1}, True), ({"mood_lte": -0.1}, False),
    ({"fact_gte": ["market.offerings", 2]}, True), ({"fact_gte": ["market.offerings", 3]}, False),
    ({"fact_lte": ["market.offerings", 1]}, False), ({"fact_eq": ["world.weather", "clear"]}, True),
    ({"fact_count_gte": ["market.mailbox", 2]}, True), ({"fact_count_gte": ["market.mailbox", 3]}, False),
    ({"fact_count_where": ["market.mailbox", {"status": "unread"}, 1]}, True),
    ({"fact_count_where": ["market.mailbox", {"status": "unread"}, 2]}, False),
    ({"fact_count_where": ["market.mailbox", {"status": ["unread", "read"]}, 2]}, True),
    ({"weather_in": ["rain", "clear"]}, True), ({"weather_in": ["rain"]}, False),
    ({"any_of": [{"energy_lte": 0.1}, {"hunger_lte": 0.3}]}, True),
    ({"all_of": [{"energy_lte": 0.1}, {"hunger_lte": 0.3}]}, False),
    ({"not": {"energy_lte": 0.1}}, True),
])
def test_evaluate(cond, want):
    assert evaluate(cond, ctx()) is want


@pytest.mark.parametrize("hour, want", [(21, True), (23.9, True), (0, True), (8.9, True), (9, False), (12, False), (20, True)])
def test_hours_wrap_past_midnight(hour, want):
    assert evaluate({"hours": [20, 9]}, ctx(hour=hour)) is want


def test_hours_within_a_day():
    assert evaluate({"hours": [6, 11]}, ctx(hour=6))
    assert not evaluate({"hours": [6, 11]}, ctx(hour=11))


def test_history_conditions():
    c = ctx(known={"make_wish"}, last_done={"make_wish": 30.0})
    assert evaluate({"knows": "make_wish"}, c)
    assert not evaluate({"knows": "assess_wishes"}, c)
    assert evaluate({"done_before": "make_wish"}, c)
    assert evaluate({"not_done_since_minutes": ["make_wish", 20]}, c)
    assert not evaluate({"not_done_since_minutes": ["make_wish", 60]}, c)
    assert evaluate({"not_done_since_minutes": ["assess_wishes", 60]}, c)


def test_random_is_seeded():
    a = [evaluate({"random_lt": 0.5}, ctx(rng=random.Random(7))) for _ in range(5)]
    b = [evaluate({"random_lt": 0.5}, ctx(rng=random.Random(7))) for _ in range(5)]
    assert a == b
    rng = random.Random(1)
    hits = sum(evaluate({"random_lt": 0.3}, ctx(rng=rng)) for _ in range(2000))
    assert 500 < hits < 700


def test_validate_against_content():
    c = content.load()
    assert conditions.validate({"fact_count_where": ["market.mailbox", {"status": "read"}, 1]}, c) == []
    assert conditions.validate({"weather_in": ["snow"]}, c) == ["weather_in: unknown weather ['snow'] (known: ['clear', 'cloudy', 'rain', 'windy', 'fog'])"]
    assert "needs a list of conditions" in conditions.validate({"any_of": []}, c)[0]
    assert "one key" in conditions.validate({"energy_gte": 0.1, "hunger_lte": 0.5}, c)[0]
    assert conditions.validate({"not": {"knows": "fly"}}, c) == ["not: knows: no action 'fly'"]
    assert conditions.validate({"random_lt": 0.2}, c, "outcome") == []

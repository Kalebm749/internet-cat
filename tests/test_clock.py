from cat import clock

PH = {"dawn": 5, "day": 8, "dusk": 18, "night": 21}


def test_phases_wrap_midnight():
    p = lambda s: clock.phase(clock.parse(s), PH)
    assert p("2026-11-18T03:00") == "night"
    assert p("2026-11-18T05:00") == "dawn"
    assert p("2026-11-18T12:00") == "day"
    assert p("2026-11-18T19:40") == "dusk"
    assert p("2026-11-18T23:59") == "night"


def test_advance_crosses_day():
    assert clock.advance("2026-11-18T23:50", 20) == "2026-11-19T00:10"


def test_labels_and_day_number():
    first = clock.parse("2026-11-18T07:00")
    now = clock.parse("2026-11-30T19:40")
    assert clock.day_number(first, now) == 13
    assert clock.time_label(now) == "7:40 pm"
    assert clock.time_label(clock.parse("2026-11-30T00:05")) == "12:05 am"
    assert clock.day_label(first, now) == "Monday, Day 13"
    assert clock.part_of_day(now) == "evening"


def test_hour_in_range_wraps():
    d = clock.parse("2026-11-18T23:00")
    assert clock.hour_in_range(d, 22, 4)
    assert not clock.hour_in_range(clock.parse("2026-11-18T12:00"), 22, 4)
    assert clock.hour_in_range(clock.parse("2026-11-18T08:00"), 8, 18)
    assert not clock.hour_in_range(clock.parse("2026-11-18T18:00"), 8, 18)

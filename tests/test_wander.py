"""Moving on: where it could go, how long it has stayed, and staleness of a long stay."""
from cat import behaviour, content, store, tuning
from cat.llm import MockBackend
from cat.loop import FakeClock, Loop


def test_a_long_stay_shows_and_exits_say_what_it_knows(tmp_path):
    t = tuning.load()
    lp = Loop(store.connect(tmp_path / "nit.db"), content.load(), t, MockBackend(seed=1), clock=FakeClock(), seed=1)
    lp.life.content.events.clear()
    with store.transaction(lp.conn):
        for h in range(7, 11):                         # four hours at the fountain, reflecting
            lp.conn.execute("INSERT INTO activities(action_id, location, inputs, started_at, planned_minutes, ended_at, "
                            "status, family, freshness) VALUES ('sit_and_reflect', 'fountain', '{}', ?, 20, ?, "
                            "'finished', 'rest', 1)", (f"2026-11-21T{h:02d}:00", f"2026-11-21T{h:02d}:20"))
        lp.conn.execute("INSERT INTO activities(action_id, location, inputs, started_at, planned_minutes, ended_at, "
                        "status, family, freshness) VALUES ('eat_offering', 'market', '{}', '2026-11-21T06:00', 5, "
                        "'2026-11-21T06:05', 'finished', 'eat', 1)")
    lp.life.sim_time = "2026-11-21T11:00"
    seen = []
    lp.backend.handlers["decide"] = lambda m, s: (seen.append(m[1]["content"]),
                                                  {"thought": "hm", "reasoning": "hm", "action": "inspect", "inputs": {},
                                                   "preserve": False, "letter": None})[1]
    lp.step()
    prompt = seen[0]
    assert "You have been here for about 4 hours." in prompt
    assert '"market" = the market stall (last there 5 hours ago)' in prompt
    assert "(you have never been there)" in prompt
    c = lp.life.content
    here = behaviour.freshness(lp.conn, c, t, c.actions["make_wish"], None, lp.life.sim_time, lp.life.world.values,
                               location="fountain")
    assert here.parts["place"] > 1 and here.value < 1
    move = behaviour.freshness(lp.conn, c, t, c.actions["move"], None, lp.life.sim_time, lp.life.world.values,
                               location="fountain")
    assert "place" not in move.parts                   # moving on is never stale because of the stay

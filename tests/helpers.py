"""Test helpers for scripted brains under closed moves: read the menu out of the prompt and pick from it."""
import re

MENU = re.compile(r"^- \[(m\d+)\] (.+)$", re.M)
QUIET = "sit quietly and do nothing for a while"


def menu_of(messages) -> list[tuple[str, str]]:
    return MENU.findall(messages[-1]["content"])


def pick(messages, label=None, why="", feeling="neutral", **extra) -> dict:
    """A brain reply that chooses the move whose label equals `label` (or contains it); by default the
    quiet 'do nothing' move, so a test that does not care about the move changes nothing in the world."""
    want = (label or QUIET).lower()
    items = menu_of(messages)
    for mid, lab in items:
        if lab.lower() == want:
            break
    else:
        for mid, lab in items:
            if want in lab.lower():
                break
        else:
            raise AssertionError(f"no move like {want!r} in the menu: {[l for _, l in items]}")
    return {"reaction": extra.pop("reaction", ""), "thought": extra.pop("thought", ""), "move": mid,
            "why": why, "feeling": feeling, **extra}


def quiet_brain(**kw):
    """A brain handler that always does the quiet move (or `label`)."""
    return lambda m, s: pick(m, **kw)


def inject_rejections(e, every=7):
    """Under closed moves the world never applies an invalid change, so dashboard tests that need rejected
    changes in the data add a few synthetic ones, as the effect log would have recorded them."""
    import json
    ids = [r[0] for r in e.conn.execute("SELECT id FROM ticks WHERE kind='tick' ORDER BY id")]
    for tid in ids[::every]:
        change = {"op": "consume", "object_id": "ghost_fish"}
        e.conn.execute("UPDATE ticks SET rejected_changes=? WHERE id=?",
                       (json.dumps([{"change": change, "reason": "no such object 'ghost_fish'"}]), tid))
        e.conn.execute("INSERT INTO effect_logs(tick_id,op,args,accepted,reason) VALUES(?,?,?,0,?)",
                       (tid, "consume", json.dumps(change), "no such object 'ghost_fish'"))

"""The website, visitor mail and the dev dashboard (docs/rewrite-contracts.md §4.8, §8), on a short mock life."""
import base64
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cat import content, mail, store, tuning
from cat.config import ConfigError, load_config, validate
from cat.llm import MockBackend
from cat.loop import FakeClock, Loop


def live(cfg, hours=6.0, letters=()):
    """A mock life in cfg's database; letters (sender, text) are posted to mail.db first."""
    if letters:
        m = mail.open_for_writing(cfg.mail_db_path)
        for sender, text in letters:
            assert mail.submit(m, cfg.settings["web"], sender, text, "visitor", secret="passphrase").ok
        m.close()
    t = tuning.load()
    fc = FakeClock()
    lp = Loop(store.connect(cfg.db_path), content.load(), t, MockBackend(seed=2), clock=fc, seed=2, cfg=cfg)
    lp.run(int(hours * 3600 / (t["time_scale"] * t["loop"]["step_seconds"])), sleep=fc.sleep)
    return lp


# ---------------------------------------------------------------- settings

def test_settings_are_validated(tmp_path):
    good = load_config().settings
    assert validate(good) == []
    bad = dict(good)
    del bad["web"]
    bad["llm_num_ctx"] = "big"
    probs = validate(bad)
    assert "settings.yaml: missing 'web'" in probs and "settings.yaml: 'llm_num_ctx' has the wrong type" in probs
    (tmp_path / "settings.yaml").write_text("ollama_host: [")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(tmp_path)


# ---------------------------------------------------------------- mail

def test_letters_are_checked_and_rate_limited(cfg):
    web = cfg.settings["web"]
    web.update(mail_per_ip_per_hour=2, mail_per_hour=3, mail_blocklist=["spam"])
    m = mail.open_for_writing(cfg.mail_db_path)
    send = lambda name, text, who="a", **k: mail.submit(m, web, name, text, who, secret=k.pop("secret", "secret1"), **k)
    assert send("Ann", "   ").code == "empty"
    assert send("Ann", "x" * (web["mail_max_chars"] + 1)).code == "long"
    assert send("Ann", "buy SPAM now").code == "blocked"
    assert send("", "no name").code == "name" and send("A b", "bad chars").code == "name"
    r = send("Ann", "Hello\x00 Nit")
    assert r.ok and tuple(m.execute("SELECT sender, text FROM letters").fetchone()) == ("Ann", "Hello Nit")
    assert send("Ann", "again").ok
    assert send("Ann", "and again").code == "slow"
    assert send("Bob", "from someone else", "b").ok
    assert send("Cyd", "one more", "c").code == "full"
    later = datetime.now() + timedelta(hours=2)
    assert send("Ann", "next hour", now=later).ok


def test_a_name_is_a_claimed_account_nobody_else_can_write_as(cfg):
    web = cfg.settings["web"]
    m = mail.open_for_writing(cfg.mail_db_path)
    assert mail.submit(m, web, "Kaleb", "first", "a", secret="short").code == "secret"        # a new name needs a passphrase
    assert mail.submit(m, web, "Kaleb", "first", "a", secret="a good phrase").ok               # this letter claims the name
    assert mail.submit(m, web, "KALEB", "faking", "b", secret="not it").code == "denied"        # unique ignoring case
    assert mail.submit(m, web, "kaleb", "mine again", "c", secret="a good phrase").ok
    rows = m.execute("SELECT sender, account_id FROM letters ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["Kaleb", "Kaleb"] and rows[0][1] == rows[1][1]              # one account, one name
    assert "a good phrase" not in " ".join(str(tuple(r)) for r in m.execute("SELECT * FROM accounts"))   # only a hash
    for i in range(8):
        mail.submit(m, web, "Kaleb", "guess", "thief", secret=f"guess{i}")
    assert mail.submit(m, web, "Kaleb", "guess", "thief", secret="a good phrase").code == "slow"   # guessing is slowed


def test_letters_reach_the_mailbox_untemplated(cfg):
    lp = live(cfg, 0.5, [("Ann", "What do you dream about? {inputs.x} {fact:market.offerings}")])
    box = lp.life.world.get("market.mailbox")
    assert box == [{"id": "L1", "sender": "Ann", "text": "What do you dream about? {inputs.x} {fact:market.offerings}",
                    "status": "unread", "received": box[0]["received"]}]
    assert lp.conn.execute("SELECT letter_id FROM mail_imports").fetchall()[0][0] == 1
    assert lp._import_mail() == 0                                     # imported once only


# ---------------------------------------------------------------- the website

@pytest.fixture
def site(cfg):
    from cat.web.app import create_app
    live(cfg, 30, [("Ann", "Will you make a wish for me?")])
    return TestClient(create_app(cfg))


def test_website_pages(site):
    r = site.get("/")
    assert r.status_code == 200 and "Write to Nit" in r.text and "Diary" in r.text and "Who Nit is" in r.text
    assert "A humanoid creature" in r.text
    d = site.get("/api/now").json()
    assert d["now"]["name"] == "Nit" and d["now"]["place"] in [l.name for l in content.load().locations.values()]
    page = site.get("/api/diary").json()
    assert page["entries"] and page["next"]
    act = next(e for e in page["entries"] if e["kind"] == "activity" and e["status"] == "finished")
    assert act["started"] <= act["time"] and act["start_clock"] == act["started"][-5:] and act["minutes"] >= 0
    assert act["name"] and len(act["decided_real"]) == 8                       # HH:MM:SS, real time of the decision
    assert f'Day {act["start_day"]}, {act["start_clock"]}</time> → {act["end_clock"]}' in site.get("/").text
    older = site.get("/api/diary", params={"before": page["next"]}).json()
    assert older["entries"] and all(e["time"] <= page["next"] for e in older["entries"])
    assert site.get("/api/now", params={"after": "9999"}).json()["entries"] == []
    assert site.get("/healthz").json()["status"] == "ok"


def test_website_letter_form(site, cfg):
    r = site.post("/letters", data={"text": "Hello", "sender": "Bob", "secret": "bo-secret"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?letter=ok#letter"
    assert "Your letter is in the mailbox" in site.get("/?letter=ok").text
    r = site.post("/letters", data={"text": "", "sender": "Bob", "secret": "bo-secret"}, follow_redirects=False)
    assert r.headers["location"] == "/?letter=empty#letter"
    n = mail.open_for_reading(cfg.mail_db_path).execute("SELECT COUNT(*) FROM letters").fetchone()[0]
    site.post("/letters", data={"text": "bot", "website": "http://spam"}, follow_redirects=False)   # honeypot
    assert mail.open_for_reading(cfg.mail_db_path).execute("SELECT COUNT(*) FROM letters").fetchone()[0] == n


def test_website_before_the_first_start_and_before_migration(cfg, tmp_path):
    from cat.web.app import create_app
    c = TestClient(create_app(cfg))
    assert c.get("/").status_code == 200 and c.get("/api/now").json() == {"now": None, "entries": []}
    old = store.connect(cfg.db_path)
    old.execute("PRAGMA user_version = 1")
    old.close()
    assert c.get("/api/now").json()["now"] is None                     # readers never read an unmigrated life


def test_the_website_never_writes_the_life(cfg):
    import pathlib
    src = pathlib.Path(__file__).parent.parent / "cat" / "web" / "app.py"
    text = src.read_text()
    assert "store.open_readonly" in text and "store.connect(" not in text and "transaction(" not in text


# ---------------------------------------------------------------- the dev dashboard

PAGES = ["/", "/decisions", "/decisions/3", "/character", "/inner", "/memory", "/knowledge", "/repetition", "/world", "/sleep",
         "/mail", "/content"]


@pytest.fixture
def dash(cfg, monkeypatch):
    monkeypatch.delenv("DEVDASH_BIND", raising=False)
    monkeypatch.delenv("DEVDASH_USER", raising=False)
    monkeypatch.delenv("DEVDASH_PASS", raising=False)
    from cat.devdash.app import create_app
    live(cfg, 30, [("Ann", "Hello there")])
    return TestClient(create_app(cfg))


def test_every_dashboard_page_and_its_json_twin(dash):
    for p in PAGES:
        assert dash.get(p).status_code == 200, p
        api = "/api/dev/overview" if p == "/" else f"/api/dev{p}"
        assert dash.get(api).status_code == 200, api
    d = dash.get("/api/dev/decisions/3").json()
    assert d["mind"]["options"] and d["messages"][0]["role"] == "system" and d["attempts"]
    assert dash.get("/decisions/999999").status_code == 404
    assert dash.get("/api/dev/content").json()["content"] == "ok"
    m = dash.get("/api/dev/mail").json()
    assert len(m["imports"]) == 1                          # imported (then read, answered, set aside or still there)


def test_dashboard_access_rules(cfg, monkeypatch):
    from cat.devdash import auth
    from cat.devdash.app import create_app
    monkeypatch.setenv("DEVDASH_BIND", "0.0.0.0")
    monkeypatch.delenv("DEVDASH_USER", raising=False)
    monkeypatch.delenv("DEVDASH_PASS", raising=False)
    with pytest.raises(auth.AccessError):
        create_app(cfg)
    monkeypatch.setenv("DEVDASH_USER", "me")
    monkeypatch.setenv("DEVDASH_PASS", "secret")
    c = TestClient(create_app(cfg))
    assert c.get("/").status_code == 401 and c.get("/healthz").status_code == 200
    ok = {"Authorization": "Basic " + base64.b64encode(b"me:secret").decode()}
    assert c.get("/", headers=ok).status_code == 200
    assert c.post("/", headers=ok).status_code == 403                  # no X-Requested-With


def test_the_llm_page_lets_you_read_each_call_s_input_and_output(dash, cfg):
    import json as _json
    call = {"id": "c1", "operation": "chat", "task": "decide", "model": "qwen3:4b", "status": "completed",
            "started_at": "2026-11-21T10:00:00", "started_ts": 1.0, "input_tokens_estimate": 10, "input_tokens": 12,
            "output_tokens": 5, "duration_ms": 900, "error": None,
            "io": {"system": "You are Nit.", "user": "It is morning <b>now</b>.", "response": '{"action": "inspect"}'}}
    path = cfg.path("data/llm-calls.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"updated_at": "x", "active": [], "recent": [call]}))
    html = dash.get("/llm").text
    assert "Read input and output" in html and "You are Nit." in html and "&#34;action&#34;: &#34;inspect&#34;" in html
    assert "<b>now</b>" not in html and "&lt;b&gt;now&lt;/b&gt;" in html                       # escaped, never markup
    assert dash.get("/api/dev/llm").json()["recent"][0]["io"]["user"].startswith("It is morning")

/* Optional end-to-end check of static/js/journal.js against a real server and database.
 *   npm i --no-save jsdom      (anywhere on NODE_PATH, or in tests/js)
 *   node tests/js/live_updates.cjs        (run from the repo root; uses .venv/bin/python)
 * It seeds a temp DB, starts uvicorn with a 1s poll, loads the page in jsdom, appends ticks with
 * scripts/add_ticks.py and asserts what the reader would see. */
const { JSDOM } = require("jsdom");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path"), assert = require("assert");

const ROOT = path.resolve(__dirname, "..", "..");
const PY = path.join(ROOT, ".venv", "bin", "python");
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cat-js-"));
const DB = path.join(tmp, "t.db"), PORT = 8097, BASE = `http://127.0.0.1:${PORT}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const py = (...args) => { const r = spawnSync(PY, args, { cwd: ROOT, encoding: "utf8" }); if (r.status) throw new Error(r.stderr); return r.stdout; };

// config copy with a 1s poll so the test is quick
fs.cpSync(path.join(ROOT, "config"), path.join(tmp, "config"), { recursive: true });
const sp = path.join(tmp, "config", "settings.yaml");
fs.writeFileSync(sp, fs.readFileSync(sp, "utf8").replace("poll_seconds: 10", "poll_seconds: 1"));

let server;
function startServer() {
  server = spawn(PY, ["-m", "uvicorn", "cat.web.app:app", "--port", String(PORT), "--log-level", "warning"],
    { cwd: ROOT, env: { ...process.env, CAT_DB: DB, CAT_CONFIG_DIR: path.join(tmp, "config") }, stdio: "inherit" });
}
async function waitUp() { for (let i = 0; i < 50; i++) { try { await fetch(BASE + "/api/state"); return; } catch (e) { await sleep(200); } } throw new Error("server did not start"); }

const cfgDir = () => path.join(tmp, "config");
// asleep ticks are deliberately hidden from the diary, so make sure the cat is awake first
async function ensureAwake() {
  for (let i = 0; i < 20; i++) {
    const st = (await (await fetch(BASE + "/api/state")).json()).status.state;
    if (st !== "asleep") return;
    py("scripts/add_ticks.py", "--db", DB, "--config-dir", cfgDir(), "--ticks", "1", "--seed", String(100 + i));
  }
}
async function load(scrollY = 0) {
  const html = (await (await fetch(BASE + "/")).text());
  const dom = new JSDOM(html, { url: BASE + "/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, BASE), o); w.scrollTo = (o) => { w.__scrolled = o; w.__scrollY = (o && o.top) || 0; };
      Object.defineProperty(w, "scrollY", { get: () => w.__scrollY || 0, configurable: true }); w.__scrollY = scrollY; } });
  await new Promise((r) => dom.window.addEventListener("load", r));
  return dom.window;
}
const entries = (w) => [...w.document.querySelectorAll("#diary > .entry")];
const ids = (w) => entries(w).map((e) => parseFloat(e.dataset.id));

(async () => {
  let failed = 0;
  const t = async (name, fn) => { try { await fn(); console.log("ok   -", name); } catch (e) { failed++; console.log("FAIL -", name, "\n      ", e.message); } };
  try {
    // ---------- empty database: empty states, then the first entries appear ----------
    startServer(); await waitUp();
    let w = await load();
    await t("empty DB shows empty states, no errors", async () => {
      assert.ok(!w.document.querySelector("#diary-empty").hidden);
      assert.match(w.document.querySelector("#diary-empty").textContent, /hasn.t woken up yet/);
      assert.ok(!w.document.querySelector("#keepsakes-empty").hidden);
      assert.strictEqual(entries(w).length, 0);
    });
    py("scripts/add_ticks.py", "--db", DB, "--config-dir", path.join(tmp, "config"), "--ticks", "3", "--seed", "1");
    await sleep(2500);
    await t("first ticks replace the empty message and get a day header", async () => {
      assert.ok(w.document.querySelector("#diary-empty").hidden);
      assert.ok(entries(w).length >= 1 && entries(w).length <= 3);
      assert.strictEqual(w.document.querySelectorAll("#diary > .day-header").length, 1);
    });
    w.close();

    // ---------- seeded DB ----------
    server.kill(); await sleep(400);
    for (const f of [DB, DB + "-wal", DB + "-shm"]) fs.rmSync(f, { force: true });
    py("scripts/seed_db.py", "--db", DB, "--ticks", "120", "--seed", "3");
    fs.writeFileSync(path.join(tmp, "config", "settings.yaml"), fs.readFileSync(sp, "utf8")); // keep 1s poll
    startServer(); await waitUp();

    await ensureAwake();
    w = await load();
    const n0 = entries(w).length, top0 = Math.max(...ids(w));
    await t("server render has entries, newest first, with day headers", async () => {
      assert.ok(n0 >= 15);
      assert.deepStrictEqual(ids(w), [...ids(w)].sort((a, b) => b - a));
      assert.ok(w.document.querySelectorAll("#diary > .day-header").length >= 2);
    });

    py("scripts/add_ticks.py", "--db", DB, "--config-dir", path.join(tmp, "config"), "--ticks", "2", "--seed", "5");
    await sleep(2500);
    await t("new ticks appear at the top within a poll, animated, no reload", async () => {
      assert.ok(entries(w).length > n0, "no new entries");
      assert.ok(Math.max(...ids(w)) > top0);
      assert.deepStrictEqual(ids(w), [...ids(w)].sort((a, b) => b - a), "order broken");
      assert.ok(w.document.querySelector("#diary > .entry.is-new"));
      assert.strictEqual(w.document.querySelector("#new-pill").hidden, true);
    });
    await t("panels are refreshed (header time matches server)", async () => {
      const s = await (await fetch(BASE + "/api/state")).json();
      assert.strictEqual(w.document.querySelector("time").textContent, s.time.label);
      assert.strictEqual(w.document.body.dataset.latest, String(s.latest_tick_id));
    });
    w.close();

    // ---------- reader scrolled down: don't jump, show a pill ----------
    await ensureAwake();
    w = await load(800);
    const n1 = entries(w).length;
    py("scripts/add_ticks.py", "--db", DB, "--config-dir", path.join(tmp, "config"), "--ticks", "2", "--seed", "6");
    await sleep(2500);
    await t("scrolled reader: entries are held back and a pill counts them", async () => {
      assert.strictEqual(entries(w).length, n1, "DOM changed under a scrolled reader");
      const pill = w.document.querySelector("#new-pill");
      assert.strictEqual(pill.hidden, false);
      assert.match(pill.textContent, /^\d+ new (entry|entries) ↑$/);
    });
    w.document.querySelector("#new-pill").click();
    await t("clicking the pill inserts them at the top and scrolls up", async () => {
      assert.ok(entries(w).length > n1);
      assert.strictEqual(w.document.querySelector("#new-pill").hidden, true);
      assert.ok(w.__scrolled && w.__scrolled.top === 0);
    });

    // ---------- older pages ----------
    const before = entries(w).length;
    const link = w.document.querySelector("#older");
    await t("'read older pages' appends 20 more via the API", async () => {
      link.click(); await sleep(800);
      assert.ok(entries(w).length > before);
      assert.deepStrictEqual(ids(w), [...ids(w)].sort((a, b) => b - a));
      const keys = entries(w).map((e) => e.dataset.key);
      assert.strictEqual(new Set(keys).size, keys.length, "duplicate entries");
    });

    // ---------- dozing stretch collapses and grows in place ----------
    await ensureAwake();
    py("scripts/add_ticks.py", "--db", DB, "--config-dir", path.join(tmp, "config"), "--ticks", "2", "--offline");
    await sleep(2500);
    let dozes;
    await t("offline ticks show as one collapsed doze line + dozing status", async () => {
      dozes = w.document.querySelectorAll("#diary > .entry-doze");
      assert.ok(dozes.length >= 1);
      assert.ok(w.document.body.classList.contains("state-dozing"));
      assert.match(w.document.querySelector("#pill").textContent, /dozing/);
    });
    const nd = dozes.length;
    py("scripts/add_ticks.py", "--db", DB, "--config-dir", path.join(tmp, "config"), "--ticks", "2", "--offline");
    await sleep(2500);
    await t("a longer doze updates the same entry (no duplicates)", async () => {
      assert.strictEqual(w.document.querySelectorAll("#diary > .entry-doze").length, nd);
    });
    py("scripts/add_ticks.py", "--db", DB, "--config-dir", path.join(tmp, "config"), "--ticks", "1");
    await sleep(2500);
    await t("when the brain returns the status goes back to awake", async () => {
      assert.ok(!w.document.body.classList.contains("state-dozing"));
      assert.match(w.document.querySelector("#pill").textContent, /awake|asleep/);
    });

    // ---------- server down: banner, last data stays; then recovery ----------
    const keep = entries(w).length;
    server.kill(); await sleep(2800);
    await t("API unreachable: banner shows and the last data stays", async () => {
      assert.strictEqual(w.document.querySelector("#banner").hidden, false);
      assert.strictEqual(entries(w).length, keep);
    });
    startServer(); await waitUp(); await sleep(2200);
    await t("banner clears when the server is back", async () => {
      assert.strictEqual(w.document.querySelector("#banner").hidden, true);
    });
    w.close();
  } catch (e) { failed++; console.log("ERROR", e); }
  finally { if (server) server.kill(); fs.rmSync(tmp, { recursive: true, force: true }); }
  console.log(failed ? `\n${failed} FAILED` : "\nall live-update checks passed");
  process.exit(failed ? 1 : 0);
})();

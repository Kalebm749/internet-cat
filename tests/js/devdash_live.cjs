/* Optional end-to-end check of the dev dashboard's browser JS against a real server.
 *   npm i --no-save jsdom ; node tests/js/devdash_live.cjs   (from the repo root; uses .venv/bin/python)
 * Seeds a temp world, starts devdash on a 1s refresh, then drives the real pages in jsdom. */
const { JSDOM } = require("jsdom");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path"), assert = require("assert");

const ROOT = path.resolve(__dirname, "..", "..");
const PY = path.join(ROOT, ".venv", "bin", "python");
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cat-dd-"));
const DB = path.join(tmp, "seed.db"), PORT = 8096, BASE = `http://127.0.0.1:${PORT}`;
const HDR = { "X-Requested-With": "devdash" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const py = (...a) => { const r = spawnSync(PY, a, { cwd: ROOT, encoding: "utf8" }); if (r.status) throw new Error(r.stderr || r.stdout); return r.stdout; };

fs.cpSync(path.join(ROOT, "config"), path.join(tmp, "config"), { recursive: true });
const sp = path.join(tmp, "config", "settings.yaml");
fs.writeFileSync(sp, fs.readFileSync(sp, "utf8").replace("refresh_seconds: 5", "refresh_seconds: 1"));

let server;
const api = async (u, o) => (await fetch(BASE + u, o)).json();
const commands = async () => (await api("/api/dev/commands?limit=100")).items;

async function load(url, { confirmAnswers = [], promptAnswers = [], clip = [] } = {}) {
  const html = await (await fetch(BASE + url)).text();
  const calls = { confirm: [], prompt: [] };
  const dom = new JSDOM(html, { url: BASE + url, runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(new URL(u, BASE), o);
      w.confirm = (q) => { calls.confirm.push(q); return confirmAnswers.length ? confirmAnswers.shift() : true; };
      w.prompt = (q, d) => { calls.prompt.push(q); return promptAnswers.length ? promptAnswers.shift() : d; };
      Object.defineProperty(w.navigator, "clipboard", { value: { writeText: (t) => { clip.push(t); return Promise.resolve(); } }, configurable: true });
      w.scrollTo = () => {};
    } });
  await new Promise((r) => dom.window.addEventListener("load", r));
  dom.calls = calls;
  return dom;
}

(async () => {
  let failed = 0;
  const t = async (name, fn) => { try { await fn(); console.log("ok   -", name); } catch (e) { failed++; console.log("FAIL -", name, "\n      ", e.message); } };
  try {
    py("scripts/seed_db.py", "--db", DB, "--force", "--ticks", "100", "--seed", "4");
    server = spawn(PY, ["-m", "uvicorn", "cat.devdash.app:app", "--port", String(PORT), "--log-level", "warning"],
      { cwd: ROOT, env: { ...process.env, CAT_DB: DB, CAT_CONFIG_DIR: path.join(tmp, "config") }, stdio: "inherit" });
    for (let i = 0; i < 50; i++) { try { await fetch(BASE + "/healthz"); break; } catch (e) { await sleep(200); } }

    // ---------- Overview: dev-control buttons ----------
    let dom = await load("/");
    let w = dom.window, d = w.document;
    await t("overview renders the 8 dev controls and the engine card", async () => {
      assert.strictEqual(d.querySelectorAll("#controls [data-command]").length, 8);
      assert.match(d.body.textContent, /Engine/);
      assert.ok(d.querySelector("#auto-refresh").checked);
    });
    const before = (await commands()).length;
    await t("declining the confirmation sends nothing", async () => {
      dom.calls.confirm.length = 0;
      let dom2 = await load("/", { confirmAnswers: [false] });
      dom2.window.document.querySelector('[data-command="snapshot"]').click();
      await sleep(400);
      assert.strictEqual(dom2.calls.confirm.length, 1);
      assert.strictEqual((await commands()).length, before);
      dom2.window.close();
    });
    await t("confirming queues the command (pending: the engine runs it) and shows a toast", async () => {
      let dom2 = await load("/", { confirmAnswers: [true] });
      dom2.window.document.querySelector('[data-command="snapshot"]').click();
      await sleep(600);
      const items = await commands();
      assert.strictEqual(items.length, before + 1);
      assert.strictEqual(items[0].command, "snapshot"); assert.strictEqual(items[0].status, "pending");
      const toast = dom2.window.document.querySelector("#toast");
      assert.ok(!toast.hidden && /queued #\d+: snapshot/.test(toast.textContent));
      dom2.window.close();
    });
    await t("set interval asks seconds then ticks, and sends them", async () => {
      let dom2 = await load("/", { promptAnswers: ["7", "3"] });
      dom2.window.document.querySelector('[data-command="set_interval"]').click();
      await sleep(600);
      const c = (await commands())[0];
      assert.strictEqual(c.command, "set_interval"); assert.deepStrictEqual(JSON.parse(c.args), { seconds: 7, ticks: 3 });
      dom2.window.close();
    });
    await t("inject event sends the text and the spawned object", async () => {
      let dom2 = await load("/", { promptAnswers: ["A storm rolls in.", "a puddle"] });
      dom2.window.document.querySelector('[data-command="inject_event"]').click();
      await sleep(600);
      const a = JSON.parse((await commands())[0].args);
      assert.strictEqual(a.text, "A storm rolls in."); assert.strictEqual(a.spawn.name, "a puddle");
      dom2.window.close();
    });
    await t("an invalid value is rejected by the server and reported", async () => {
      let dom2 = await load("/", { promptAnswers: ["-5", ""] });
      const n0 = (await commands()).length;
      dom2.window.document.querySelector('[data-command="set_interval"]').click();
      await sleep(600);
      assert.strictEqual((await commands()).length, n0);
      assert.match(dom2.window.document.querySelector("#toast").textContent, /could not queue set_interval/);
      dom2.window.close();
    });

    // ---------- auto-refresh ----------
    const tickLabel = (win) => win.document.querySelector('a[href^="/ticks/"]').textContent;
    const t0 = tickLabel(w);
    py("scripts/add_ticks.py", "--db", DB, "--config-dir", path.join(tmp, "config"), "--ticks", "2", "--seed", "9");
    await sleep(2500);
    await t("auto-refresh picks up new ticks without a reload", async () => {
      assert.notStrictEqual(tickLabel(w), t0);
      assert.match(d.querySelector("#refresh-state").textContent, /updated/);
    });
    const box = d.querySelector("#auto-refresh");
    box.checked = false; box.dispatchEvent(new w.Event("change"));
    const t1 = tickLabel(w);
    py("scripts/add_ticks.py", "--db", DB, "--config-dir", path.join(tmp, "config"), "--ticks", "2", "--seed", "10");
    await sleep(2500);
    await t("the pause-refresh toggle stops the updates", async () => {
      assert.strictEqual(tickLabel(w), t1);
      assert.strictEqual(d.querySelector("#refresh-state").textContent, "(paused)");
    });
    w.close();

    // ---------- Tick detail: copy buttons ----------
    const clip = [];
    // a real action tick (brain + narrator calls): which id that is depends on when the seed run slept
    const tid = py("-c", "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute(\"SELECT id FROM ticks WHERE kind='tick' ORDER BY id LIMIT 1 OFFSET 20\").fetchone()[0])", DB).trim();
    dom = await load(`/ticks/${tid}`, { clip }); w = dom.window; d = w.document;
    await t("copy buttons copy the exact text of their block", async () => {
      const btns = [...d.querySelectorAll("button.copy")];
      assert.ok(btns.length >= 8);
      const user = d.querySelector("#c" + d.querySelector("[id$='-user']").id.slice(1)) || d.querySelector("[id$='-user']");
      const btn = d.querySelector(`button.copy[data-copy="#${user.id}"]`);
      btn.click(); await sleep(200);
      assert.strictEqual(clip[0], user.textContent);
      assert.ok(user.textContent.includes("## Who you are right now"));
      assert.strictEqual(btn.textContent, "copied");
    });
    await t("the tick page lists the stage bar and the public-site box", async () => {
      assert.ok(d.querySelector(".stagebar")); assert.match(d.querySelector("#public").textContent, /What the public site shows/);
    });
    w.close();

    // ---------- Logs: live tail ----------
    dom = await load("/logs"); w = dom.window; d = w.document;
    const rows0 = d.querySelectorAll("#log-table tbody tr").length;
    const logfile = path.join(tmp, "logs", "engine.jsonl");
    d.querySelector("#live-tail").checked = true; d.querySelector("#live-tail").dispatchEvent(new w.Event("change"));
    await sleep(500);
    const localIso = (ms) => new Date(Date.now() + ms - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, -1);   // log ts are local time
    const stamp = localIso(5000);                                                  // newer than anything seeded
    fs.appendFileSync(logfile, JSON.stringify({ ts: stamp, level: "ERROR", service: "engine", tick_id: 42, event: "demo", message: "live tail says hello <b>", extra: { k: 1 } }) + "\n");
    await sleep(3500);
    await t("live tail inserts a new log line at the top, escaped, with its tick link", async () => {
      const first = d.querySelector("#log-table tbody tr");
      assert.match(first.textContent, /live tail says hello <b>/);
      assert.ok(!first.querySelector("b"), "message must not be interpreted as HTML");
      assert.strictEqual(first.querySelector("a").getAttribute("href"), "/ticks/42");
      assert.ok(first.classList.contains("lv-error"));
      assert.strictEqual(d.querySelectorAll("#log-table tbody tr").length, rows0 + 1);
      assert.match(d.querySelector("#tail-state").textContent, /new|listening/);
    });
    d.querySelector("#live-tail").checked = false; d.querySelector("#live-tail").dispatchEvent(new w.Event("change"));
    fs.appendFileSync(logfile, JSON.stringify({ ts: localIso(9000), level: "INFO", service: "engine", tick_id: null, event: "x", message: "after toggle off", extra: {} }) + "\n");
    await sleep(2500);
    await t("turning the live tail off stops it", async () => {
      assert.ok(![...d.querySelectorAll("#log-table tbody tr")].some((r) => /after toggle off/.test(r.textContent)));
    });
    w.close();

    // ---------- Playground form ----------
    const brainId = (await api("/api/dev/llm?kind=brain&limit=1")).items[0].id;
    dom = await load("/playground?call_id=" + brainId); w = dom.window; d = w.document;
    await t("the playground is prefilled with the stored prompts and runs a replay (Ollama is unreachable here)", async () => {
      assert.ok(d.querySelector("#pg-user").value.includes("## Who you are right now"));
      assert.ok(d.querySelector("#pg-system").value.length > 100);
      assert.ok(d.querySelector("#pg-original"));
      d.querySelector("#pg-extra-model").value = "qwen3:14b";
      d.querySelector("#pg-form").dispatchEvent(new w.Event("submit", { cancelable: true }));
      await sleep(4000);
      const cards = d.querySelectorAll("#pg-results .rcard");
      assert.strictEqual(cards.length, 2, "original + 1 run");
      assert.match(cards[1].textContent, /qwen3:14b/); assert.match(cards[1].textContent, /error/);
      assert.match(d.querySelector("#pg-status").textContent, /done in/);
      assert.ok(!d.querySelector("#pg-run").disabled);
    });
    await t("choosing no model is refused client-side", async () => {
      d.querySelectorAll("input[name=model]").forEach((i) => (i.checked = false));
      d.querySelector("#pg-extra-model").value = "";
      d.querySelector("#pg-form").dispatchEvent(new w.Event("submit", { cancelable: true }));
      await sleep(200);
      assert.match(d.querySelector("#pg-status").textContent, /choose a model/);
    });
    w.close();
  } catch (e) { failed++; console.log("ERROR", e); }
  finally { if (server) server.kill(); fs.rmSync(tmp, { recursive: true, force: true }); }
  console.log(failed ? `\n${failed} FAILED` : "\nall dashboard JS checks passed");
  process.exit(failed ? 1 : 0);
})();

# Pi Architecture: World Engine + Host Site

Build spec for the Raspberry Pi side of Internet Cat. Read `creature-architecture.md` first. It defines the concepts (brain, narrator, needs, mood, memory, sleep). This doc says **how to build them on the Pi**.

The laptop is just an Ollama server (see `laptop-brain.md`). **All game logic lives on the Pi.**

---

## 1. Goals for v1

1. A world loop that runs 24/7 and keeps the creature alive: ticks, brain and narrator calls, needs, mood, memory, sleep.
2. A read-only website, served from the Pi on the home network, that shows the creature's current state and history.
3. Survives the brain going offline, the Pi rebooting and bad LLM output, without corrupting state.

**Not in v1:** visitor interaction, a 3D world, public internet hosting, multiple creatures.

---

## 2. Hardware constraints (Pi 3B+)

- 1GB RAM, 4× Cortex-A53. Use **64-bit Raspberry Pi OS Lite** (Bookworm or later), Python 3.11+.
- No LLM or embedding work runs on the Pi. Everything model-related is an HTTP call to the laptop.
- Keep dependencies light. Avoid anything that needs compiling on the Pi.
- SD cards wear out. Write in small batches (SQLite in WAL mode) and back up the DB nightly.

---

## 3. Stack

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| Storage | SQLite (WAL mode), single file `data/cat.db` |
| Config | YAML (`config/character.yaml`, `config/world.yaml`, `config/settings.yaml`) |
| LLM client | `httpx` → Ollama HTTP API (`/api/chat` with `format` JSON schema, `/api/embed`) |
| Vector math | `numpy` (cosine similarity over a few thousand vectors is trivial) |
| Web | `FastAPI` + `uvicorn`, Jinja2 templates, light vanilla JS polling `/api/state` |
| Process mgmt | Two `systemd` services: `cat-engine` and `cat-web` |
| Tests | `pytest`, with a `MockBackend` so everything runs without the laptop |

---

## 4. Processes

```
┌─────────────── Raspberry Pi ───────────────┐            ┌──── Laptop ────┐
│  cat-engine (tick loop) ──── HTTP ─────────┼───────────►│ Ollama :11434  │
│       │ writes                             │            └────────────────┘
│       ▼                                    │
│   data/cat.db (SQLite, WAL)                │
│       ▲ reads                              │
│  cat-web (FastAPI :8080) ◄── browser (LAN) │
└────────────────────────────────────────────┘
```

- **cat-engine** is the only writer. It runs the tick loop forever.
- **cat-web** is read-only. It never calls the LLM.
- They share nothing except the database. Either can restart independently.

---

## 5. Repo layout

```
internet-cat/
  CLAUDE.md                  # short pointer to these docs + conventions
  requirements.txt
  docs/
    creature-architecture.md
    pi-architecture.md
    laptop-brain.md
    web-gui.md
  config/
    character.yaml           # identity, core traits, tendencies, needs + decay
    world.yaml               # areas, objects, events
    settings.yaml            # ollama host, models, tick interval, tuning numbers
  cat/
    __init__.py
    main.py                  # engine entrypoint: loads config, runs loop
    clock.py                 # sim time, day/night
    world.py                 # areas, objects, events, validating + applying effects
    character.py             # needs, decay, mood derivation, tendencies
    effects.py               # closed set of world changes + validation
    memory.py                # short-term, long-term, recall, embeddings
    brain.py                 # builds brain prompt, parses brain output
    narrator.py              # builds narrator prompt, parses narrator output
    sleep.py                 # nightly consolidation, trait drift, preferences
    llm.py                   # LLMBackend interface, OllamaBackend, MockBackend
    db.py                    # schema, migrations, queries
    web/
      app.py                 # FastAPI app
      templates/index.html
      static/ (css, js, cat.png)
  tests/
  deploy/
    cat-engine.service
    cat-web.service
    backup.sh
```

---

## 6. Data model (SQLite)

```sql
-- singleton rows hold current state as JSON so the schema stays flexible early on
state(id INTEGER PRIMARY KEY CHECK(id=1),
      sim_time TEXT, area TEXT, needs JSON, mood JSON, energy REAL,
      tendencies JSON, is_asleep INT, brain_online INT, updated_at TEXT)

objects(id TEXT PRIMARY KEY, name TEXT, area TEXT,      -- area = 'inventory' when carried
        tags JSON, state JSON, created_by TEXT,          -- 'world' | 'event' | 'creature'
        created_at TEXT)

ticks(id INTEGER PRIMARY KEY, sim_time TEXT, real_time TEXT, area TEXT,
      reaction TEXT, thought TEXT, action TEXT, intent TEXT,
      outcome TEXT, gains JSON, effort TEXT, duration INT,
      changes JSON, rejected_changes JSON, needs_after JSON, mood_after JSON)

events(id INTEGER PRIMARY KEY, sim_time TEXT, area TEXT, text TEXT)

memories(id INTEGER PRIMARY KEY, kind TEXT,             -- 'short' | 'long'
         text TEXT, importance REAL, created_sim_time TEXT,
         last_recalled TEXT, embedding BLOB, embed_model TEXT)

preferences(id INTEGER PRIMARY KEY, subject TEXT, valence REAL,  -- -1..1
            reason TEXT, updated_at TEXT)

sleep_log(id INTEGER PRIMARY KEY, sim_date TEXT, summary TEXT,
          tendency_changes JSON, new_memories JSON, new_preferences JSON)
```

**Memories are stored as text plus an embedding.** The embedding (a list of floats saved as a `float32` blob) is only used to search. The website always shows the `text`. Store `embed_model` so that if the embedding model changes, old memories can be re-embedded with a one-off script.

---

## 7. Config

### `character.yaml` (example shape)
```yaml
name: Mochi
species: small grey cat
appearance: "..."
backstory: "..."
core_traits: [curious, dramatic, secretly sentimental]   # never change
voice: "short lowercase sentences, playful"
tendencies:            # 0..1, drift during sleep
  boldness: 0.5
  sociability: 0.4
  tidiness: 0.6
needs:                 # 0..100, decay per sim hour
  food:        {start: 60, decay: 6}
  energy:      {start: 80, decay: 4}
  fun:         {start: 50, decay: 7}
  cleanliness: {start: 70, decay: 2}
  curiosity:   {start: 50, decay: 5}
```

### `world.yaml`
Areas (description, exits), starting objects (name, area, tags, state), and events (text, chance, optional area/hour filters, optional object to spawn). Start with a **fixed, hand-written map**. Objects can still be created at runtime by events or by the creature, so the world is already "growable" at the object level.

### `settings.yaml`
```yaml
ollama_host: http://192.168.1.50:11434
brain_model: qwen3:8b
narrator_model: qwen3:8b
embed_model: nomic-embed-text
tick_interval_seconds: 90
sim_minutes_per_tick_default: 20
gain_values: {"0": 0, "+": 15, "++": 30}
effort_energy_cost: {low: 2, medium: 6, high: 12}
repetition_penalty: {window: 6, similarity_threshold: 0.85, multiplier: 0.4}
tendency_max_step: 0.05
recall: {k: 5, w_recency: 1.0, w_importance: 1.0, w_relevance: 1.5}
```
All tuning numbers live here, not in code.

---

## 8. Engine: the tick

Follow `creature-architecture.md` §3. The implementation rules:

1. **World tick** (`clock`, `character`, `world`): advance the sim clock, decay needs, roll events, apply any objects the events spawn.
2. **Brain call** (`brain.py`): build the situation overview (§4 of the architecture doc) and call Ollama with a JSON schema. Output: `reaction, thought, action, intent`.
3. **Narrator call** (`narrator.py`): send the action, the area's objects (id, name, tags, state), the inventory, the time and weather. Output: `outcome, gains, effort, duration, changes[]`.
4. **Apply** (`effects.py`): validate each change (see §9), apply the valid ones and log the rejected ones. Convert buckets to numbers, apply the repetition penalty, apply the effort cost, clamp, and recompute mood.
5. **Record**: insert a `ticks` row. If the tick was notable (big need change, new object, rejected weirdness, strong reaction), add a short-term memory with its embedding.
6. **Sleep check**: if energy is low and it's night, or the brain chose to sleep, run `sleep.py` (§11).

**Each tick runs in a single DB transaction.** A crash mid-tick must leave the previous state intact.

---

## 9. Effects: "open actions, closed effects"

The creature can **try anything** ("build a fort out of cushions"). The narrator writes the story freely. But the narrator can **change the saved world only with these operations**:

| Op | Args | Validation |
|---|---|---|
| `move_self` | `area` | area is connected to the current area |
| `move_object` | `object_id, area` | object is in the current area or inventory, tagged `movable`, and the target is current or adjacent |
| `take` | `object_id` | object is in the current area and tagged `movable` |
| `drop` | `object_id` | object is in the inventory |
| `consume` | `object_id` | object is in reach and tagged `edible`. Deletes it. Only this can raise `food`. |
| `set_state` | `object_id, key, value` | object is in reach |
| `create` | `name, tags, area` | area is current. Rate-limited (e.g. ≤1 per tick). Tags come from an allowed list. |

Anything that fails validation goes into `rejected_changes` and doesn't happen. Story text that contradicts the saved world is fine for one tick. The next prompt is built from the database, so reality wins.

**Why:** without this, the narrator makes things up (eating fish that don't exist) and nothing stays. With it, the fort is still there tomorrow, and the creature can remember it and come back to it.

---

## 10. Memory and recall

- **Short-term:** notable moments from today. Shown in full in the brain prompt (the most recent ~8).
- **Long-term:** created during sleep. Recalled by `score = w_recency·recency + w_importance·importance + w_relevance·cosine(query, memory)`, where the query embedding comes from a short description of the current situation (area, the objects here, the current mood, the last outcome).
- Embeddings come from `POST {ollama_host}/api/embed` using `embed_model`.
- Recall loads every long-term embedding into a numpy matrix in memory. Reload it after sleep. That's fine up to tens of thousands of memories.
- Update `last_recalled` whenever a memory is used. That feeds the recency score and makes memories that keep coming up feel "important".

---

## 11. Sleep cycle

Triggered from the tick. While asleep, ticks don't call the brain. They just restore energy and the clock moves forward. Run once per sleep:

1. **Consolidate:** LLM call with today's short-term memories → 2–5 long-term memories with importance (0–1). Embed and store them, then delete the short-term memories.
2. **Tendencies:** LLM call with the day's summary + current tendencies → proposed change for each tendency. **Clamp each one to ±`tendency_max_step`** and to 0..1.
3. **Preferences:** LLM call → new or updated `preferences(subject, valence, reason)`.
4. Write a `sleep_log` row. The website can show "last night Mochi dreamed about…"

If the brain is offline during sleep, try again on the next sleep. Never skip the clamp.

---

## 12. Brain offline handling

- Before each tick, call `GET {ollama_host}/api/tags` with a ~3s timeout. Cache the result for one tick.
- **Offline:** the world tick still runs (clock, decay, events). Set `brain_online=0`. The creature "dozes", which restores a small amount of energy, and the tick is logged with the outcome "dozing". No brain or narrator calls.
- **Online again:** resume normally. The first brain prompt notes that it just woke from a doze.
- LLM call that times out or returns invalid JSON: retry once, then treat it as a doze tick. Never crash the loop.

---

## 13. Website (v1, read-only)

**The full GUI spec is in `web-gui.md`** (layout, visual style, components, API shapes). That doc takes priority over the summary below.

FastAPI on port 8080, LAN only. One page and one JSON endpoint.

### `GET /api/state` → JSON
Everything the page needs, read from the database.

### Page panels
| Panel | Source |
|---|---|
| **Cat icon** | Static image `static/cat.png` (a render of the 3D model). Later: swap in `<model-viewer>` with a `.glb`. |
| **Time and location** | `state.sim_time`, `state.area` (+ "dozing"/"asleep" badge, brain online indicator) |
| **Mood and energy** | `state.mood` (word + small bar), `state.energy` bar |
| **Needs** | One bar per need from `state.needs` |
| **Core traits** | `character.yaml` core traits (chips) |
| **Tendencies** | One slider-style bar per tendency (0..1), with last night's change (▲/▼) |
| **Preferences** | `preferences` sorted by abs(valence): likes and dislikes |
| **Diary** | Last ~20 `ticks`: time, area, action, outcome, reaction. Newest first. |
| **Long-term memories** | `memories where kind='long'`, newest first, with importance |

- The page polls `/api/state` every ~10s and redraws. No framework needed.
- Must work on a phone screen.

---

## 14. Headless OS setup (no monitor or keyboard)

The Pi runs **headless**. Everything is done over SSH from your PC.

### Flash the microSD card
1. Use a decent card: **32GB+, A1/A2 rated**, from a known brand. Cheap cards die fast under a constantly writing database.
2. On your PC, install **Raspberry Pi Imager**. Choose:
   - Device: **Raspberry Pi 3**
   - OS: **Raspberry Pi OS Lite (64-bit)**. "Lite" means no desktop, which saves RAM.
3. Click **Edit settings** (OS customisation) before writing:
   - **Hostname:** `internet-cat` → reachable as `internet-cat.local`
   - **Username/password:** your own (don't keep defaults)
   - **Wi-Fi:** only if not using Ethernet (set the country correctly)
   - **Locale/timezone:** `America/Denver`
   - **Services tab → Enable SSH.** Best option: "Allow public-key authentication only" and paste your PC's public key (`~/.ssh/id_ed25519.pub`; create one with `ssh-keygen -t ed25519` if needed).
4. Write the card, put it in the Pi and power on. The first boot takes a couple of minutes.

### First login
```
ssh <user>@internet-cat.local
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv sqlite3
```
- **Prefer wired Ethernet** (the 3B+ has a port). It's more reliable than Wi-Fi for a 24/7 box.
- Give the Pi a **fixed IP** with a DHCP reservation in your router (like the laptop).
- If `.local` doesn't resolve on your PC, find the Pi's IP in your router's device list.

### Get the code on
```
sudo mkdir -p /opt/internet-cat && sudo chown $USER /opt/internet-cat
git clone <your-repo-url> /opt/internet-cat
cd /opt/internet-cat && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
Workflow: write code on your PC with Claude Code → `git push` → on the Pi `git pull` + `sudo systemctl restart cat-engine cat-web`. Later, a `deploy/pi/update.sh` can do this in one command.

### Viewing the site
From any browser on the network: `http://internet-cat.local:8080`.

### Keeping it healthy headless
- `journalctl -u cat-engine -f` to watch the creature live over SSH.
- Reduce SD wear: the nightly DB backup should copy to the laptop (`rsync` over SSH) as well as to the card, so a dead card doesn't lose the creature's life.
- Optional: `sudo apt install unattended-upgrades` for security updates.

---

## 15. Deployment

- `deploy/cat-engine.service` and `cat-web.service`: `Restart=always`, run as a non-root user, working dir `/opt/internet-cat`.
- `deploy/backup.sh` (cron, nightly): `sqlite3 data/cat.db ".backup data/backups/cat-$(date +%F).db"`, keep 14 copies.
- Logs go to journald.

---

## 16. Build order (milestones)

1. **Skeleton:** repo layout, config loading, SQLite schema, `MockBackend`. `pytest` passes.
2. **World + character:** clock, decay, events, mood derivation. A loop with mock decisions runs for 1000 ticks without errors.
3. **Effects:** all ops + validation, with unit tests for every rejection case.
4. **Ollama brain + narrator:** real calls with JSON schemas. Run 50 ticks and read the diary.
5. **Memory:** short-term, embeddings, recall.
6. **Sleep:** consolidation, tendencies with clamping, preferences.
7. **Offline handling:** kill Ollama mid-run, and the engine keeps going and recovers.
8. **Website:** all panels.
9. **Deploy:** systemd, backups, run for a week.

**Done for v1:** it runs for 7 days unattended, the diary reads like a life, and nothing in the world appears or disappears without an effect causing it.

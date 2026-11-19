# Server Architecture: World Engine, Website and Ollama on One Host

How Internet Cat is built and run. Read `creature-architecture.md` first: it defines the concepts (brain, narrator, needs, mood, memory, sleep). This doc says **how they are built** and **how the system is deployed**. The website is specified in `web-gui.md`; every config field is in `config-reference.md`.

Everything runs on **one machine**: an Ubuntu laptop (HP Omen 15, GTX 1060 6GB, 16GB RAM) under **Docker Compose**. Ollama, the engine, the public website and the dev dashboard are all containers on that host. There is no separate server and no Raspberry Pi.

---

## 1. Goals for v1

1. A world loop that runs 24/7 and keeps the creature alive: ticks, brain and narrator calls, needs, mood, memory, sleep.
2. A read-only website, served on the home network, that shows the creature's current state and history.
3. Survives the brain going offline, the machine or containers restarting, and bad LLM output, without corrupting state.

**Not in v1:** visitor interaction, a 3D world, public internet hosting, multiple creatures.

---

## 2. Host and constraints

- Ubuntu laptop, GTX 1060 6GB (VRAM is the scarce resource), 16GB RAM, plugged in 24/7 on wired Ethernet if possible.
- The GPU is only used by Ollama. The engine and website are light Python processes.
- Models must fit in 6GB VRAM together: one chat model for everything plus the embedding model (see §3).
- SQLite lives on a bind mount (`./data`), in WAL mode. Back it up regularly (§15).
- Keep dependencies light and pure-Python where possible (fast image builds, small image).

---

## 3. Stack

| Concern | Choice |
|---|---|
| Language | Python 3.11+ (image: 3.12-slim) |
| Storage | SQLite (WAL), single file `data/cat.db` |
| Config | YAML: `config/character.yaml`, `world.yaml`, `settings.yaml` |
| LLM client | `httpx` → Ollama HTTP API (`/api/chat` with a JSON-schema `format`, `/api/embed`, `/api/tags`) |
| Vector math | `numpy` (cosine similarity over a few thousand vectors is trivial) |
| Web | `FastAPI` + `uvicorn`, Jinja2 templates, light vanilla JS polling `/api/state` |
| Deployment | Docker Compose: `ollama`, `engine`, `web`, `devdash` |
| Tests | `pytest`, with a `MockBackend` so everything runs without Ollama |

**Models.** A 4–8B instruct model at ~Q4 (e.g. `qwen3:8b`, `llama3.1:8b`, `gemma3:4b` as the lighter fallback), plus `nomic-embed-text`. Use **one chat model for brain, narrator and sleep** so only one big model is resident. Check the Ollama library for newer small models before pulling. To choose, compare candidates on saved prompts: JSON validity (should be 100% with `format`), seconds per call, the narrator's rejected-change rate, and read the diary text for voice and variety.

---

## 4. Services

```
┌──────────────────────── Ubuntu laptop (Docker Compose) ────────────────────────┐
│  ollama  (GPU, :11434 internal only)  ◄── HTTP ──  engine (tick loop)          │
│                                                        │ writes                 │
│                                                        ▼                        │
│                                              data/cat.db (SQLite, WAL)          │
│                                                        ▲ reads                  │
│  browser (LAN) ──►  web  (FastAPI :8080)  ─────────────┤                        │
│  you (laptop)  ──►  devdash (:8090, 127.0.0.1)  ───────┘  + data/devdash.db     │
└─────────────────────────────────────────────────────────────────────────────────┘
```

- **ollama**: the official image with the NVIDIA GPU. Not published to the network.
- **engine**: the only writer of game state. Runs the tick loop forever.
- **web**: read-only. Never calls the LLM. Port 8080 is the only port exposed to the LAN.
- **devdash**: the developer dashboard (§17). Read-only on `cat.db`; published on 127.0.0.1 only unless you opt in (then Basic auth is mandatory).
- They share nothing except the data directory. Each can restart independently. `restart: unless-stopped` and healthchecks on all of them.

---

## 5. Repo layout

```
internet-cat/
  CLAUDE.md  README.md  requirements.txt  requirements-dev.txt
  Dockerfile  compose.yaml  .env.example            # Docker Compose deployment
  docs/        creature-architecture.md  server-architecture.md  web-gui.md  config-reference.md  phase-2-...md
  config/      character.yaml  world.yaml  settings.yaml
  cat/
    config.py clock.py world.py character.py effects.py memory.py
    brain.py narrator.py sleep.py llm.py db.py engine.py main.py
    trace.py logs.py devqueue.py devcontrol.py downtime.py healthcheck.py pullmodels.py
    web/       app.py views.py templates/ static/
    devdash/   app.py auth.py data.py playground.py templates/ static/ (vendor/uPlot)
  scripts/     seed_db.py add_ticks.py show_diary.py check_ollama.py reembed.py make_placeholder_assets.py
  deploy/      backup.sh  update.sh  pull-models.sh
  tests/
  data/        cat.db  devdash.db  logs/  backups/     # bind mount, git-ignored
```

---

## 6. Data model (SQLite)

```sql
meta(key PRIMARY KEY, value)                -- first_sim_time, brain_last_seen, sleep_pending, ...
state(id=1, sim_time, area, needs JSON, mood JSON, energy, weather,
      tendencies JSON, is_asleep, brain_online, updated_at)
objects(id PRIMARY KEY, name, area,         -- area = 'inventory' when carried
        tags JSON, state JSON, created_by,  -- 'world' | 'event' | 'creature'
        created_at)
ticks(id, kind,                             -- 'tick' | 'doze' | 'asleep'
      sim_time, real_time, area, reaction, thought, action, intent,
      outcome, gains JSON, effort, duration, changes JSON, rejected_changes JSON,
      needs_after JSON, mood_after JSON)
events(id, tick_id, sim_time, area, text)
memories(id, kind,                          -- 'short' | 'long'
         text, importance, created_sim_time, last_recalled, embedding BLOB, embed_model)
preferences(id, subject, valence, reason, updated_at)      -- valence -1..1
self_image(id, text, confidence, formed_day, updated_at)    -- what it thinks it is; learned in sleep; confidence 0..1
sleep_log(id, sim_date, summary, tick_id,   -- tick_id: where the sleep began
          tendency_changes JSON, new_memories JSON, new_preferences JSON)
-- tracing (§17): tick_traces, llm_calls, embed_calls, recall_logs, effect_logs, need_math, mood_math,
--                event_rolls, sleep_traces;  memories also carry tick_id (which tick wrote them)
```

**Energy is not stored as a need.** It is derived from the needs (`energy_weight`) and cached in `state.energy`.

**Memories are text plus an embedding.** The embedding (`float32` blob) is only a search index. The website shows the text. `embed_model` is stored so that changing the model can be followed by `scripts/reembed.py`.

---

## 7. Config

Three YAML files in `config/`, mounted read-only into the containers. Every field is described in `config-reference.md`. **All tuning numbers live in `settings.yaml`, never in code.** Key points:

- `ollama_host` defaults to `http://ollama:11434` (the Compose service name).
- `character.yaml` and `world.yaml` currently hold a placeholder cat and a tiny house; the engine works with any content that follows the schema.
- `settings.yaml` is read when a service starts. Edit it and restart the engine (a dev-dashboard command will later reload it live).

---

## 8. Engine: the tick

Follow `creature-architecture.md` §3. As built:

1. **World events** roll at the start of the tick (weather changes, spawned objects).
2. **Rest checks.** If asleep, or exhausted at night, the tick is a *rest* tick (no brain call, §11).
3. **Brain call** builds the situation overview (words, never numbers) and asks Ollama for `reaction, thought, action, intent, feeling, wants_sleep` with a JSON schema. Relevant long-term and recent short-term memories are included.
4. **Narrator call** gets the action plus the area's objects (ids, tags, state), inventory, exits, time and weather. It returns `outcome, gains, effort, duration, changes[]`.
5. **Apply:** validate every change (§9), convert buckets to numbers, apply the repetition penalty and the effort cost, decay needs for the action's duration, clamp, recompute mood.
6. **Record:** insert the `ticks` row, events, object changes, any short-term memory and the new state in **one transaction**. LLM and embedding calls happen *outside* it, so a crash leaves the previous state intact.
7. If the brain chose to sleep (and is tired enough, or it is night), the sleep cycle runs after the tick is committed.

Calls are retried once (`llm_retries`). If they still fail, the tick is a doze (§12).

---

## 9. Effects: "open actions, closed effects"

The creature can try anything; the narrator writes the story freely; but the saved world changes **only** through these ops, checked in order against the real state (`cat/effects.py`):

| Op | Args | Validation |
|---|---|---|
| `move_self` | `area` | area is connected to the current area |
| `move_object` | `object_id, area` | in reach, `movable`, target is here or adjacent |
| `take` | `object_id` | object is here, `movable`, inventory not full |
| `drop` | `object_id` | object is in the inventory |
| `consume` | `object_id` | in reach and `edible`; deletes it. Only this can raise needs flagged `requires_consume` (food) |
| `set_state` | `object_id, key, value` | in reach; length-limited |
| `create` | `name, tags, area` | current area; rate-limited per tick; tags filtered to `allowed_tags` |

Anything that fails goes into `rejected_changes` with a reason, is logged, and does not happen. The next prompt is built from the database, so reality wins. Every object appearance or disappearance (including event spawns) is recorded in a tick's `changes`, so the world can be audited.

---

## 10. Memory and recall

- **Short-term:** notable ticks (big need change, strong feeling, a creation, rejected changes). The most recent few appear in the brain prompt.
- **Long-term:** created during sleep. Recalled by `w_recency·recency + w_importance·importance + w_relevance·cosine(query, memory)`; the query is a short description of the current situation. Embeddings come from `/api/embed`. If embedding fails, memories are stored without an index and recall falls back to recency + importance.
- `last_recalled` is updated when a memory is used, which feeds recency.

---

## 11. Sleep

- The creature falls asleep when exhausted at night, or when the brain sets `wants_sleep` and it is tired enough (or night). Asleep ticks skip the brain and narrator, restore the `sleep_restore` need, and decay needs slowly. It wakes when rested, in the morning.
- When sleep begins, the sleep cycle makes four calls (consolidate memories, drift tendencies, learn preferences, propose a self-image line). **Nothing is written unless all succeed**; if the brain is offline it stays pending and retries on every sleeping tick. LLM output becomes numbers in code: importance buckets, tendency proposals scaled and **clamped** to ±`sleep.tendency_max_step`, blended preference valences. Core traits never change.
- **Self-image step:** one call reads the day's short-term memories and log plus the current self-image lines and proposes **at most one** new line (or none). A line carries text, a confidence (0..1) and the day it was formed. A proposal may name an existing line in `replaces` to revise or contradict it. Proposals below `sleep.self_image_min_confidence`, duplicates and empty text are rejected; over `sleep.max_self_image_lines` the least confident lines are forgotten. Every proposal and its outcome (`added`, `revised`, `rejected`, `dropped_at_cap`, `none`) is written to the `sleep_traces` row with step `self_image`. The brain sees the lines (text only) in its situation prompt; the website shows them in the About panel.

---

## 12. Brain offline and downtime

- **Brain offline.** Each tick starts with `GET /api/tags` (cached for that tick). If Ollama is unreachable, or a call still fails after its retry, the tick is a **doze**: events, clock and decay still run, a little rest is restored, `brain_online=0`, and the tick is stored as `kind='doze'` with outcome "dozing". When the brain returns, the first prompt notes that the creature just woke from a doze. The loop never crashes because the brain is unavailable.
- **Whole-system downtime** (the host or containers were off): governed by `downtime_policy` in `settings.yaml`: `pause` (sim time resumes exactly where it stopped) or `catch_up` (decay for the gap, capped by `max_catch_up_hours`, with one "dozed through the night" diary entry).
- A tick's transaction is atomic, so stopping or killing a container mid-tick never leaves half a tick behind.

---

## 13. Website

The full GUI spec is `web-gui.md`, which takes priority for the website. FastAPI on port 8080, one page plus a small JSON API (`/api/state`, `/api/diary`, `/api/keepsakes`, `/healthz`), read-only, fully server-rendered so it works without JavaScript.

---

## 14. Host setup (the laptop)

One-time setup of the Ubuntu host. The README gives the exact commands.

**Hardware.** HP Omen 15, GTX 1060 Mobile 6GB (confirm with `nvidia-smi`), 16GB RAM. The Omen has hybrid graphics (Intel + NVIDIA); Ollama uses the NVIDIA GPU either way. `ollama ps` must show `100% GPU`. Use wired Ethernet if possible.

**GPU in containers.** Install the NVIDIA driver (`sudo ubuntu-drivers install`, reboot, `nvidia-smi`), Docker Engine with the compose plugin, and the **NVIDIA Container Toolkit**; run `sudo nvidia-ctk runtime configure --runtime=docker` and restart Docker. Test with `docker run --rm --gpus all nvidia/cuda:<tag> nvidia-smi`. If Ollama was installed natively before, disable it so it does not fight the container for the GPU and port: `sudo systemctl disable --now ollama`.

**Network and firewall.**
- Give the laptop a fixed IP (a DHCP reservation in the router).
- Only the website is exposed: allow 8080 from the local subnet only (`sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp`, adjusting the subnet; allow OpenSSH first if you use SSH, then `sudo ufw enable`).
- Ollama has no authentication and is **not published** by Compose. Never port-forward 8080 or 11434 on the router.
- Docker publishes ports by editing iptables directly, which can bypass ufw rules. Keep port mappings bound deliberately (the dev dashboard binds to 127.0.0.1) and test from another machine.

**Power (24/7).**
- Never sleep: `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`.
- Lid closed = keep running: in `/etc/systemd/logind.conf` set `HandleLidSwitch=ignore`, `HandleLidSwitchExternalPower=ignore`, `HandleLidSwitchDocked=ignore`, then `sudo systemctl restart systemd-logind`.
- No surprise reboots: keep `Unattended-Upgrade::Automatic-Reboot "false";` (or pick a time inside the creature's night). The engine copes with restarts either way.
- **Battery:** a laptop at 100% charge 24/7 wears the battery, and old batteries can swell. Check it now and then. If the BIOS has a charge limit or "battery care" option, turn it on; otherwise consider removing the battery.
- **Heat:** keep it on a hard surface with the vents clear; a stand or cooling pad helps with the lid closed.

**Verify Ollama** (from inside the stack, since the port is not published):
```
docker compose exec ollama ollama list
docker compose run --rm engine python scripts/check_ollama.py
```

---

## 15. Deployment

Docker Compose. See `README.md` for the full walkthrough.

- **Image:** one slim `python:3.12-slim` image (`Dockerfile`, targets `test` and `runtime`), non-root user (uid/gid from `CAT_UID`/`CAT_GID`, default 1000, so it can write `./data`). The `engine` and `web` services use it with different commands. `ollama` is the official image with the NVIDIA GPU reservation.
- **Mounts:** `./data` is a bind mount (database, backups, logs) so it can be inspected and backed up from the host. `./config` is mounted **read-only**, so YAML can be edited without rebuilding (restart the engine to apply). The Ollama models live in a named volume.
- **Ports:** only the website (`${WEB_PORT:-8080}` → 8080) is published. Ollama is reachable only as `http://ollama:11434` inside the compose network.
- **Start order:** `engine` starts after `ollama` *starts* but is **not** gated on its health. If Ollama is down, or the models are not pulled yet, the engine starts anyway and dozes (§12). The brain only counts as online when Ollama answers and every configured model is pulled (`health_require_models`).
- **Healthchecks:** `ollama` runs `ollama list`; `web` serves `/healthz`; the engine rewrites `data/engine.heartbeat` every loop iteration and `python -m cat.healthcheck engine` checks its age. All services `restart: unless-stopped`.
- **Models:** `deploy/pull-models.sh` (the `model-pull` compose service, profile `init`) pulls the models named in `settings.yaml` into the Ollama volume. Safe to re-run.
- **Backup:** `deploy/backup.sh` runs `sqlite3 .backup` into `./data/backups` (safe while the engine runs), integrity-checks it, and keeps the newest `backup_keep` copies; run it from host cron. Needs `sqlite3` on the host.
- **Update:** `deploy/update.sh` pulls the code, rebuilds the image and restarts the services; data and models are untouched.
- **Logs:** `docker compose logs -f engine` (JSON log files in `data/logs/` come with the dev dashboard work).

## 16. Build order

1. Skeleton, config, schema, mock backend. ✔
2. World and character. ✔
3. Effects. ✔
4. Ollama brain and narrator. ✔
5. Memory. ✔
6. Sleep. ✔
7. Offline handling. ✔
8. Website. ✔
9. **Docker Compose deployment (phase 2, part A).** ✔
10. **Dev dashboard (phase 2, part B).** ✔

**Done for v1:** it runs for 7 days unattended, the diary reads like a life, and nothing in the world appears or disappears without an effect causing it.

---

## 17. Tracing and the dev dashboard

**Tracing** (`cat/trace.py`): every tick is fully explainable. While a tick runs the engine hands the tracer everything it does; after the tick commits, the tracer writes it in its own transaction. Tracing never breaks the creature (a failed write is a warning). What is stored: stage timings; every LLM *attempt* with the exact system prompt, user prompt, JSON schema, raw response, parsed JSON, token counts and Ollama's own durations; embedding calls (with their purpose); the full recall scoring; every proposed effect with its accept/reject reason; the per-need arithmetic (before, decay, bucket, raw gain, repetition multiplier, effort, clamp, after) and the mood inputs; event rolls; and the sleep steps (LLM output, proposed vs clamped). `trace_level: summary` drops text, `trace_retention_days` prunes the large tables during sleep. Structured JSON logs go to stdout and `data/logs/`.

**Dev commands** (`cat/devqueue.py`, `cat/devcontrol.py`): the dashboard never writes game state. It inserts rows into `dev_commands` in `data/devdash.db`; the engine polls them (every `dev_poll_seconds`, also while paused) and runs pause / resume / step / set_interval / force_sleep / reload_settings / inject_event / snapshot, publishing `engine_status` and the loaded config back to the same file. Keeping these in a separate database means `cat.db` has exactly one writer.

**Dashboard** (`cat/devdash/`): server-rendered Jinja + a little vanilla JS, vendored uPlot for charts, no CDN. Pages: Overview, Ticks, Tick detail, LLM calls, Memory & embeddings, Needs & mood, Behaviour, Sleep, World, Logs, Config, Playground; each has a JSON twin under `/api/dev/`. The playground replays stored prompts against Ollama and saves the runs in `devdash.db`; replays are never applied to the world. Access: loopback by default; LAN exposure needs Basic auth (the service refuses to start without credentials); POSTs need `X-Requested-With: devdash` and explicit confirmation.

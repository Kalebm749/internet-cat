# Internet Cat

A framework for a conscious entity that lives in a simulated world. Plain code tracks time, needs and
rules; a local LLM (Ollama) writes what the entity thinks and what happens. Nothing assumes a species,
body or appearance: the entity starts with no self-image and may learn one while it sleeps. Everything
runs on **one Ubuntu laptop** with **Docker Compose**, and a read-only website shows its journal.

```
┌──────────────────── your laptop (Docker Compose) ────────────────────┐
│  ollama (GPU)  ◄──  engine (tick loop, the only writer)              │
│                         │                                            │
│                         ▼                                            │
│                 data/cat.db (SQLite)  ◄──  web  :8080  ◄── browsers  │
│                         ▲                                            │
│                         └───────────  devdash 127.0.0.1:8090 (you)   │
└──────────────────────────────────────────────────────────────────────┘
```

## Docs
- [`docs/creature-architecture.md`](docs/creature-architecture.md): the concepts (brain, narrator, needs, mood, memory, sleep)
- [`docs/server-architecture.md`](docs/server-architecture.md): how it is built and deployed, and the host setup
- [`docs/web-gui.md`](docs/web-gui.md): the website
- [`docs/config-reference.md`](docs/config-reference.md): every config field (`character.yaml`, `world.yaml`, `settings.yaml`)
- [`docs/phase-2-docker-and-devdash.md`](docs/phase-2-docker-and-devdash.md): the plan for this deployment and the dev dashboard

---

## 1. Host prerequisites (one-time, on the laptop)

These need `sudo` and change the machine, so they are written down here for you to run yourself.
Tested target: Ubuntu, HP Omen 15, GTX 1060 6GB, 16GB RAM.

**NVIDIA driver, Docker, GPU in containers**
```bash
sudo ubuntu-drivers install && sudo reboot          # NVIDIA driver
nvidia-smi                                          # should list the GTX 1060 (6144MiB)

# Docker Engine + the compose plugin: follow https://docs.docker.com/engine/install/ubuntu/
docker compose version                              # must work (compose v2 plugin)

# NVIDIA Container Toolkit: follow https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# GPU test. Use a current "-base" tag from https://hub.docker.com/r/nvidia/cuda/tags (example below)
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```
If you ever installed Ollama natively, turn that off. The container needs the GPU and port 11434:
```bash
sudo systemctl disable --now ollama
```

**Keep the laptop running 24/7**
```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```
Lid closed = keep running: in `/etc/systemd/logind.conf` set
```
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
```
then `sudo systemctl restart systemd-logind`. To avoid surprise reboots keep
`Unattended-Upgrade::Automatic-Reboot "false";` in `/etc/apt/apt.conf.d/50unattended-upgrades`
(the creature copes with restarts either way; see "Downtime" below).
Give the laptop a fixed IP (a DHCP reservation in your router) and prefer wired Ethernet.

**Firewall: only the website, only from your network** (adjust the subnet)
```bash
sudo ufw allow OpenSSH                                            # if you use SSH, so enabling ufw can't lock you out
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
sudo ufw enable
```
Docker publishes ports by editing iptables directly, which can **bypass ufw**. Test from another
machine that only 8080 answers (and not 11434). Never port-forward 8080 or 11434 on your router.

**Battery and heat.** A laptop at 100% charge 24/7 wears its battery, and old batteries can swell.
If the BIOS has a charge limit / "battery care" option, turn it on; otherwise consider removing the
battery. Keep it on a hard surface with clear vents (a stand or cooling pad helps with the lid closed).

**For backups:** `sudo apt install sqlite3` (the backup script runs on the host).

---

## 2. First start

```bash
git clone <your-repo-url> internet-cat && cd internet-cat
cp .env.example .env          # then edit: CAT_UID / CAT_GID = output of `id -u` / `id -g`; TZ; WEB_PORT
mkdir -p data                 # create it yourself so it is owned by you, not root
docker compose up -d --build  # ollama + engine + web
deploy/pull-models.sh         # one-off: downloads the models named in config/settings.yaml (a few GB)
```
Until the models are pulled the engine is up but **dozing** (it says so in the log): that is
normal, not an error. Then check:
```bash
docker compose ps                                       # ollama, engine, web should become (healthy)
docker compose exec ollama ollama ps                    # after the first tick: PROCESSOR should say 100% GPU
docker compose logs -f engine                           # watch the creature live
docker compose run --rm engine python scripts/check_ollama.py   # tags + one chat + one embedding, with timings
```
Open **`http://<laptop-ip>:8080`** from any device on your network.

If you see `permission denied` on `/app/data`, `CAT_UID`/`CAT_GID` in `.env` don't match the owner of
`./data`; fix them and run `docker compose up -d --build` again.

---

## 3. Everyday use

| I want to… | Command |
|---|---|
| watch the engine | `docker compose logs -f engine` |
| stop / start everything | `docker compose stop` / `docker compose start` |
| apply a settings or character/world edit | edit `config/*.yaml`, then `docker compose restart engine` (no rebuild; `web` reads config at start too: `docker compose restart web`) |
| update to new code | `deploy/update.sh` (`--images` also pulls a newer Ollama image) |
| back up the database | `deploy/backup.sh` |
| change the model | edit `brain_model` etc. in `settings.yaml`, `deploy/pull-models.sh`, `docker compose restart engine` |
| read the diary in the terminal | `docker compose run --rm engine python scripts/show_diary.py -n 50` |
| run the tests in a container | `docker build --target test .` |

**Backups.** `deploy/backup.sh` makes a consistent copy while the engine runs
(`./data/backups/cat-YYYY-MM-DD.db`, newest `backup_keep` = 14 kept). Run it nightly from host cron:
```
15 4 * * *  /path/to/internet-cat/deploy/backup.sh >> $HOME/cat-backup.log 2>&1
```
**Restore** a backup:
```bash
docker compose stop engine web
cp data/backups/cat-2026-11-18.db data/cat.db && rm -f data/cat.db-wal data/cat.db-shm
docker compose start engine web
```
**Start the creature's life over:** stop `engine` and `web`, move `data/cat.db` (and its `-wal`/`-shm`) away, start again.

**Editing the character or world.**
- Changing `settings.yaml`, `prompts.yaml`, tendencies, needs or areas is picked up on restart (settings and prompts can also be reloaded live from the dev dashboard). If you add or remove a need or tendency, the saved state is adjusted at startup (a new need starts at its `start` value; a creature in a removed area goes back to the start area).
- World **objects** are seeded once, on the first start. Edits to the object list in `world.yaml` don't change an existing world: reset (above) to re-seed it.

**Downtime** (the laptop was off, or the engine stopped). `downtime_policy` in `settings.yaml`:
`pause` (default) resumes sim time exactly where it stopped and nothing decays; `catch_up` applies
the decay for the gap (capped at `max_catch_up_hours` simulated hours) and writes one
"dozed through the night" diary entry. Either way the engine survives Ollama being down: the
creature just *dozes* until the brain is back.

**Ports.** Only the website is published to your network (`WEB_PORT`, default 8080); the dev dashboard is published on 127.0.0.1 only (see "Using the dev dashboard"). Ollama is reachable only inside
the compose network. To use Ollama from the host for debugging, add `ports: ["127.0.0.1:11434:11434"]`
to the `ollama` service in `compose.yaml` (loopback only).

---

## 4. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Engine logs "models are missing" and the diary shows "dozing" | Run `deploy/pull-models.sh`. |
| Engine logs "brain/narrator unavailable" repeatedly | `docker compose logs ollama`; `docker compose ps`; GPU out of memory (try a smaller model such as `gemma3:4b`). |
| Ollama is slow / `ollama ps` doesn't say 100% GPU | The NVIDIA Container Toolkit step was skipped, or a native Ollama still holds the GPU (`sudo systemctl disable --now ollama`). |
| `docker compose up` fails with "could not select device driver nvidia" | Container Toolkit not configured: `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`. |
| Site unreachable from another device | ufw rule/subnet, or the laptop's IP changed (use a DHCP reservation). |
| Dev dashboard shows the engine as "not running" | The engine's status is stale or the engine is down: `docker compose ps`, `docker compose logs engine`. |
| `permission denied` writing `/app/data` | `CAT_UID`/`CAT_GID` in `.env` ≠ owner of `./data`. |
| Engine shows `unhealthy` | Its heartbeat file is stale: the loop is stuck or crashing. See `docker compose logs engine`. |

---

## Using the dev dashboard

A developer-only dashboard that shows **everything the system does**: every tick, every prompt and
model call, every embedding lookup, every rule the code applied, plus health and logs. The public
site is for watching the creature; this is for debugging and tuning it.

**Open it:** `http://127.0.0.1:8090` on the laptop (it starts with `docker compose up -d`). To use it from
another machine without exposing it, tunnel: `ssh -L 8090:127.0.0.1:8090 <laptop>`, then open
`http://127.0.0.1:8090` locally.

**Access rules** (set in `.env`, see `.env.example`):
- By default it is published on **127.0.0.1 only** (`DEVDASH_BIND=127.0.0.1`).
- To reach it from your LAN set `DEVDASH_BIND=0.0.0.0` **and** `DEVDASH_USER` / `DEVDASH_PASS`. It then requires HTTP Basic auth on everything except `/healthz`, and the service **refuses to start** if the credentials are missing. If credentials are set it asks for them even on loopback. Use a long password; Basic auth is not encrypted, so only do this on a network you trust, and never expose it beyond your local network.
- It can never change game state: it opens `cat.db` read-only. Its own writes go to a separate file, `data/devdash.db` (command queue, playground runs).
- State-changing requests (`POST`) must carry the header `X-Requested-With: devdash` (the pages add it; for `curl` add it yourself) and an explicit `"confirm": true`.

**Pages**

| Page | What it shows |
|---|---|
| **Overview** | engine state (running / paused / dozing / not running), current tick, sim time, interval, last tick duration, brain online; Ollama reachability, loaded models + VRAM, installed model versions; service health; last 24 h: ticks, doze ticks, errors, invalid-JSON rate, rejected-effect rate, brain/narrator latency (mean, p95), tokens per tick; the dev controls and recent commands |
| **Ticks** | every tick; filter by status, kind, area, action text, has rejections, has errors, slow ms |
| **Tick detail** | one tick as a timeline (including which need expressions the brain prompt used, and why): stage timing bar → the numbers the public site shows (each links to its math) → event rolls (chance vs roll, or why filtered) → recall (every candidate memory with recency / importance / relevance / score) → brain call → narrator call (exact system prompt, user prompt, schema, raw response, parsed JSON, tokens, timings, retries; each with a copy button) → effects (accepted ✓ / rejected ✗ with reason) → need math (before − decay + gain × repetition − effort + clamp = after) → mood math → memory writes and embedding calls → sleep cycle |
| **LLM calls** | every attempt, filterable by kind, model, validity, errors, latency; charts of latency p50/p95, tokens in/out and invalid-JSON rate over ticks or real time; each call has **Open in playground** |
| **Memory** | long- and short-term memories with times recalled; recall stats (most recalled, never recalled); a 2D PCA **embedding map** (colour = kind, size = importance, fainter = older); a **search box** that embeds your text and scores every memory exactly as the engine's recall would |
| **Needs & mood** | every need, the energy (a separate resource), valence and arousal (each −50..50) over sim time (24 h / 7 d / all) with sleep and doze periods shaded; the needs that sat at a critical level the longest |
| **Behaviour** | action variety per day, top repeated actions, repetition-penalty hits, time per area, effect ops histogram, rejection reasons |
| **Sleep** | each night: inputs, raw LLM outputs, **proposed vs clamped** tendency changes, new memories, preference changes; tendency history chart |
| **World** | areas, objects (tags, state, who made them), inventory, and each object's history (created / moved / eaten, by which tick) |
| **Logs** | tail of `data/logs/*.jsonl` (all services), filter by service, level, tick, text; **live tail**; a tick id links to Tick detail |
| **Config** | `character.yaml`, `world.yaml`, `settings.yaml` as the engine loaded them, a diff against the files on disk, and a *Reload settings* button |
| **Playground** | replay any stored call, edit the prompt / model / temperature, run it, compare with the original; run the same prompt on 2-3 models side by side |

**Dev controls** (Overview; each asks for confirmation). They are queued in `data/devdash.db` and run by the
engine within about 2 seconds (`dev_poll_seconds`); the dashboard never touches game state itself.
- **Pause / Resume / Step**: pause the tick loop, run exactly one tick, continue. A pause is not "downtime" (the engine keeps its last-alive time fresh), and it is not remembered across a restart.
- **Set interval**: change the tick interval for N ticks, or until reset/restart.
- **Force sleep cycle**: run consolidation, tendency drift and preference learning now (needs the brain online); it shows up on the Sleep page.
- **Reload settings**: re-read the YAML and apply `settings.yaml` live (`character.yaml` / `world.yaml` need a restart). Invalid files change nothing.
- **Inject event**: a one-shot world event (optionally spawning an object) on the next tick.
- **Snapshot DB**: an online backup to `data/backups/`.

From a terminal: `curl -s 'http://127.0.0.1:8090/api/dev/ticks?limit=5'`, and
`curl -s -X POST http://127.0.0.1:8090/api/dev/commands -H 'X-Requested-With: devdash' -H 'Content-Type: application/json' -d '{"command":"pause","confirm":true}'`.
Every page has a JSON twin under `/api/dev/...` (paginated, 50 by default).

**What gets recorded, and how long.** Every tick writes a trace to `cat.db`: exact prompts and raw responses, embedding
calls, recall scoring, effect validation, need and mood arithmetic, event rolls, sleep steps, stage timings.
- `trace_level: full` (default) keeps prompt and response text; `summary` keeps only the numbers (timings, tokens, rejections, math).
- `trace_retention_days` (default 14): during each sleep cycle the big tables (`llm_calls`, `embed_calls`, `recall_logs`) older than this are pruned. The small ones (`tick_traces`, `need_math`, `effect_logs`, ...) are kept. Prompts are the bulk: roughly 6-8 KB per tick with the real prompts, so a 14-day window at the default pace is on the order of 100 MB.
- `trace_enabled: false` turns tracing off. Tracing never breaks the creature: a failed trace write is a logged warning.
- Logs are JSON lines on stdout (`docker compose logs engine`) **and** in `data/logs/<service>.jsonl` (size-rotated).

**Where does a number on the public site come from?** Open the site, note the tick, then Ticks → that tick →
"What the public site shows": every need, the energy, the mood, the diary text and the change chips link to the exact
arithmetic and the exact prompts that produced them.

**Prompt playground.** From any call (LLM calls → *Open in playground*), edit the system / user prompt, the JSON schema,
model, temperature or `num_ctx`, and **Run**. Tick 2-3 models to compare them side by side with timings and token counts.
Replays are **never applied to the world**; they are saved in `devdash.db` (Playground → all runs). Ollama serves one request at a time, so
the engine's own calls wait while a replay runs.

**Without Docker** (develop the dashboard on fake data):
```bash
.venv/bin/python scripts/seed_db.py --db data/seed.db --force   # 120 mock ticks, a doze, sleep nights, a forced sleep, commands, logs
.venv/bin/python -m cat.devdash --db data/seed.db               # http://127.0.0.1:8090
.venv/bin/python -m cat.web --db data/seed.db                   # the public site, for comparison
```

---

## 5. Development (no Docker, no GPU needed)

Everything runs and is tested with a mock brain.
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                              # the whole suite
.venv/bin/python -m cat.main --mock --ticks 100               # watch a mock run
.venv/bin/python scripts/seed_db.py --db data/seed.db --force  # ~120 mock ticks with sleep, a doze, keepsakes
.venv/bin/python -m cat.web --db data/seed.db                  # the site at http://127.0.0.1:8080
.venv/bin/python scripts/add_ticks.py --db data/seed.db --ticks 3   # watch the site update live
```
Against a real Ollama on this machine: `.venv/bin/python -m cat.main --ollama-host http://127.0.0.1:11434 --ticks 50 --interval 0`,
then `.venv/bin/python scripts/show_diary.py -n 50`. See `CLAUDE.md` for conventions.

## Layout
```
compose.yaml  Dockerfile  .env.example     deployment (ollama, engine, web, devdash)
config/       character.yaml  world.yaml  settings.yaml   (PLACEHOLDER content: replace)
cat/          engine, brain, narrator, effects, memory, sleep, trace, web/, devdash/ ...
deploy/       backup.sh  update.sh  pull-models.sh
scripts/      seed_db.py  add_ticks.py  show_diary.py  check_ollama.py  reembed.py
data/         cat.db, devdash.db, logs/, backups/   (bind mount, git-ignored)
```

# Internet Cat

**Nit** lives in a small simulated square: an old fountain and a market stall. Nit is a humanoid creature, black
all over, curious and truthful. Every ~15 real seconds a small local model decides what Nit does next, choosing
from the actions Nit *knows* at that place. Code carries the action out and owns every number. At night a larger
model goes over the day: it forms memories, confirms what Nit learned, finds likes and dislikes, and, very slowly
and only on evidence, lets Nit become someone a little different. Visitors can leave letters in the mailbox at
the market stall; Nit reads them when it chooses, and may write back.

One real hour is one day for Nit. Everything runs on **one Ubuntu laptop** with **Docker Compose**.

```
┌──────────────────── your laptop (Docker Compose) ────────────────────────┐
│  ollama (GPU)  ◄──  engine (python -m cat.nit, the only writer of nit.db) │
│                        │      ▲ imports letters                           │
│                        ▼      │                                           │
│                  data/nit.db  data/mail.db ◄── web :8080 ◄── visitors     │
│                        ▲                                                  │
│                        └──── devdash 127.0.0.1:8090 (you, read-only)      │
└───────────────────────────────────────────────────────────────────────────┘
```

## Docs
- [`docs/rewrite-contracts.md`](docs/rewrite-contracts.md): how it all works (identity, body, decisions, world and
  actions, sleep and memory, repetition, models, the dashboard): the design and each step as built
- `content/`: the world and the character sheet, in YAML. Growing the world is editing these files (below).
- `docs/archive/`: the design documents of the first engine (replaced in November 2026), kept for reference

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
docker compose up -d --build  # ollama + engine + web + devdash
deploy/pull-models.sh         # one-off: qwen3:4b (decisions), qwen3:14b (sleep), nomic-embed-text (memory), ~12 GB
```
Until the models are pulled the engine is up but **dozing** (it says so in the log): that is normal, not an
error. The first start creates a **new life** in `data/nit.db` (Nit at the fountain, 07:00 on day 1). Then check:
```bash
docker compose ps                                       # ollama, engine, web, devdash should become (healthy)
docker compose exec ollama ollama ps                    # after the first decision: PROCESSOR should say 100% GPU
docker compose logs -f engine                           # one line per decision; "the night's sleep cycle is done"
docker compose run --rm engine python scripts/check_ollama.py   # tags + one decision-style call + one embedding
```
Open **`http://<laptop-ip>:8080`** from any device on your network, and the dev dashboard at
**`http://127.0.0.1:8090`** on the laptop.

If you see `permission denied` on `/app/data`, `CAT_UID`/`CAT_GID` in `.env` don't match the owner of
`./data`; fix them and run `docker compose up -d --build` again.

**Coming from the first engine?** Its life is in `data/cat.db`. The new engine never reads it: it starts a new
life in `data/nit.db`. Keep `cat.db` as a keepsake or move it into `data/backups/`.

---

## 3. Everyday use

| I want to… | Command |
|---|---|
| watch the engine | `docker compose logs -f engine` |
| stop / start everything | `docker compose stop` / `docker compose start` |
| change the world or the character | edit `content/*.yaml` (check it on the dashboard's **Content** page), then `docker compose restart engine web devdash` |
| change tuning, prompts or models | edit `config/settings.yaml` or `config/prompts/*.yaml`, then `docker compose restart engine` |
| see how the real model is doing | `docker compose exec engine python scripts/measure_nit.py` (below) |
| update to new code | `deploy/update.sh` (`--images` also pulls a newer Ollama image) |
| back up the life | `deploy/backup.sh` |
| run the tests in a container | `docker build --target test .` |

**Measuring a real run.** After a couple of real hours (two of Nit's days), run
`docker compose exec engine python scripts/measure_nit.py` (`--json` for machine-readable output). It only reads.
It reports:
- **Speed:** decision latency (median, 90th percentile, worst) against the 15 s target.
- **Validity:** how many answers needed a retry, how many fell back, and the most common problems.
- **Prompt:** prompt size against its token budget, and how often context was dropped to fit.
- **List bias:** whether the position of an option in the shuffled menu steers the pick.
- **Hunger:** whether Nit eats when it is hungry and food is on the menu.
- **Nights:** whether each night's sleep cycle finished, and how long each 14B stage took in real seconds.
- **Behaviour:** variety, freshness, restarts, repeated thoughts, rejected decisions, what Nit has learned and
  remembered, and character changes.
- **Letters:** how many were imported, read and answered.

**Growing the world.** Every place is one file in `content/locations/` with its facts, events and actions. A new
place is a new file plus an exit from an existing place; a new action is a few lines of YAML built from the
existing conditions and world operations. The engine checks every file at start-up and refuses to start with a list
of every problem (file and line). Only a new *kind* of thing needs code: a new world operation, condition, or
capability (drawing, music, web search). See `docs/rewrite-contracts.md` §1 and §4.
- A fact the creature changed (a wish it made, an offering it ate) keeps its value when you edit the content; a
  fact the world changed keeps its value unless you change that fact's default.
- `character.yaml` seeds the character once, at the first start. After that only Nit's own development changes its
  traits and tendencies; the `core` (nature, senses, boundaries) always follows the file, and an edit to it is
  recorded as a new character version.

**Letters.** Visitors write on the website; letters go straight into the mailbox at the market stall (after
automatic checks: length, a blocklist `web.mail_blocklist`, and rate limits per visitor and overall). Nit reads them
when it chooses, may act on what they ask, and may reply. Replies go onto an answered pile that you can read on the
dashboard's **Mail** page; they are not shown to visitors yet. The website's only write is `data/mail.db`.

**Backups.** `deploy/backup.sh` makes a consistent copy while the engine runs (`./data/backups/nit-YYYY-MM-DD.db`,
newest `backup_keep` = 14 kept). Run it nightly from host cron:
```
15 4 * * *  /path/to/internet-cat/deploy/backup.sh >> $HOME/cat-backup.log 2>&1
```
**Restore** a backup:
```bash
docker compose stop engine web devdash
cp data/backups/nit-2026-11-22.db data/nit.db && rm -f data/nit.db-wal data/nit.db-shm
docker compose start engine web devdash
```
**Start Nit's life over:** stop `engine`, move `data/nit.db` (and its `-wal`/`-shm`) away, start again.

**Updates never reset the creature.** Schema changes are numbered migrations (`cat/store.py`); before applying any to
an existing life the engine copies it to `data/backups/nit-<time>-pre-migration.db`, and if that copy fails it
migrates nothing. The website and the dashboard never read a life the engine has not migrated yet.

**Downtime** (the laptop was off, or the engine stopped): Nit's time pauses and resumes where it stopped. If Ollama
is down Nit *dozes*: time and any running activity go on, but no decisions are made until the brain is back.

**Ports.** Only the website is published to your network (`WEB_PORT`, default 8080); the dev dashboard is published on
127.0.0.1 only. Ollama is reachable only inside the compose network. To use Ollama from the host for debugging, add
`ports: ["127.0.0.1:11434:11434"]` to the `ollama` service in `compose.yaml` (loopback only).

---

## 4. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Engine logs "the brain is unavailable ... dozing" | `deploy/pull-models.sh`; `docker compose logs ollama`; `docker compose ps`. |
| Decisions are slow (see `measure_nit.py`) | `docker compose exec ollama ollama ps` should show the 4B model at 100% GPU. A 3B/1.7B model is a setting: `nit.models.decision_model`. |
| Nights take a long time; the clock waits at the waking moment | The 14B sleep model runs partly on the CPU of a 6 GB GPU. `measure_nit.py` shows each stage's real seconds. An 8B sleep model is a setting: `nit.models.sleep_model`. |
| The engine refuses to start with "content is invalid" | The message lists every problem with file and line; the dashboard's **Content** page shows the same. |
| Ollama is slow / `ollama ps` doesn't say 100% GPU | The NVIDIA Container Toolkit step was skipped, or a native Ollama still holds the GPU (`sudo systemctl disable --now ollama`). |
| `docker compose up` fails with "could not select device driver nvidia" | Container Toolkit not configured: `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`. |
| Site unreachable from another device | ufw rule/subnet, or the laptop's IP changed (use a DHCP reservation). |
| `permission denied` writing `/app/data` | `CAT_UID`/`CAT_GID` in `.env` ≠ owner of `./data`. |
| Engine shows `unhealthy` | Its heartbeat (`data/engine.heartbeat`) is older than 2 minutes: the loop is stuck. `docker compose logs engine`. |

---

## Using the dev dashboard

`http://127.0.0.1:8090` on the laptop. To use it from another machine without exposing it, tunnel:
`ssh -L 8090:127.0.0.1:8090 <laptop>`. It only reads `nit.db`; it never changes the life and never calls a model.
Every page has a JSON twin under `/api/dev/<page>`.

| Page | What it shows |
|---|---|
| **Overview** | time, place, activity, last thought, body words, the derived condition and the repetition score; mood, energy and hunger over the last 24 sim hours; engine heartbeat; models; recent decisions; the last night |
| **Decisions** | every decision; the **inspector** shows its triggers, the Mind (exactly what Nit had in front of it), the prompt, every attempt with its problems, and what came of it (activity, freshness and what lowered it, body changes) |
| **Character** | who Nit is now, every character version with its change and evidence, every development proposal with each check's verdict (accepted or not), preferences |
| **Memory** | core memories, long-term memories with their retention score, today's notable moments |
| **Knowledge** | what Nit knows it can do, where, when and how it learned it, in its own words |
| **Repetition** | freshness by action, recent activities and what made them stale, repeated thoughts |
| **World** | every fact (and whether Nit changed it), places, perceived events, capability use |
| **Sleep** | each night: every stage's status, real seconds and output |
| **Mail** | the mailbox, the answered pile with Nit's replies, imports |
| **Content** | validation of `content/` and of the `nit:` tuning |

**Access rules** (set in `.env`, see `.env.example`): published on **127.0.0.1 only** by default. To reach it from
your LAN set `DEVDASH_BIND=0.0.0.0` **and** `DEVDASH_USER` / `DEVDASH_PASS`: it then requires HTTP Basic auth on
everything except `/healthz` and **refuses to start** without credentials. Basic auth is not encrypted: only do this
on a network you trust, never beyond it.

---

## 5. Development (no Docker, no GPU needed)

Everything runs and is tested with a mock brain.
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                                    # the whole suite
.venv/bin/python -m cat.nit --mock --fast --hours 48                # two mock days in seconds -> data/nit-mock.db
.venv/bin/python scripts/measure_nit.py --db data/nit-mock.db       # the report, on the mock run
CAT_DB=data/nit-mock.db .venv/bin/python -m cat.web                 # the site at http://127.0.0.1:8080
.venv/bin/python -m cat.devdash --db data/nit-mock.db               # the dashboard at http://127.0.0.1:8090
.venv/bin/python scripts/nit_mind.py --know make_wish               # what Nit sees, and one (mock) decision
```
Against a real Ollama on this machine:
```bash
.venv/bin/python scripts/nit_mind.py --ollama-host http://127.0.0.1:11434   # one real decision, with timing
.venv/bin/python -m cat.nit --ollama-host http://127.0.0.1:11434           # the real loop, in real time
```
See `CLAUDE.md` for conventions.

## Layout
```
compose.yaml  Dockerfile  .env.example    deployment (ollama, engine, web, devdash)
content/      character.yaml  world.yaml  locations/*.yaml  actions/universal.yaml  capabilities.yaml
config/       settings.yaml (engine + the nit: tuning)   prompts/{decision,world,sleep}.yaml
cat/          loop, execute, decision, mind, scheduler, body, identity, knowledge, memories, sleepcycle,
              behaviour, content, conditions, ops, worldstate, store, capabilities/, mail, readmodel, web/, devdash/
deploy/       backup.sh  update.sh  pull-models.sh
scripts/      measure_nit.py  nit_mind.py  check_ollama.py  make_nit_portrait.py  make_placeholder_assets.py
data/         nit.db, mail.db, logs/, backups/   (bind mount, git-ignored)
```

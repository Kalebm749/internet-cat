# Internet Cat

An autonomous creature that lives in a simulated world. Plain code tracks time, needs and rules;
a local LLM (Ollama) writes what the creature thinks and what happens. Everything runs on **one
Ubuntu laptop** with **Docker Compose**, and a read-only website shows the creature's journal.

```
┌──────────────────── your laptop (Docker Compose) ────────────────────┐
│  ollama (GPU)  ◄──  engine (tick loop, the only writer)              │
│                         │                                            │
│                         ▼                                            │
│                 data/cat.db (SQLite)  ◄──  web  :8080  ◄── browsers  │
└──────────────────────────────────────────────────────────────────────┘
```

## Docs
- [`docs/creature-architecture.md`](docs/creature-architecture.md): the concepts (brain, narrator, needs, mood, memory, sleep)
- [`docs/server-architecture.md`](docs/server-architecture.md): how it is built and deployed, and the host setup
- [`docs/web-gui.md`](docs/web-gui.md): the website
- [`docs/config-reference.md`](docs/config-reference.md): every config field (`character.yaml`, `world.yaml`, `settings.yaml`)
- [`docs/phase-2-docker-and-devdash.md`](docs/phase-2-docker-and-devdash.md): the plan for this deployment and the dev dashboard (not built yet)

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
- Changing `settings.yaml`, tendencies, needs or areas is picked up on restart. If you add or remove a need or tendency, the saved state is adjusted at startup (a new need starts at its `start` value; a creature in a removed area goes back to the start area).
- World **objects** are seeded once, on the first start. Edits to the object list in `world.yaml` don't change an existing world: reset (above) to re-seed it.

**Downtime** (the laptop was off, or the engine stopped). `downtime_policy` in `settings.yaml`:
`pause` (default) resumes sim time exactly where it stopped and nothing decays; `catch_up` applies
the decay for the gap (capped at `max_catch_up_hours` simulated hours) and writes one
"dozed through the night" diary entry. Either way the engine survives Ollama being down: the
creature just *dozes* until the brain is back.

**Ports.** Only the website is published (`WEB_PORT`, default 8080). Ollama is reachable only inside
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
| `permission denied` writing `/app/data` | `CAT_UID`/`CAT_GID` in `.env` ≠ owner of `./data`. |
| Engine shows `unhealthy` | Its heartbeat file is stale: the loop is stuck or crashing. See `docker compose logs engine`. |

---

## 5. Development (no Docker, no GPU needed)

Everything runs and is tested with a mock brain.
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                              # the whole suite
.venv/bin/python -m cat.main --mock --ticks 100               # watch a mock run
.venv/bin/python scripts/seed_db.py --db data/dev.db --force  # ~120 mock ticks with sleep, a doze, keepsakes
.venv/bin/python -m cat.web --db data/dev.db                  # the site at http://127.0.0.1:8080
.venv/bin/python scripts/add_ticks.py --db data/dev.db --ticks 3   # watch the site update live
```
Against a real Ollama on this machine: `.venv/bin/python -m cat.main --ollama-host http://127.0.0.1:11434 --ticks 50 --interval 0`,
then `.venv/bin/python scripts/show_diary.py -n 50`. See `CLAUDE.md` for conventions.

## Layout
```
compose.yaml  Dockerfile  .env.example     deployment
config/       character.yaml  world.yaml  settings.yaml   (PLACEHOLDER content: replace)
cat/          engine, brain, narrator, effects, memory, sleep, web/ ...
deploy/       backup.sh  update.sh  pull-models.sh
scripts/      seed_db.py  add_ticks.py  show_diary.py  check_ollama.py  reembed.py
data/         cat.db, backups/   (bind mount, git-ignored)
```

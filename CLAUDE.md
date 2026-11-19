# Internet Cat

A framework for a conscious entity in a simulated world. Nothing in the code, config or docs assumes a
particular form, species, body or appearance; the current character (Gregorovitch) is one configuration.
The entity starts with no self-image and learns one during sleep. The cat 3D model work is paused: keep
the cat assets (`cat/web/static/img/cat*.png`, `scripts/make_placeholder_assets.py`) but leave them unused.
One Ubuntu laptop (GTX 1060 6GB) runs everything under
Docker Compose: **ollama** (the brain), **engine** (tick loop, SQLite, the only writer) and **web**
(read-only website). Plain code owns time, needs and rules; the LLM only describes.
There is no Raspberry Pi any more: do not add Pi, systemd or SD-card concerns.

## Docs (in `docs/`)
- `creature-architecture.md` — concepts (background)
- `behavior-guide.md` — how the whole simulation behaves (triggers, impacts), the tuning guide and how to reset
- `server-architecture.md` — how it is built and deployed, incl. host setup (wins for implementation)
- `web-gui.md` — the website (wins for the web)
- `config-reference.md` — every config field (`settings.yaml`, `character.yaml`, `world.yaml`, `prompts.yaml`)
- `phase-2-docker-and-devdash.md` — the plan for the Docker Compose deployment (part A, done) and the dev dashboard (part B, done)

Decided: "open actions, closed effects" (server-architecture §9). Need levels shape the brain's prompt
(band words for the most pressing needs), never its choices: code never picks, suggests or ranks actions
by need. Energy is a **separate resource** (-50..50: effort spends it, sleep restores it), never a need.
Mood is one number, a slider from -50 (negative) to 50 (positive), computed from need bands.

## Commands
```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                   # all tests (use MockBackend, no network)
.venv/bin/python -m cat.main --mock --ticks 100    # run the engine with the mock brain
```
Real brain outside Docker (needs Ollama reachable):
```
.venv/bin/python scripts/check_ollama.py --host http://127.0.0.1:11434   # tags + one chat + one embed
.venv/bin/python -m cat.main --ollama-host http://127.0.0.1:11434 --ticks 50 --interval 0
.venv/bin/python scripts/show_diary.py -n 50                             # read the diary
```
Website (read-only, never calls the LLM):
```
.venv/bin/python scripts/seed_db.py --db data/seed.db --force   # ~120 mock ticks incl. sleep + a doze stretch
.venv/bin/python -m cat.web --db data/seed.db                   # http://127.0.0.1:8080 (port from settings)
.venv/bin/python scripts/add_ticks.py --db data/seed.db --ticks 3 [--offline]   # watch it update live
.venv/bin/python scripts/make_placeholder_assets.py            # paused: regenerates the unused cat images + paper texture
node tests/js/live_updates.cjs    # optional browser-side check; needs `npm i --no-save jsdom`
```
Web layout: `cat/web/views.py` (all words/labels/phases), `app.py` (routes), `templates/`, `static/`.
Markup for live-added items lives in `<template>` elements in `index.html`; keep it in step with
`_diary_entry.html` and the server-rendered panels.

Dev dashboard (`cat/devdash/`, port 8090, loopback by default; see README "Using the dev dashboard"):
```
.venv/bin/python scripts/seed_db.py --db data/seed.db --force   # real engine + mock brain: ticks, sleep, commands, logs
.venv/bin/python -m cat.devdash --db data/seed.db               # http://127.0.0.1:8090
node tests/js/devdash_live.cjs                                  # optional browser-side check (needs `npm i --no-save jsdom`)
```

Docker Compose (don't run these for the user; document them):
```
cp .env.example .env && mkdir -p data
docker compose up -d --build        # ollama + engine + web
deploy/pull-models.sh               # one-off model download
deploy/backup.sh                    # sqlite3 .backup into data/backups (host cron)
docker compose config -q            # validate compose.yaml (safe, starts nothing)
```

## Conventions
- Python 3.11+ (image: 3.12-slim); keep dependencies light and pure-Python.
- All tuning numbers live in `config/settings.yaml`, never hard-coded.
- Only `cat/effects.py` changes the world on the narrator's behalf; rejected changes are logged.
- Each tick's apply+record step is one DB transaction (`db.transaction`); LLM calls happen outside it.
- The engine is the only writer of game state; `cat/web` is read-only and never calls the LLM.
- Everything must run and be tested with `MockBackend`; never require Ollama in tests.
- Tick order: events roll at tick start; the action's duration then advances the clock and decays needs in the apply step (event and dislike drains, then the narrator's gains, which only add).
- Projects (`cat/projects.py`, tables `projects` and `artifacts`) are formed only by the sleep cycle and advanced only by the narrator's validated `project` report; code owns the numbers (a tapering step, milestones, completion gains), and project work is exempt from the repetition penalty. Nothing in a project picks or ranks actions.
- The world director (`cat/director.py`) proposes at most one happening every few ticks; it must never be shown needs or mood, and code validates and applies its proposal as an event (`events_max_per_tick`, area cap, known weather, capped drains). Hand-written events in `world.yaml` stay as seeds and fallback.
- Fixed prompt text lives in `config/prompts.yaml`, never in code; entries and their `{placeholders}` are declared in `cat/prompts.py` and validated at startup.
- Placeholder content in `config/character.yaml` / `world.yaml` — the engine must work with any valid config.
- Don't run docker, sudo or deploy commands for the user; write the files and document the steps.
- No config field, prompt or doc may state what the entity is. Self-image lines (table `self_image`) are written only by the sleep cycle (`cat/sleep.py`), capped by `sleep.max_self_image_lines`; the web only reads them. The config validator (`cat/config.py`) reports every problem at once and refuses to start.
- **Tracing never breaks the creature** (`cat/trace.py`): every hook is wrapped, a failed trace write is a warning. Anything new in a tick that affects a number should also be traced (need/mood math, effects, event rolls) so Tick detail can explain it.
- **The dev dashboard never writes game state**: `cat.db` is opened `readonly=True` (a test greps for it); its writes go to `data/devdash.db` (`cat/devqueue.py`). Commands are run by the engine (`cat/devcontrol.py`).
- POSTs to devdash need the `X-Requested-With: devdash` header and `"confirm": true`. LAN exposure (`DEVDASH_BIND`) requires Basic auth (`cat/devdash/auth.py`).
- Dashboard pages render server-side from `cat/devdash/data.py`; every page has a `/api/dev/...` JSON twin. Charts use the vendored uPlot (`static/vendor/`), never a CDN.

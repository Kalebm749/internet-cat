# Internet Cat

Autonomous creature in a simulated world. One Ubuntu laptop (GTX 1060 6GB) runs everything under
Docker Compose: **ollama** (the brain), **engine** (tick loop, SQLite, the only writer) and **web**
(read-only website). Plain code owns time, needs and rules; the LLM only describes.
There is no Raspberry Pi any more: do not add Pi, systemd or SD-card concerns.

## Docs (in `docs/`)
- `creature-architecture.md` — concepts (background)
- `server-architecture.md` — how it is built and deployed, incl. host setup (wins for implementation)
- `web-gui.md` — the website (wins for the web)
- `config-reference.md` — every config field
- `phase-2-docker-and-devdash.md` — current work: Docker Compose (part A), then the dev dashboard (part B)

Decided: "open actions, closed effects" (server-architecture §9). Energy is **derived** from needs
(`energy_weight`), never a need itself.

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
.venv/bin/python scripts/seed_db.py --db data/dev.db --force   # ~120 mock ticks incl. sleep + a doze stretch
.venv/bin/python -m cat.web --db data/dev.db                   # http://127.0.0.1:8080 (port from settings)
.venv/bin/python scripts/add_ticks.py --db data/dev.db --ticks 3 [--offline]   # watch it update live
.venv/bin/python scripts/make_placeholder_assets.py            # regenerate placeholder portrait/texture
node tests/js/live_updates.cjs    # optional browser-side check; needs `npm i --no-save jsdom`
```
Web layout: `cat/web/views.py` (all words/labels/phases), `app.py` (routes), `templates/`, `static/`.
Markup for live-added items lives in `<template>` elements in `index.html`; keep it in step with
`_diary_entry.html` and the server-rendered panels.

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
- Tick order: events roll at tick start; the action's duration then advances the clock and decays needs in the apply step.
- Placeholder content in `config/character.yaml` / `world.yaml` — the engine must work with any valid config.
- Don't run docker, sudo or deploy commands for the user; write the files and document the steps.

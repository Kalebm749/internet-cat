# Internet Cat

Autonomous creature in a simulated world. Runs on a Raspberry Pi 3B+ (engine + read-only website, SQLite);
the "brain" is a remote Ollama server. Plain code owns time, needs and rules; the LLM only describes.

## Docs (in `docs/`)
- `creature-architecture.md` — concepts (background)
- `pi-architecture.md` — how to build it (wins for implementation)
- `web-gui.md` — the website (wins for the web)
- `laptop-brain.md` — Ollama server setup (no code)
- `config-reference.md` — every config field

Decided: "open actions, closed effects" (pi-architecture §9). Energy is **derived** from needs
(`energy_weight`), never a need itself.

## Commands
```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                   # all tests (use MockBackend, no network)
.venv/bin/python -m cat.main --mock --ticks 100    # run the engine with the mock brain
```

Real brain (needs Ollama reachable at `ollama_host`):
```
.venv/bin/python scripts/check_ollama.py                  # tags + one chat + one embed
.venv/bin/python -m cat.main --ticks 50 --interval 0      # real brain, 50 ticks
.venv/bin/python scripts/show_diary.py -n 50              # read the diary
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

## Conventions
- Python 3.11+, pure-Python deps only (must pip install on a Pi without compiling).
- All tuning numbers live in `config/settings.yaml`, never hard-coded.
- Only `cat/effects.py` changes the world on the narrator's behalf; rejected changes are logged.
- Each tick's apply+record step is one DB transaction (`db.transaction`); LLM calls happen outside it.
- The engine is the only DB writer; `cat/web` is read-only and never calls the LLM.
- Everything must run and be tested with `MockBackend`; never require Ollama in tests.
- Tick order: events roll at tick start; the action's duration then advances the clock and decays needs in the apply step.
- Placeholder content in `config/character.yaml` / `world.yaml` — engine must work with any valid config.

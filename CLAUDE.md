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

## Conventions
- Python 3.11+, pure-Python deps only (must pip install on a Pi without compiling).
- All tuning numbers live in `config/settings.yaml`, never hard-coded.
- Only `cat/effects.py` changes the world on the narrator's behalf; rejected changes are logged.
- Each tick's apply+record step is one DB transaction (`db.transaction`); LLM calls happen outside it.
- The engine is the only DB writer; `cat/web` is read-only and never calls the LLM.
- Everything must run and be tested with `MockBackend`; never require Ollama in tests.
- Placeholder content in `config/character.yaml` / `world.yaml` — engine must work with any valid config.

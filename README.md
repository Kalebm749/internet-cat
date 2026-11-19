# Internet Cat

An autonomous creature that lives in a simulated world. Plain code tracks time, needs and rules;
a local LLM (Ollama) writes what the creature thinks and what happens. Everything runs on one
Ubuntu laptop with Docker Compose, and a read-only website shows its journal.

> Status: the engine, memory, sleep and website are built and tested with a mock brain. The Docker
> Compose deployment is being added (see `docs/phase-2-docker-and-devdash.md`); this README gets
> the full setup walkthrough in milestone A4.

## Docs
- [`docs/creature-architecture.md`](docs/creature-architecture.md): the concepts
- [`docs/server-architecture.md`](docs/server-architecture.md): how it is built, deployed and the host setup
- [`docs/web-gui.md`](docs/web-gui.md): the website
- [`docs/config-reference.md`](docs/config-reference.md): every config field

## Try it now (no Docker, no GPU)
```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
.venv/bin/python -m cat.main --mock --ticks 100
.venv/bin/python scripts/seed_db.py --db data/dev.db --force && .venv/bin/python -m cat.web --db data/dev.db
```

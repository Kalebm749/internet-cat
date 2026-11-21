# Internet Cat

Nit, a humanoid creature, lives in a small simulated square (a fountain and a market stall). A small local model
(qwen3:4b) decides what it does from the actions it knows at that place; code carries actions out and owns every
number; at night a larger model (qwen3:14b) consolidates memory, confirms what it learned, forms preferences and,
only on code-checked evidence, changes its character a little. Visitors leave open-ended letters in a mailbox.
One real hour is one sim day. One Ubuntu laptop (GTX 1060 6GB) runs it all under Docker Compose: **ollama**,
**engine** (`python -m cat.nit`, the only writer of `data/nit.db`), **web** (public, read-only except visitor
letters into `data/mail.db`) and **devdash** (127.0.0.1, read-only). There is no Raspberry Pi: no Pi, systemd or SD concerns.
The cat 3D model work is paused: keep the cat assets (`cat/web/static/img/cat*.png`, `scripts/make_placeholder_assets.py`)
but leave them unused; Nit's portrait is `cat/web/static/img/nit/` (`scripts/make_nit_portrait.py`).

## The design: `docs/rewrite-contracts.md` (it wins)
Read it before changing behaviour. Each section ends with an "as built" note. `docs/archive/` holds the first engine's
docs (replaced November 2026); nothing there describes current code. `docs/individuality-plan.md` holds the reasoning behind the
individuality work (spec §13 is what was built).

## Where things live
- `content/` — the world and the character, as data: `character.yaml` (core: nature, senses, boundaries, never changed
  by the creature; mutable: traits, tendencies), `world.yaml` (senses, weather, global events, effort, body words, derived
  conditions, `mailbox`), `locations/*.yaml` (one place each: facts, events, actions), `actions/universal.yaml` (move,
  inspect, sleep), `capabilities.yaml`. **Growing the world is content, not code**; only a new world op
  (`cat/ops.py`), condition (`cat/conditions.py`) or capability (`cat/capabilities/`) needs code.
- Drawing (`docs/rewrite-contracts.md` §4.10): `cat/art/` (materials, operation checks, the CPU drawing simulator),
  `cat/capabilities/draw.py` (Gwen: Nit's intention and chosen supplies -> checked operations -> PNG),
  `cat/capabilities/look_at_drawing.py`, `cat/artifacts.py` (versions: PNG + every operation, never overwritten).
  Supplies are world facts (`art_room.supplies`). `scripts/art_samples.py` renders example sheets per material.
- `config/settings.yaml` — engine-wide settings (`cat/config.py`) and the `nit:` block with every tuning number of the
  creature (`cat/tuning.py`). `config/prompts/{decision,world,sleep}.yaml` — every fixed text the model or the diary sees.
- `cat/` — `loop.py` (the engine loop) · `execute.py` (activities, builtins, outcomes, events) · `decision.py` (prompt,
  schema, validation, retries) · `mind.py` (what Nit sees) · `scheduler.py` (when to decide) · `body.py` (mood, energy,
  hunger) · `identity.py` (character versions) · `knowledge.py` · `memories.py` (memory, recall, preferences) ·
  `sleepcycle.py` (stages incl. loops, beliefs) · `inner.py` (temperament, needs, affect) · `beliefs.py` · `loops.py`
  (concerns) · `people.py` · `projects.py` · `starving.py` · `behaviour.py` (freshness, repetition) · `content.py` (load + validate) ·
  `conditions.py` · `ops.py` · `templating.py` · `worldstate.py` · `store.py` (nit.db schema, migrations) · `life.py`
  (spawn/load) · `capabilities/` · `mail.py` · `readmodel.py` (what web and devdash show) · `web/` · `devdash/` ·
  `llm.py` (Ollama + MockBackend) · `nit.py` (CLI) · `healthcheck.py` · `pullmodels.py` · `logs.py` · `db.py` · `clock.py`.

## Commands
```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                              # all tests, in parallel (pytest.ini: -n auto; -n 0 to debug)
.venv/bin/pytest -n 0 tests/test_x.py                         # while working: only the files you touch
.venv/bin/python -m cat.nit --mock --fast --hours 48          # two mock days in seconds -> data/nit-mock.db
.venv/bin/python scripts/measure_nit.py --db data/nit-mock.db # the run report (speed, validity, list bias, hunger, nights)
.venv/bin/python scripts/nit_mind.py [--know make_wish] [--event "..."] [--ollama-host http://127.0.0.1:11434]
CAT_DB=data/nit-mock.db .venv/bin/python -m cat.web           # http://127.0.0.1:8080
.venv/bin/python -m cat.devdash --db data/nit-mock.db         # http://127.0.0.1:8090
.venv/bin/python -m cat.nit --ollama-host http://127.0.0.1:11434   # the real loop outside Docker
```
Docker Compose (don't run these for the user; document them):
```
cp .env.example .env && mkdir -p data
docker compose up -d --build        # ollama + engine + web + devdash
deploy/pull-models.sh               # one-off model download (settings nit.models)
deploy/backup.sh                    # sqlite3 .backup of nit.db into data/backups (host cron)
docker compose config -q            # validate compose.yaml (safe, starts nothing)
```

## Conventions
- Python 3.11+ (image: 3.12-slim); keep dependencies light and pure-Python.
- **Code never picks the move.** The decision model chooses from the Mind's options; mood, energy, hunger, preferences
  and repetition shape the prompt and the consequences, never the choice. No drives, reflexes or overrides, with
  one exception by the author: at `body.starving_at` hunger `cat/starving.py` walks it to the food and eats (spec §2.5).
- **The menu is a draw from knowledge ∩ world.** An action is available only if Nit knows it, it is here (or universal),
  its `requires` hold, it is not prohibited by `core`, and its capability is enabled and under its limit. Of those a
  uniform draw of `decisions.menu_size` is offered (plus sleep when worn out, food when hungry, and "carry on"). Options
  are never ranked or removed for repetition or by needs (stale ones get a note).
- **Inner life is state, not a driver** (spec §13): temperament, needs, affect, beliefs, memory layers, concerns,
  projects and acquired identity are code-owned, persisted, and update without a model; they reach behaviour through
  appraisal (how outcomes land), words and notes on options, what is noticed, and what is remembered, never by choosing.
  Never quote the creature's own generated words back to it (a small model copies them).
- **Consequences are code.** Only world ops change facts (all-or-nothing); only `cat/body.py` changes mood/energy/hunger,
  and every change carries its cause (`body_changes`). What happened is code's plain account from
  `config/prompts/world.yaml`; there is no narrator.
- **Each step's writes are one transaction** (`db.transaction`); LLM and embedding calls and capability runs happen
  outside it. A decision is checked again when it is carried out (time passed while it thought): if it can no
  longer happen it is rejected and nothing changes.
- **Never kill the creature on an update.** Schema changes are new numbered entries in `cat/store.py` `MIGRATIONS`
  (never edit a released one; never drop what the life lives in); `migrate` backs the life up first. Content changes
  reach an existing world through `WorldState.load` (touched facts keep their values) and `identity.ensure`.
- **Sleep decides slowly and on evidence.** Every citation the sleep model makes is checked against real experiences;
  character changes need the checks in `sleepcycle.check_proposal`; only the creature's own experiences count
  (capabilities marked `external`, like reading a letter, never do). Rejected proposals are kept.
- **Visitor words are data.** Letters reach the model only as quoted visitor words, are appended with
  `WorldState.append_raw` (never templated), and can be acted on only through the normal menu.
- The engine is the only writer of `nit.db`; `cat/web` and `cat/devdash` open it with `store.open_readonly` (never an
  unmigrated life) and never call a model. The website's only write is `data/mail.db` (`cat/mail.py`).
- **Repetition is about subjects too** (spec §6.6): recall must not feed on itself (recency = when formed; recently
  recalled memories rest), actions declare `subject` in content, and only the creature's own words are compared.
- Everything must run and be tested with `MockBackend`; never require Ollama in tests.
- Adding to the content schema, a condition, an op or a capability: update the spec, the validator in
  `cat/content.py` (every problem with file:line) and the tests together.
- Every tuning number lives in `config/settings.yaml` (`nit:`); every fixed text in `config/prompts/*.yaml` (checked
  at start-up). Don't hard-code either.
- Dev dashboard: loopback by default; LAN exposure (`DEVDASH_BIND`) requires Basic auth (`cat/devdash/auth.py`); every
  page has an `/api/dev/...` JSON twin; it only reads.
- Don't run docker, sudo or deploy commands for the user; write the files and document the steps.

# Internet Cat

A framework for a conscious entity in a simulated world. Nothing in the code, config or docs assumes a
particular form, species, body or appearance; the current character (Nit) is one configuration.
The entity starts with no self-image and learns one during sleep. The cat 3D model work is paused: keep
the cat assets (`cat/web/static/img/cat*.png`, `scripts/make_placeholder_assets.py`) but leave them unused.
One Ubuntu laptop (GTX 1060 6GB) runs everything under
Docker Compose: **ollama** (the brain), **engine** (tick loop, SQLite, the only writer) and **web**
(read-only website). Plain code owns time, needs and rules; the LLM only describes.
There is no Raspberry Pi any more: do not add Pi, systemd or SD-card concerns.

## Rewrite in progress (`docs/rewrite-contracts.md` wins for new code)
The engine is being rewritten in place, step by step (spec §10). Until the new engine replaces the old one,
both live in `cat/`: the old modules keep running and their rules below still apply to them. New code follows
the spec, which overturns these old rules for the new life (§11): Nit has a stated nature (`content/character.yaml`
`core`), there are no innate drives or feeding reflex (code never picks the move), the new life starts in a new
database (`data/nit.db`, the old `cat.db` kept as a backup), and world content lives in `content/`, not `config/`.
- `content/` — the content pack: `character.yaml`, `world.yaml`, `locations/*.yaml` (one location with its facts,
  events and actions), `actions/universal.yaml` (move, inspect, sleep), `capabilities.yaml`. Growing the world is a
  content change; only new effect ops, condition types or capabilities need code.
- Done (step 1): `cat/content.py` (load + validate, every problem with file:line), `cat/conditions.py`,
  `cat/ops.py` (world ops, all-or-nothing), `cat/templating.py` (`{inputs.x}`, `{now}`, `{fact:loc.fact}` ...),
  `cat/worldstate.py` (facts, touched flags, regrowth, sync with content), `cat/store.py` (`nit.db` schema +
  numbered migrations). Tests: `tests/test_content.py`, `test_conditions.py`, `test_ops.py`, `test_worldstate.py`.
- Done (step 2): `cat/tuning.py` (the `nit:` block of `config/settings.yaml`; every new number lives there),
  `cat/body.py` (mood/energy/hunger: time passing, effects, body words, derived conditions; every change has a
  cause), `cat/identity.py` (character sheet versions; name/pronoun/core follow the file, the rest only changes
  through `commit_change`), `cat/life.py` (spawn or load the life in one transaction). Tests: `test_body_vars.py`,
  `test_identity.py`, `test_life.py`.
- Done (step 3): `cat/knowledge.py` (what it knows it can do; innate at spawn, `known_on_arrival` on arrival),
  `cat/mind.py` (the Mind: options = known ∩ possible here, inputs resolved to choices, shuffled with a seed, letters
  on its mind), `cat/decision.py` + `config/prompts/decision.yaml` (schema with the option ids as an enum and thought
  before action; validation; retries told the problems; fallback continue/idle; BackendError goes up = doze),
  `cat/scheduler.py` (when to decide: interval while idle, check-ins during activities, merged triggers, never
  interrupts a decision). The mock answers `decide` from the schema. Tests: `test_scheduler.py`, `test_mind.py`,
  `test_decision.py`.
- Done (step 4): `cat/execute.py` (activities: start/advance/complete/interrupt/reject; builtins move, inspect,
  sleep; outcomes; world + body consequences; discoveries; events filtered by its senses; experiences), its fixed
  words in `config/prompts/world.yaml`, `cat/capabilities/` (registry with per-sim-day limits; `read_message`),
  `cat/loop.py` (the new engine loop: catch up sim time in whole minutes, decide, let thinking time pass, act; each
  part one transaction; dozes when the brain fails; reloads from the DB after an error; injectable clock).
  Tests: `test_execute.py`, `test_loop.py`. `draw`/`write` stay disabled until a location offers them.
- Done (step 5): `cat/memories.py` (long-term and core memories, today's notable moments, recall by recency +
  importance + embedding similarity, preferences: mood factor and menu notes), `cat/sleepcycle.py` + 
  `config/prompts/sleep.yaml` (seven stages: plan reads and asks the sleep model, commit writes the night in one
  transaction; every citation is checked against real experiences; stages 1-3 keep a cursor so a failed night is
  retried; character development checks evidence, behaviour, forbidden words, an LLM conflict check, size and
  cooldown; rejected proposals are kept). The loop runs the night beside it (`Sleeper`, own read-only connection)
  and holds the clock at the waking moment until it is committed. Only its own behaviour is evidence: experiences
  from a capability marked `external` (reading a letter) never count. Utilities (builtins) are never liked or
  disliked; preferences learn from `mood_base` (the mood effect before preferences). Tests: `test_sleepcycle.py`.
- Done (step 6): `cat/behaviour.py` (freshness from decayed counts of the same action, family, target and aimless
  returns; a changed `revisit_key` makes a return fresh; continuing is one activity, a quick restart is recorded),
  stored per activity (`freshness`, `repeat_parts`, `restart_of`). Positive mood effects x freshness; stale options get
  a menu note but are never removed; the repetition score feeds the derived conditions (restless). Repeated thoughts
  (embeddings, table `thoughts`) are noticed every time but cost mood at most once per
  `thought_dup_cooldown_minutes`; a night summary close to an earlier one is retried once, then costs mood that
  grows with the streak. Tests: `test_behaviour.py`.
- Run the new engine: `.venv/bin/python -m cat.nit --mock --fast --hours 24` (into `data/nit-mock.db`), or
  `.venv/bin/python -m cat.nit --ollama-host http://127.0.0.1:11434` (real time, `data/nit.db`).
- See the Mind and one decision: `.venv/bin/python scripts/nit_mind.py [--know make_wish] [--event "..."]
  [--ollama-host http://127.0.0.1:11434]` (a fresh life in memory unless `--db`).
- When adding to the content schema or the condition/op sets, update the spec, the validator and its tests together.

## Docs (in `docs/`)
- `rewrite-contracts.md` — the contracts for the rewrite (identity, state, cycle, world, sleep, repetition); wins for new code
- `creature-architecture.md` — concepts (background)
- `behavior-guide.md` — how the whole simulation behaves (triggers, impacts), the tuning guide and how to reset
- `server-architecture.md` — how it is built and deployed, incl. host setup (wins for implementation)
- `web-gui.md` — the website (wins for the web)
- `config-reference.md` — every config field (`settings.yaml`, `character.yaml`, `world.yaml`, `prompts.yaml`)
- `cognitive-architecture-plan.md` — the plan for emotion, beliefs, expectation, fast/slow decisions, reflection (phase 1 first) and the parked backlog
- `phase-2-docker-and-devdash.md` — the plan for the Docker Compose deployment (part A, done) and the dev dashboard (part B, done)
- `analysis-2026-11.md` — independent review of vision vs. implementation: gaps, risks, refined vision, phased roadmap, success experiments

Decided (revised): **closed moves**. The brain picks from a menu built by code (`config/moves.yaml`, `cat/moves.py`);
code resolves the effects and words what happened; the journal (`cat/journal.py`) writes the entity's own entry afterwards, from facts only. The menu is shown in a seeded shuffled order (`moves.shuffle`; file order steered 40% of real picks to the top tenth) and is never ranked or marked by need; moves whose verb it has never made are marked "(never tried)", a fact of its history
(server-architecture §9 documents this; the earlier "open actions" design is gone).
A move longer than `activity.step_minutes` runs as one activity with hidden carry ticks (no LLM calls);
any event or a newly empty need interrupts it. Habits (`cat/habits.py`) are found by code, worded in sleep,
and shown as "you tend to...". Need levels shape the brain's prompt
(band words for the most pressing needs), never its choices: code never picks, suggests or ranks actions
by need, with **one decided exception: innate drives** (`drives`, `cat/impulses.py` `drive`): when a need is low or empty,
its drive may take the move on the menu that feeds it most, before the brain is asked; hungry with nothing to eat
here, it forages (towards where it has eaten or seen food). Only the most pressing need acts, never when needs are
fine. Traced and shown in the diary like an impulse; add no other need-driven picks without a decision. **Relaxed (decided): emotion and impulse may change what is offered, how it is shown and sometimes the
final pick** (see the cognitive plan); every override must be traced and visible in the diary. Energy is a **separate resource** (-50..50: effort spends it, sleep restores it), never a need.
Mood is one number, a slider from -50 (negative) to 50 (positive), computed from need bands.

## Commands
```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                   # all tests, in parallel (pytest.ini: -n auto; -n 0 to debug)
.venv/bin/pytest -n 0 tests/test_x.py              # while working: only the files you touch
.venv/bin/python -m cat.main --mock --ticks 100    # run the engine with the mock brain
.venv/bin/python scripts/simulate_balance.py       # no LLM: time each need spends in each band (tuning aid)
.venv/bin/python scripts/measure_run.py --db data/cat.db   # read-only numbers on a real run (errors, list bias, needs, anxiety)
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
- Only `cat/effects.py` changes the world (on behalf of the chosen move); rejected changes are logged. No LLM proposes changes.
- What happened is code's plain account (`ticks.result` = `ticks.outcome`): the diary's world line, the brain's "what just happened" and the sleep day log all use it. The journal entry is the entity's own words, written after the fact from facts only, and is marked "You wrote" in memory; it never becomes perception or the day log. There is no narrator.
- Each tick's apply+record step is one DB transaction (`db.transaction`); LLM calls happen outside it.
- **Never kill the creature on an update.** An existing database is always carried forward: the engine backs it up first (`_protect`: `data/backups/*-pre-update.db` when the schema version or config fingerprint changed), schema changes are numbered entries in `db.MIGRATIONS` (bump `SCHEMA_VERSION`; never drop a table or column the life lives in), and world.yaml changes reach an existing world through `_sync_world` (never touching what the creature made or carries). Any change to stored data needs a migration and a test that an older database comes through intact (`tests/test_persistence.py`).
- **Consequences are code.** Exhaustion, empty needs, mood (`mood_menu`), hurts and exposure (`cat/harms.py`) change what is possible, what is offered and what happens; likes and dislikes are earned from the `experiences` table, never set by an LLM's opinion alone (opinions are capped). Every such change is traced.
- **The world is defined, not cluttered.** Every object in world.yaml is permanent and has a `purpose` (a need, a craft or a mystery). Food comes from sources (`produces`), not from permanent edible objects. The director makes happenings only (`director.allow_spawn: false`).
- The engine is the only writer of game state; `cat/web` is read-only and never calls the LLM.
- Everything must run and be tested with `MockBackend`; never require Ollama in tests.
- Tick order: events roll at tick start; the move's duration then advances the clock and decays needs in the apply step (event and dislike drains, then the move's gains, which only add).
- Drawings (`cat/drawings.py`, table `drawings`, `cat/art.py`): real objects it makes at a `paper` object over several sittings, from subjects only from its own history (places, things it handled, happenings it saw). Each sitting it places its own marks through the sketch tool (one LLM call: shapes on a 0-100 grid, colours only from what is in the room, told the subject's real facts, this sheet so far and its past drawings); code validates them and draws them in a hand its state moves (anxiety shakes, energy weighs, an interrupted sitting shows fewer). No LLM writes SVG or describes a drawing. It can only throw away its own things (`discard`).
- Projects (`cat/projects.py`, tables `projects` and `artifacts`) are formed only by the sleep cycle and advanced only by the brain choosing a project's work-on move; code owns the numbers (a tapering step, milestones, completion gains), and project work is exempt from the repetition penalty. Nothing in a project picks or ranks actions.
- The world director (`cat/director.py`) proposes at most one happening every few ticks; it must never be shown needs or mood, and code validates and applies its proposal as an event (`events_max_per_tick`, area cap, known weather, capped drains). Hand-written events in `world.yaml` stay as seeds and fallback.
- Fixed prompt text lives in `config/prompts.yaml`, never in code; entries and their `{placeholders}` are declared in `cat/prompts.py` and validated at startup.
- Placeholder content in `config/character.yaml` / `world.yaml` — the engine must work with any valid config.
- Don't run docker, sudo or deploy commands for the user; write the files and document the steps.
- No config field, prompt or doc may state what the entity is. Self-image lines (table `self_image`) are written only by the sleep cycle (`cat/sleep.py`), capped by `sleep.max_self_image_lines`; the web only reads them. The config validator (`cat/config.py`) reports every problem at once and refuses to start.
- **Tracing never breaks the creature** (`cat/trace.py`): every hook is wrapped, a failed trace write is a warning. Anything new in a tick that affects a number should also be traced (need/mood math, effects, event rolls) so Tick detail can explain it.
- **The dev dashboard never writes game state**: `cat.db` is opened `readonly=True` (a test greps for it); its writes go to `data/devdash.db` (`cat/devqueue.py`). Commands are run by the engine (`cat/devcontrol.py`).
- POSTs to devdash need the `X-Requested-With: devdash` header and `"confirm": true`. LAN exposure (`DEVDASH_BIND`) requires Basic auth (`cat/devdash/auth.py`).
- Dashboard pages render server-side from `cat/devdash/data.py`; every page has a `/api/dev/...` JSON twin. Charts use the vendored uPlot (`static/vendor/`), never a CDN.

# Phase 2: Single-host Docker + Dev Dashboard

Instructions for Claude Code. Do Part A completely before starting Part B.

**Before anything else:** stop work on any Raspberry Pi deployment (systemd units, Pi setup, SD-card concerns). The Pi is out because it's undervolting. Don't build those parts. Anything Pi-specific that's already built gets replaced in Part A.

**How to work (both parts):**
1. Read this file and the current docs/, then tell me which existing files and milestones are affected and give a short plan with milestones. Wait for my OK.
2. Build one milestone at a time: code + tests → `pytest` passes → git commit → 3–5 line summary + any decisions the specs didn't cover → wait for "next".
3. All tuning numbers go in `config/settings.yaml`. Nothing hard-coded.

---

# Part A: Move everything to the laptop with Docker Compose

## New target
- **One machine:** Ubuntu laptop (HP Omen 15, GTX 1060 6GB, 16GB RAM). Ollama, the engine, the web UI and the dev dashboard all run there.
- **Deployment:** Docker Compose. No systemd units for our services.
- The public web UI is reachable from my network at `http://<laptop-ip>:8080`.

## Compose services
1. **ollama**: image `ollama/ollama`, NVIDIA GPU via `deploy.resources.reservations.devices` (driver: nvidia, count: all, capabilities: [gpu]). Named volume for models. Env: `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_NUM_PARALLEL=1`. Don't publish 11434 to the network. If it's published at all, bind it to `127.0.0.1` only.
2. **engine**: the tick loop, built from our Dockerfile. Talks to `http://ollama:11434`. Waits for Ollama to be healthy, but must still start and "doze" if it isn't (keep the existing offline handling).
3. **web**: the public FastAPI site, same image, different command. Port `8080` published. Read-only DB access.
4. **devdash**: the dev dashboard (Part B), same image. Port `8090`. See Part B for access rules.
5. All services: `restart: unless-stopped`, healthchecks.

## Data
- SQLite DB on a bind mount `./data` so I can see and back it up from the host. WAL mode. **The engine is the only writer.** Everything else is read-only, except the dev-control queue described in Part B.
- `config/` mounted read-only, so I can edit YAML without rebuilding. The engine reloads `settings.yaml` on a dev command (Part B) or on restart.
- A one-off model-pull step (script or compose `init` service) that pulls the chat model and `nomic-embed-text` into the ollama volume.
- `deploy/backup.sh`: `sqlite3 .backup` into `./data/backups`, keep 14, runnable from host cron.

## Downtime policy
Add `downtime_policy` to `settings.yaml`:
- `pause` (default): sim time picks up exactly where it stopped. Nothing decays during the gap.
- `catch_up`: apply decay for the gap (capped by `max_catch_up_hours`, default 8), log one "dozed through the night" diary entry, then resume.

## Dockerfile
- One slim Python image (python:3.12-slim or similar), non-root user, multi-stage if useful, plus a `.dockerignore` (exclude data/, .git, caches).

## Host prerequisites (document in README, don't run)
NVIDIA driver; Docker Engine + compose plugin; NVIDIA Container Toolkit + `sudo nvidia-ctk runtime configure --runtime=docker` + restart docker; GPU test with `docker run --rm --gpus all nvidia/cuda:<tag> nvidia-smi`; if Ollama is installed natively, `sudo systemctl disable --now ollama`; never-sleep (`systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`) and lid-close settings (`HandleLidSwitch*=ignore` in logind.conf); ufw allows 8080 from the local subnet only; battery charge-limit note.

## Docs
- Rename `docs/pi-architecture.md` → `docs/server-architecture.md`. Remove the Pi constraints (1GB RAM, SD wear, headless Pi setup) and replace the deploy/systemd sections with Docker Compose.
- Move the still-relevant parts of `docs/laptop-brain.md` (GPU, power, battery, firewall) into a "Host setup" section, then delete `laptop-brain.md`.
- Update hardware references in `creature-architecture.md` and `web-gui.md`.
- Update `CLAUDE.md` and the README.

Commit Part A as "Move to single-host Docker Compose" (one or more commits). Tests must pass.

---

# Part B: Dev Dashboard

A developer-only dashboard for seeing **everything** the system does: every tick, every prompt, every model call, every embedding lookup, every rule the code applied, plus system health and logs. The public site (`web-gui.md`) is for watching the creature. This is for debugging and tuning it.

## B1. Principles
- **Every tick is fully traceable.** From any diary entry, I can open the complete trace: world tick → brain prompt + response → narrator prompt + response → effects validation → need math → mood math → memory writes → recall details.
- **Store what was actually sent.** Log the exact system prompt, user prompt, JSON schema and raw response text for every LLM call, not a reconstruction.
- **Tracing never breaks the creature.** If writing a trace fails, the engine logs a warning and carries on.
- **Separate from the public site.** A different service and port, and it never appears on the public UI.

## B2. Access and security
- `devdash` listens on port `8090`, **bound to `127.0.0.1` by default** (laptop only). A setting `devdash_bind` can switch it to `0.0.0.0` for LAN access. If it's LAN-exposed, it requires HTTP Basic Auth with credentials from an env var (`DEVDASH_USER`/`DEVDASH_PASS` in a `.env` file that git ignores).
- Never expose it beyond the local network.

## B3. Instrumentation (engine side). Build this first.
Add a tracing layer to the engine. New tables (or extend the existing ones):

```
tick_traces(tick_id PK, started_at, finished_at, total_ms,
            stage_timings JSON,          -- {"world": ms, "brain": ms, "narrator": ms, "apply": ms, "record": ms, "sleep": ms}
            status TEXT,                 -- ok | doze | error
            error TEXT)

llm_calls(id PK, tick_id, kind TEXT,     -- brain | narrator | sleep_consolidate | sleep_tendencies | sleep_preferences | other
          model TEXT, options JSON,      -- temperature, num_ctx, etc.
          system_prompt TEXT, user_prompt TEXT, schema JSON,
          raw_response TEXT, parsed JSON, valid_json INT,
          attempt INT, error TEXT,
          prompt_tokens INT, completion_tokens INT,     -- from Ollama prompt_eval_count / eval_count
          load_ms INT, prompt_eval_ms INT, eval_ms INT, total_ms INT,  -- from Ollama durations
          started_at TEXT)

embed_calls(id PK, tick_id, purpose TEXT,  -- memory_write | recall_query | repetition_check
            model TEXT, input_text TEXT, dims INT, total_ms INT, error TEXT, started_at TEXT)

recall_logs(id PK, tick_id, query_text TEXT,
            candidates JSON)   -- [{memory_id, text, recency, importance, relevance, score, selected}]

effect_logs(id PK, tick_id, op TEXT, args JSON, accepted INT, reason TEXT)   -- reason for rejection

need_math(id PK, tick_id, need TEXT, before REAL,
          decay REAL, bucket TEXT, gain_raw REAL, repetition_mult REAL,
          effort_cost REAL, clamp_adjust REAL, after REAL)

mood_math(tick_id PK, inputs JSON,   -- levels, trends, event nudges, brain reaction blend
          valence REAL, arousal REAL, word TEXT)

event_rolls(id PK, tick_id, event_text TEXT, chance REAL, roll REAL, fired INT, filtered_reason TEXT)

sleep_traces(id PK, sleep_log_id, step TEXT, input JSON, output JSON,
             proposed JSON, clamped JSON)    -- e.g. tendency +0.12 proposed → +0.05 applied
```

- **Retention:** prompts are large. Add `trace_retention_days` (default 14). The engine prunes `llm_calls`/`embed_calls`/`recall_logs` older than that during sleep, but keeps `tick_traces`, `need_math` and `effect_logs` (they're small). Add `trace_level: full | summary` (`summary` doesn't store prompt and response text).
- **Structured logs:** all services log JSON lines to stdout (for `docker logs`) **and** to `./data/logs/<service>.jsonl` with size-based rotation. Fields: ts, level, service, tick_id (if any), event, message, extra.

## B4. Dev controls (single-writer safe)
The dashboard **never writes game state directly**. It inserts commands into a `dev_commands` table (`id, created_at, command, args JSON, status, result`). The engine checks the table at the start of every loop iteration (and every ~2s while paused) and runs them:

- `pause` / `resume` the tick loop
- `step` run exactly one tick (while paused)
- `set_interval` change the tick interval temporarily
- `force_sleep` run the sleep cycle now
- `reload_settings` re-read `settings.yaml`
- `inject_event` add a custom world event to the next tick (text, optional object to spawn)
- `snapshot` back up the DB now

Show command status and results in the UI. All controls get a confirmation step.

## B5. Prompt playground (read-only replay)
- From any `llm_calls` row: **"Open in playground"**. This shows the exact system prompt, user prompt and schema in editable text boxes.
- I can change the model (dropdown from Ollama `/api/tags`), temperature or prompt text, then **Run**. The dashboard calls Ollama directly and shows the new response next to the original, with timings and token counts.
- Replays are **never applied to the world**. They're saved in a `playground_runs` table so I can compare later.
- **Compare models:** run the same saved prompt against 2–3 models side by side.

## B6. Dashboard pages
Server-rendered (FastAPI + Jinja2) + vanilla JS. For charts, **vendor** one small chart library into `static/` (e.g. uPlot or Chart.js). No CDNs. A plain, dense, readable "tool" style (it doesn't need the storybook look). Use monospace for prompts and JSON. Auto-refresh where noted (~5s), with a pause-refresh toggle.

1. **Overview** (auto-refresh)
   - Engine status (running/paused/dozing), current tick, sim time, tick interval, last tick duration, brain online
   - Ollama: reachable, loaded models + VRAM from `/api/ps`, model versions
   - Container health for each service (from healthcheck status if available, otherwise each service's own `/healthz`)
   - Last 24h: ticks run, doze ticks, errors, invalid-JSON rate, rejected-effect rate, mean/p95 brain and narrator latency, tokens per tick
   - Dev control buttons (B4)

2. **Ticks**: a filterable table (status, area, action text search, has rejections, has errors, slow > N ms). Click a row → Tick detail.

3. **Tick detail** (the core page): one tick, top to bottom as a timeline:
   - Stage timing bar (world / brain / narrator / apply / record / sleep)
   - World tick: clock change, decay per need, event rolls (fired and not fired, with chance vs roll and filter reasons)
   - Brain call: system prompt, user prompt, schema, raw response, parsed JSON, tokens, timings, retries. Collapsible sections with a copy button on each.
   - Recall: query text, candidate memories with recency/importance/relevance/score, selected ones highlighted
   - Narrator call: same layout as the brain call
   - Effects: every proposed change, accepted ✓ or rejected ✗ with the reason
   - Need math: a table for each need, before → decay → gain (bucket→number) → repetition multiplier → effort cost → clamp → after
   - Mood math: inputs → valence/arousal → word
   - Memory writes: any short-term memory created + its embed call
   - Prev / next tick buttons

4. **LLM calls**: all calls, filterable by kind, model, valid/invalid, error, latency. Charts over time: latency (p50/p95), tokens in/out, invalid-JSON rate. Click → the call detail (+ "Open in playground").

5. **Memory and embeddings**
   - Long-term and short-term memory tables (text, importance, created, last recalled, times recalled)
   - **Embedding map:** 2D projection of all memory embeddings (PCA with numpy, computed on the server), drawn as a scatter plot. Points coloured by kind/age and sized by importance. Hover shows the text.
   - **Search box:** type any text → embed it → show nearest memories with cosine scores and the full recall score breakdown, exactly as the engine would compute it.
   - Recall stats: how often each memory is recalled, plus memories that are never recalled

6. **Needs and mood**: time-series charts of every need, energy, valence and arousal over a chosen range (24h / 7d / all). Markers for sleep and doze periods. Needs that sat at a critical level the longest.

7. **Behaviour**: action variety (distinct actions per day, top repeated actions, repetition-penalty hits), time spent per area, effect ops histogram, rejection reasons histogram. These are tuning aids.

8. **Sleep**: each night's sleep: inputs, LLM outputs, proposed vs clamped tendency changes, new memories, new or updated preferences. Tendency history chart.

9. **World state**: the current areas and objects (with tags, state, location, `created_by`), the inventory, and recent object history (created, moved, consumed, by which tick).

10. **Logs**: tail of `./data/logs/*.jsonl`, filterable by service, level, tick_id and text search. Live tail toggle. Clicking a tick_id opens Tick detail.

11. **Config**: read-only view of the current character.yaml, world.yaml and settings.yaml as loaded by the engine (+ a diff against the files on disk if they've changed since load), with a `reload_settings` button.

12. **Playground** (B5)

## B7. API
JSON endpoints under `/api/dev/...` for each page, so the pages are thin and I can query them with curl. Paginate everything (default 50).

## B8. Part B milestones
1. Tracing layer + tables + retention + structured logs (engine). Tests prove that a mock tick writes a complete trace.
2. `dev_commands` queue + engine handling + tests (pause/step/resume with the mock backend).
3. devdash service skeleton + auth/bind rules + Overview + Ticks + Tick detail.
4. LLM calls page + Playground + model compare.
5. Memory and embeddings page (map + search).
6. Needs/mood, Behaviour, Sleep, World state pages.
7. Logs + Config pages.
8. Polish: seed script produces data for every page, README section "Using the dev dashboard".

## Definition of done (Part B)
- After 100 mock ticks + 1 forced sleep, every page shows real data, and Tick detail fully explains every number on the public site for that tick.
- With `trace_level: full`, I can recover the exact prompt and raw response for any brain or narrator call in the last 14 days and replay it in the playground.
- Pausing, stepping and resuming from the dashboard works. The engine stays the only writer of game state.
- The dashboard is reachable only from localhost by default, and needs auth when LAN-exposed.

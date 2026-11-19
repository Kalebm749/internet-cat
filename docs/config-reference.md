# Config reference

Three files in `config/`. Loaded and validated by `cat/config.py`. The validator collects **every** problem and refuses to start, printing them all (need ids in event effects exist, `tendency_labels` match `tendencies`, `start_area` exists, need flags are known, numeric ranges are valid, `self_image` is text only). Unknown top-level fields in `character.yaml` and `world.yaml` only produce a warning (e.g. a leftover `species`).

## character.yaml
| Field | Req | Meaning |
|---|---|---|
| `name` | ✔ | Identity. |
| `pronoun` | | Used in prompts/web copy (default `they`). |
| `backstory`, `voice` | | Optional prompt text for the brain. |
| `self_image` | | `{seed: ""}`. Optional text hint for the entity's self-image; empty by default (the entity starts with none). Text only. Learned self-image lines are stored in the database, not here. |
| `core_traits` | ✔ | List of strings. Never change. |
| `tendencies` | ✔ | `{name: 0..1}`. Drift ≤ `sleep.tendency_max_step` per night. |
| `tendency_labels` | | `{tendency: [low_label, high_label]}` for the web sliders. |
| `start_area` | ✔ | Area id the creature starts in. |
| `needs` | ✔ | `{id: {...}}`, see below. Order is display order. |

### Need fields
| Field | Meaning |
|---|---|
| `start` ✔ | Initial value 0..100. |
| `decay` ✔ | Points lost per sim hour. |
| `label` | Display name (default: id). |
| `words` | `{upper_bound: word}`; first bound the value is below wins, e.g. `{15: starving, 35: peckish, 101: full}`. Default: <15 desperate, <35 low, <70 okay, else content. |
| `requires_consume` | If true, gains only apply when an `edible` object was consumed that tick. |
| `energy_weight` | Weight in the derived **energy** (weighted average of needs). At least one need needs a weight > 0. |
| `effort_drain` | Exactly one need: effort cost (`effort_energy_cost`) is subtracted from it. |
| `sleep_restore` | Exactly one need: sleeping and dozing restore it. |

`energy` is not a valid need id: energy is derived and cached in `state.energy`.

## world.yaml
| Field | Meaning |
|---|---|
| `areas` ✔ | `{id: {name, description, exits: [area ids]}}`. Exits must be mirrored. |
| `objects` ✔ | List of `{id, name, area, tags, state}`. `area` may be `inventory`. Tags are free strings; `movable` and `edible` have rules (server-architecture §9). |
| `weather` | List of weather names; the first is the starting weather. Default `[clear]`. |
| `events` | List of `{text, chance (0..1 per tick), area?, hours? [from, to), only_weather?, set_weather?, mood? (float nudge to mood, decays), spawn?: {name, tags, area}}`. An event with `area` only fires while the creature is in that area (it must perceive it); `spawn.area` is where the object appears. Spawns are skipped when the area holds `max_objects_per_area` objects. |

## settings.yaml
Every tuning number. Groups:
- **LLM**: `ollama_host` defaults to `http://ollama:11434` (the Compose service name; use `http://127.0.0.1:11434` or `--ollama-host` when running outside Docker). `llm_temperature` (per task), `llm_num_ctx`, `tendency_words`, `preferences_shown`, `brain_model`, `narrator_model`, `sleep_model`, `embed_model`, `ollama_think`, `llm_timeout_seconds`, `health_timeout_seconds`, `llm_retries`.
- **Pacing**: `tick_interval_seconds` (real), `sim_start`, `sim_minutes_per_tick_default`, `sim_minutes_min/max` (narrator duration clamp), `phases` (start hour of dawn/day/dusk/night).
- **Effects**: `gain_values`, `effort_energy_cost`, `repetition_penalty`, `create_limit_per_tick`, `allowed_tags`, `state_value_max_length`.
- **Mood**: `mood.*` weights, `feeling_values`, word thresholds. `default_need_words` / `energy_words` map values to words. `events_max_per_tick`, `max_objects_per_area` cap events.
- **Tracing & logs**: `trace_enabled`, `trace_level` (`full` | `summary`), `trace_retention_days`, `trace_recall_candidates`, `log_dir`, `log_level`, `log_max_bytes`, `log_backup_count`, `log_tick_events`.
- **Dev tools**: `dev_db_path`, `dev_commands_enabled`, `dev_poll_seconds`, `paused_alive_seconds`, `devdash.*`.
- **Downtime**: `downtime_policy` (`pause` | `catch_up`), `max_catch_up_hours`, `catch_up_min_gap_ticks`, `catch_up_decay_multiplier`.
- **Doze/sleep**: `doze.*`, `sleep.*` (thresholds, restore rate, `tendency_max_step`).
- **Memory**: `notable.*` (what counts as a notable tick, importance of short-term memories), `recall`, `recency_half_life_sim_hours`, `short_term_shown`, `notable`, `long_term_cap`.
- **Web**: `web_port`, `diary_page_size`, `keepsakes_first_page`, and `web_ui.*` (poll interval, when a need counts as low/critical, trend and chip thresholds, keepsake dot thresholds, page-size limits).
- `db_path`, `backup_keep` (also read by `deploy/backup.sh`).
- **Deployment**: `heartbeat_file` (written by the engine each loop), `healthcheck.engine_max_age_seconds` (the engine is unhealthy when its heartbeat is older than this or 3x the tick interval) and `healthcheck.http_timeout_seconds`, `health_require_models` (brain is online only if all configured models are pulled), `pull_timeout_seconds`.
- `CAT_CONFIG_DIR` (env) selects the config directory (set to `/app/config` in the containers); relative paths such as `data/cat.db` resolve against its parent.

## Effects: how narrator changes are validated (`cat/effects.py`)
Changes are checked **in order** against the real world; invalid ones are dropped, stored in `ticks.rejected_changes` with a reason, and logged.
- Objects are referenced by `object_id`; a unique exact name is accepted as a fallback.
- `create`: tags not in `allowed_tags` are stripped (recorded as `dropped_tags`); at most `create_limit_per_tick` per tick; area must be the current one.
- Caps: `max_changes_per_tick`, `inventory_max`, `max_objects_per_area`, `name_max_length`, `state_value_max_length`.
- `consume` of an edible is the only thing that lets `requires_consume` needs rise; `consume_min_gain` is the minimum bucket granted if the narrator forgot to score it. Those needs are never repetition-penalised.
- Objects spawned by world events are recorded in the tick's `changes` as `{"op": "create", "source": "event"}`, so every object appearance or disappearance is auditable.

## Memory (`cat/memory.py`)
- A tick is *notable* if a need moved by >= `notable.need_change`, the brain's `feeling` is in `strong_feelings`, a `create` happened, or changes were rejected. Notable ticks become short-term memories (text + embedding; importance = `importance_base` + `importance_per_trigger` x triggers).
- Recall: `w_recency*recency + w_importance*importance + w_relevance*cosine(query, memory)`, recency = 0.5^(sim hours since created/last recalled / `recency_half_life_sim_hours`). Recalled memories get `last_recalled` updated.
- If the embedding call fails, memories are still stored (no index) and recall falls back to recency + importance.
- After changing `embed_model`, run `scripts/reembed.py`.

## Sleep (`cat/sleep.py`, settings `sleep.*`)
- **Falls asleep** when it is night and energy < `fall_asleep_energy_below` (no brain call), or the brain's `wants_sleep` is true and energy < `wants_sleep_energy_below` (or it is night).
- **Asleep ticks** (`ticks.kind = 'asleep'`) skip the brain/narrator, restore the `sleep_restore` need by `restore_per_sim_hour`, and decay needs at `decay_multiplier` speed. It **wakes** when that need reaches `wake_rest_at_least`, it is past `wake_not_before_hour` and not night, or after `max_hours`.
- **The cycle** runs when sleep begins and makes 3 LLM calls (consolidate, tendencies, preferences). Nothing is written unless all succeed; if the brain is offline it stays pending (`meta.sleep_pending`) and retries on every sleeping tick. Short-term memories are kept until then.
- LLM output is turned into numbers by code: memory importance is `low|medium|high` -> `importance_values`; tendency proposals are -1..1, scaled by `tendency_max_step` and clamped to 0..1 (core traits are never touched); preference valences are blended with the existing value (`preference_blend`), so new likes start weak and strengthen by repetition. Weak ones (< `preference_min_abs`) are forgotten and the total is capped.
- `sleep_log.sim_date` is the date the sleep began.

## Brain offline (`cat/engine.py`)
- Each tick starts with `GET /api/tags` (`health_timeout_seconds`), cached for that tick. If it fails, or the brain/narrator call still fails after `llm_retries` retries, the tick is a **doze**: events, clock and decay still run, `doze.energy_restore` is added to the sleep_restore need, the tick is stored with `kind='doze'`, `outcome='dozing'`, and `state.brain_online=0`. No LLM calls are made.
- When the brain returns, the first prompt says the creature just came round from a doze (and omits the stale "last outcome").
- A creature that is exhausted at night still falls asleep while the brain is offline; the sleep cycle waits (see Sleep).
- If a tick raises an unexpected error, the state is reloaded from the DB and the loop waits at least `error_backoff_seconds` before the next one.

## Website (`cat/web/`)
- `character.yaml` optional fields used by the page: `portrait` (default `img/cat.png`; `<name>-asleep.<ext>` next to it is swapped in while asleep/dozing if the file exists), per-need `label` and `words`, `tendency_labels`, `pronoun`.
- `GET /` is a complete server-rendered snapshot (works without JS; "read older pages" and "more keepsakes" are plain links). `journal.js` polls `GET /api/state` every `web_ui.poll_seconds` and adds only new entries. `GET /api/diary?before=<tick id>&limit=` and `GET /api/keepsakes?offset=&limit=` page through history.
- Diary entries are keyed (`t<tick id>`, `d<first doze tick id>`, `s<sleep_log id>`). Consecutive dozing ticks collapse into one entry; asleep ticks are hidden (the "Last night…" card stands for them). `sleep_log.tick_id` is the tick where the sleep began, which is where the card sits.
- With an empty or missing database the page shows the empty states, built from the config.

## Downtime (`cat/downtime.py`, settings `downtime_policy` ...)
What happens to the creature while the engine (or the whole host) was off:
- `pause` (default): sim time resumes exactly where it stopped; nothing decays during the gap.
- `catch_up`: at startup the real gap is converted to sim time at the normal pace (`sim_minutes_per_tick_default` per `tick_interval_seconds`), **capped at `max_catch_up_hours` simulated hours**. Needs decay for that time (`catch_up_decay_multiplier`; if the creature was asleep when it stopped, the gap counts as sleep and restores rest). One tick row of kind `catchup` is written ("dozed through the night" / "...afternoon"), shown on the website as a collapsed doze line. Then the loop resumes, and the first prompt says the creature just came round from a doze.
- Gaps shorter than `catch_up_min_gap_ticks` tick intervals are treated as ordinary restarts. After a crash the last sign of life may be up to one tick interval older than the real stop, so one interval is subtracted; after a clean stop (SIGTERM / `docker compose stop`) the gap is exact. With `tick_interval_seconds: 0` (mock runs) there is no pace to convert with, so nothing is caught up.
- The engine records `meta.last_alive_epoch` and `meta.clean_stop` on every tick commit and at stop.

## Tracing and structured logs (`cat/trace.py`, `cat/logs.py`)
Every tick is fully traceable. The engine records, in `cat.db`:
- `tick_traces` (status ok/doze/error, total and per-stage ms: world, recall, brain, narrator, apply, record, sleep; a tick that raised before being recorded is stored under a **negative** `tick_id`),
- `llm_calls` (one row per *attempt*: the exact system prompt, user prompt, JSON schema and raw response text that were sent/received, parsed JSON, valid-JSON flag, tokens and Ollama's own load/prompt/eval/total durations),
- `embed_calls` (purpose `memory_write` | `recall_query` | `repetition_check`), `recall_logs` (every scored memory with recency / importance / relevance / score and whether it was selected),
- `effect_logs` (every proposed change, accepted or rejected, with the reason), `need_math` (per need: before, decay, bucket, raw gain, repetition multiplier, effort cost, clamp adjustment, after; `after = before - decay + gain_raw*repetition_mult - effort_cost + clamp_adjust`), `mood_math` (every mood input and term), `event_rolls` (chance vs roll, or why an event was filtered), `sleep_traces` (inputs, raw LLM output, proposed vs clamped changes).
- `memories.tick_id` links a memory to the tick that wrote it.

**Tracing never breaks the creature**: a failed trace write is logged as a warning and the tick carries on. `trace_level: summary` keeps numbers (timings, tokens, rejections, math) but stores no prompt, response or input text. During every sleep cycle `llm_calls`, `embed_calls` and `recall_logs` older than `trace_retention_days` are pruned; the small tables are kept.

All services log JSON lines (`ts, level, service, tick_id, event, message, extra`) to stdout (`docker logs`) and to `data/logs/<service>.jsonl`, rotated by size. This is switched on by `CAT_STRUCTURED_LOGS=1` (set in `compose.yaml`); plain console logging is used otherwise.

## Dev commands (`cat/devqueue.py`, `cat/devcontrol.py`)
The dev dashboard never writes game state. It inserts rows into `dev_commands` in **`data/devdash.db`** (a separate file, so `cat.db` stays strictly single-writer). The engine polls the queue at the start of every loop iteration, and every `dev_poll_seconds` while waiting between ticks or paused, runs each command and records `status` (`pending → running → done | failed | rejected`) and a `result`. `devdash.db` also holds `playground_runs`, `engine_status` (what the engine publishes: state, tick id/duration, interval, pid, heartbeat) and `engine_config` (the config exactly as the engine loaded it).

| Command | Args | Effect |
|---|---|---|
| `pause` / `resume` | | Stop / continue the tick loop. While paused the engine idles (and keeps its last-alive time fresh, so a pause is never counted as downtime). Not remembered across a restart. |
| `step` | | Run exactly one tick (only while paused). |
| `set_interval` | `seconds` (null = reset), `ticks` (optional) | Change the tick interval temporarily: until reset/restart, or for `ticks` waits. Applies to a wait already in progress. |
| `force_sleep` | | Run the sleep cycle now (consolidate, drift tendencies, learn preferences) on the current day; attaches to the latest tick. Needs the brain online. The creature itself stays awake. |
| `reload_settings` | | Re-read the YAML (all three files are validated; invalid files change nothing) and apply `settings.yaml` live. Reports which keys changed. `character.yaml` / `world.yaml` changes need a restart. |
| `inject_event` | `text`, optional `area`, optional `spawn: {name, tags, area}` | A one-shot world event on the next tick: the brain sees it, it is stored in `events`, a spawned object is recorded as `create` with `source: event`. |
| `snapshot` | | Online backup to `data/backups/cat-<date>-<time>-snapshot.db`. |

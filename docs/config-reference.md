# Config reference

Four files in `config/`: `settings.yaml`, `character.yaml`, `world.yaml` and `prompts.yaml`. Loaded and validated by `cat/config.py`. The validator collects **every** problem and refuses to start, printing them all (need ids in event effects exist, `tendency_labels` match `tendencies`, `start_area` exists, need flags are known, numeric ranges are valid, `self_image` is text only). Unknown top-level fields in `character.yaml` and `world.yaml` only produce a warning (e.g. a leftover `species`).

## prompts.yaml
Every fixed piece of prompt text the LLMs see: the brain's system prompt and the headings and sentences of its situation prompt, the journal's system prompt and the lines of its user message, and the sleep cycle's system prompt and four step prompts. Edit it to change how the entity, its journal and the sleeping mind are instructed. Each entry lists in a comment the `{placeholders}` the engine fills in (`cat/prompts.py` `SPEC` is the authoritative list). The validator refuses to start on a missing or empty entry or an unknown placeholder, and warns on unknown top-level keys. A literal brace is written `{{` or `}}`. Text edits are applied live by the dev dashboard's `reload_settings`; no restart needed. Prompts never carry need numbers, and must not tell the brain which action to take.

## character.yaml
| Field | Req | Meaning |
|---|---|---|
| `name` | ✔ | Identity. |
| `pronoun` | | Used in prompts/web copy (default `they`). |
| `backstory`, `voice` | | Optional prompt text for the brain. |
| `self_image` | | `{seed: ""}`. Optional text hint for the entity's self-image; empty by default (the entity starts with none). Text only. Learned self-image lines are stored in the database, not here. |
| `core_traits` | ✔ | List of strings. Never change. |
| `tendencies` | ✔ | `{}` by default: tendencies are **learned** during sleep, in the creature's own words (drift ≤ `sleep.tendency_max_step` per night; `sleep.tendency_start`, `max_tendencies`, `max_new_tendencies_per_night`, `tendency_forget_below`, `tendency_name_max_length`). You may seed some `{phrase: 0..1}`. |
| `tendency_labels` | | Optional `{tendency: [low_label, high_label]}` for seeded tendencies on the web sliders; learned ones use their own name. |
| `start_area` | ✔ | Area id the creature starts in. |
| `needs` | ✔ | `{id: {...}}`, see below. Order is display order. |

| `stale_here` | | Text added to the brain's prompt once it has spent `stale_after_ticks` ticks in one area. A flag only: it changes no need value. |

### Need fields
| Field | Meaning |
|---|---|
| `start` ✔ | Initial value 0..100. |
| `decay_per_hour` ✔ | Points lost per sim hour. |
| `meaning` ✔ | One sentence saying what the need is. Shown on hover in the website's Needs panel. |
| `expressions` ✔ | `{full, okay, low, empty}`: text for each band. `empty`, `low` and `full` are required and non-empty; `okay` may be null. The brain sees the expressions of the most pressing needs (in words, never numbers). |

There are no behaviour flags. The sustenance need (`settings.yaml` `sustenance_need`) only rises when an edible object is consumed. `energy` is not a valid need id: energy is a separate resource (see below).

A level becomes a band by `settings.yaml` `need_bands`: below `empty_below` is `empty`, below `low_below` is `low`, below `okay_below` is `okay`, otherwise `full` (thresholds must ascend).

## world.yaml
| Field | Meaning |
|---|---|
| `areas` ✔ | `{id: {name, description, exits: [area ids]}}`. Exits must be mirrored. |
| `objects` ✔ | List of `{id, name, area, tags, state}`. `area` may be `inventory`. Tags are free strings; `movable` and `edible` have rules (server-architecture §9). |
| `weather` | List of weather names; the first is the starting weather. Default `[clear]`. |
| `objects` | Permanent things: `{id, name, area, tags, state?, purpose?, produces?}`. `purpose` says what it is for (a need, a craft or a mystery; documentation only). `produces: {name, tags, at_hour: H | every_hours: N, max}` makes it a **source**: it puts out a thing once a day at/after hour H, or every N sim hours, whether or not the creature is there, never more than `max` waiting; products expire after `object_lifetime_hours.source`. A permanent edible object is a warning (food comes from sources). An existing world follows this list at every start (`_sync_world`). |
| `events` | List of `{text, chance (0..1 per tick), area?, hours? [from, to), only_weather?, set_weather?, drains? ({need id: points} the world takes from needs when it fires), mood? (float nudge to mood -1..1, decays), spawn?: {name, tags, area}}`. An event with `area` only fires while the creature is in that area (it must perceive it); `spawn.area` is where the object appears. Spawns are skipped when the area holds `max_objects_per_area` objects, and expire after `object_lifetime_hours` (settings). |

## settings.yaml
Every tuning number. Groups:
- **LLM**: `ollama_host` defaults to `http://ollama:11434` (the Compose service name; use `http://127.0.0.1:11434` or `--ollama-host` when running outside Docker). `llm_temperature` (per task), `llm_num_ctx`, `tendency_words`, `preferences_shown`, `brain_model`, `narrator_model`, `sleep_model`, `embed_model`, `ollama_think`, `llm_timeout_seconds`, `health_timeout_seconds`, `llm_retries`.
- **Pacing**: `tick_interval_seconds` (real), `sim_start`, `sim_minutes_per_tick_default`, `sim_minutes_min/max` (clamp on a move's duration), `phases` (start hour of dawn/day/dusk/night).
- **World director**: `director.enabled`, `every_ticks`, `stale_every_ticks` (≤ every_ticks), `text_max_length`, `max_mood` (0..1), `max_drain_points`, `max_drain_needs`, `avoid_repeat`, `recent_actions_shown`; and `llm_temperature.director`. Its wording is the `director` section of `prompts.yaml`.
- **Effects**: `gain_values`, `effort_energy_cost` (energy points an action's effort spends), `repetition_penalty` (`window` decisions, `multiplier` and `needs`: the needs whose gains shrink when the same move is repeated in the same place; no similarity test), `create_limit_per_tick`, `allowed_tags`, `state_value_max_length`, `consume_min_gain`, `sustenance_need`.
- **Needs and prompts**: `need_bands` (`empty_below`, `low_below`, `okay_below`, ascending), `max_pressing_needs` (how many need expressions the brain's prompt shows, default 2), `stale_after_ticks` (ticks in one area before `stale_here` is added; 0 turns it off, default 3), `dislike_drain` (`needs`, `points_per_hour`, `max_matches`: needs the world drains while something disliked is present).
- **Moves** (`config/moves.yaml`, loaded and validated with the other files): `verbs` with `label`, `target` (`none|area|object|project|subject`; `subject` = something from its own history to draw), `reach`, `any_tags`, `effort`, `minutes: [min, max]`, `effects` and `by_tag` (need → bucket), `novelty`, `does` (`none|move_self|take|drop|consume|work_on|sleep|draw_new|draw_more|finish_drawing|pin|discard`), `only_when` (`always|can_sleep`), `needs_here` (tags: offered only when an object here has one, e.g. `[paper]`), `when_state` (object verbs: `{key: [values]}` the target's state must match, e.g. `{status: [in progress]}`), `outcome` (the plain words the diary, the brain and memory use for what happened) and `reveals` (object verbs only: the outcome also says what is true of the target, from world.yaml `tag_facts`); only `consume` verbs may feed sustenance. Settings: `moves.max_offered`, `moves.novelty_hours`, `moves.shuffle` (seeded shuffled menu order; false = file order), `moves.mark_untried` (mark a move "(never tried)" when it has never made that verb).
- **Drawing**: `drawing.enabled`, `subjects_offered` (subjects from its history offered for a new drawing), `happenings_considered` (recent happenings it could draw), `max_in_progress` (unfinished drawings at once), `board_capacity` (drawings on a `board`), `marks_per_sitting` (code's strokes per `moves.reference_minutes` when a sitting has no marks of its own), `sketch` (its own marks through one LLM call per sitting), `marks_max` / `marks_min` (marks per sitting; a 60-minute sitting gets `marks_max`), `history_shown` (past drawings it is reminded of), `colours` (`charcoal: {name: hex}`, always with the paper; `paints: {name: hex}`, only while an object tagged `paints` is here). `llm_temperature.sketch`. Its prompt text is under `sketch` in prompts.yaml. How the website draws them: `web_ui.drawing` (`width`, `height`, `jitter_max`, `pinned_shown`). Tags `paper`, `board`, `drawing` and `paints` must be in `allowed_tags`. `object_lifetime_hours.creature` is unset, so its drawings never expire.
- **Sleep place**: `sleep.nest_restore_multiplier`: energy restored while asleep is multiplied by this where an object tagged `nest` is.
- **Director**: `director.allow_spawn` (false: it makes happenings only, never objects).
- **Drives**: `drives.enabled`, `needs` (need ids that have a drive), `chance` (`low` and `empty`, 0..1 per decision for the most pressing need), `forage` (hungry with nothing here: go looking). The one place code picks a move because of a need; worded by `impulse.drive`, `impulse.drive_<need>` and `impulse.drive_forage` in prompts.yaml. `activity.interrupt_on_low_need`: a need newly low ends a running move.
- **Journal**: `journal.enabled` (false: no entry, the diary shows the reason it gave), `journal.max_length` (characters kept). The entry is written after code decides what happened, with `brain_model` and `llm_temperature.journal`; its text is under `journal` in prompts.yaml.
- **world.yaml `tag_facts`**: `{tag: text}`, what is true of a thing with that tag, as the end of a sentence starting "It …" (e.g. `soft: is soft`). Tags must be in `allowed_tags`.
- **Activities**: `activity.enabled`, `step_minutes` (sim minutes per carry tick), `carry_interval_seconds`, `interrupt_on_events`, `interrupt_on_empty_need`.
- **Anxiety**: `anxiety.enabled`, `start`, `band_points`, `energy_points` (energy below key adds value), `rise_per_hour`, `fall_per_hour`, `sleep_fall_multiplier`, `mood_weight`, `decay_pressure`, `sleep_restore_penalty`, `words` (anxiety below key -> word), `impulse_above`, `impulse_max_chance` (0..1), `impulses` (`{name: {weight, verbs}}`; their wording is `impulse.<name>` in prompts.yaml, `impulse.default` otherwise).
- **Habits**: `habits.enabled`, `window_ticks`, `min_count`, `min_share` (0..1), `max_stored`, `max_shown`, `text_max_length`. Wording in `prompts.yaml`: `habits.fallback`, `sleep.jobs.habits`, `sleep.habits`, `brain.situation.habits`.
- **Projects**: `projects.max_active`, `work_bucket` (bucket for one work-on move), `artifacts_enabled`, `progress_values` (points per bucket; buckets must be in `gain_values`), `taper` (0..1; a step is multiplied by 1 − taper × progress/100), `milestone_every`, `completion_gains` (need points paid by code on finishing), `pause_after_idle_nights` (0 = never), `idle_hours_note`, `stages` (progress → words, with a bound above 100), the text limits and `artifacts_shown`.
- **Energy**: a separate resource from −50 to 50. `energy_start`, `energy_words` (value → word, below the key), and in `sleep.*` / `doze.*` the thresholds (`fall_asleep_energy_below`, `wants_sleep_energy_below`, `wake_energy_at_least`) and restore rates.
- **Mood**: one number, a slider from −50 (negative) to 50 (positive), from need bands. `mood.bands` (`{need: {band: points}}`; joy and peace raise it, sustenance `empty` lowers it), `event_points`, `event_decay`, `feeling_values`, `word_threshold` (above it: content; below minus it: low; otherwise calm). `events_max_per_tick`, `max_objects_per_area` cap events.
- **Tracing & logs**: `trace_enabled`, `trace_level` (`full` | `summary`), `trace_retention_days`, `trace_recall_candidates`, `log_dir`, `log_level`, `log_max_bytes`, `log_backup_count`, `log_tick_events`.
- **Dev tools**: `dev_db_path`, `dev_commands_enabled`, `dev_poll_seconds`, `paused_alive_seconds`, `devdash.*`.
- **Downtime**: `downtime_policy` (`pause` | `catch_up`), `max_catch_up_hours`, `catch_up_min_gap_ticks`, `catch_up_decay_multiplier`.
- **Doze/sleep**: `doze.*`, `sleep.*` (thresholds, restore rate, `tendency_max_step`).
- **Memory**: `notable.*` (what counts as a notable tick, importance of short-term memories), `recall`, `recency_half_life_sim_hours`, `short_term_shown`, `notable`, `long_term_cap`.
- **Web**: `web_port`, `diary_page_size`, `keepsakes_first_page`, and `web_ui.*` (poll interval, trend and chip thresholds, keepsake dot thresholds, page-size limits).
- `db_path`, `backup_keep` (also read by `deploy/backup.sh`).
- **Deployment**: `heartbeat_file` (written by the engine each loop), `healthcheck.engine_max_age_seconds` (the engine is unhealthy when its heartbeat is older than this or 3x the tick interval) and `healthcheck.http_timeout_seconds`, `health_require_models` (brain is online only if all configured models are pulled), `pull_timeout_seconds`.
- `CAT_CONFIG_DIR` (env) selects the config directory (set to `/app/config` in the containers); relative paths such as `data/cat.db` resolve against its parent.

## Effects: how world changes are validated (`cat/effects.py`)
The changes a chosen move makes (code, never an LLM) are checked **in order** against the real world; invalid ones are dropped, stored in `ticks.rejected_changes` with a reason, and logged.
- Objects are referenced by `object_id`; a unique exact name is accepted as a fallback.
- `create`: tags not in `allowed_tags` are stripped (recorded as `dropped_tags`); at most `create_limit_per_tick` per tick; area must be the current one.
- Caps: `max_changes_per_tick`, `inventory_max`, `max_objects_per_area`, `name_max_length`, `state_value_max_length`.
- `object_lifetime_hours`: `{event | director | creature: sim hours}` before an object that maker brought is gone (missing or 0 = it stays). Carried objects and the world's starting objects never expire; each expiry is a `despawn` in the tick's `changes`.
- `consume` of an edible is the only thing that lets the sustenance need (`sustenance_need`) rise; `consume_min_gain` is the minimum bucket granted for a `consume` move. The sustenance need is never repetition-penalised, and only the needs in `repetition_penalty.needs` are.
- Objects spawned by world events are recorded in the tick's `changes` as `{"op": "create", "source": "event"}`, so every object appearance or disappearance is auditable.

## Memory (`cat/memory.py`)
- A tick is *notable* if a need moved by >= `notable.need_change`, the brain's `feeling` is in `strong_feelings`, a `create` happened, changes were rejected, an impulse took the decision over (`notable.impulse`, default true), or anxiety climbed into a higher `anxiety.words` band (`notable.anxiety_rise`, default true). Notable ticks become short-term memories (text + embedding; importance = `importance_base` + `importance_per_trigger` x triggers).
- Recall: `w_recency*recency + w_importance*importance + w_relevance*cosine(query, memory)`, recency = 0.5^(sim hours since created/last recalled / `recency_half_life_sim_hours`). Recalled memories get `last_recalled` updated.
- If the embedding call fails, memories are still stored (no index) and recall falls back to recency + importance.
- After changing `embed_model`, run `scripts/reembed.py`.

## Sleep (`cat/sleep.py`, settings `sleep.*`)
- **Falls asleep** when it is night and energy < `fall_asleep_energy_below` (no brain call), or the brain's `wants_sleep` is true and energy < `wants_sleep_energy_below` (or it is night).
- **Asleep ticks** (`ticks.kind = 'asleep'`) skip the brain and journal, restore **energy** by `restore_per_sim_hour`, and decay needs at `decay_multiplier` speed. It **wakes** when energy reaches `wake_energy_at_least`, it is past `wake_not_before_hour` and not night, or after `max_hours`.
- **The cycle** runs when sleep begins and makes 5 LLM calls (consolidate, tendencies, preferences, self-image, projects). Nothing is written unless all succeed; if the brain is offline it stays pending (`meta.sleep_pending`) and retries on every sleeping tick. Short-term memories are kept until then.
- LLM output is turned into numbers by code: memory importance is `low|medium|high` -> `importance_values`; tendency proposals are -1..1, scaled by `tendency_max_step` and clamped to 0..1 (core traits are never touched); preference valences are blended with the existing value (`preference_blend`), so new likes start weak and strengthen by repetition. Weak ones (< `preference_min_abs`) are forgotten and the total is capped.
- Self-image: after preferences, one call may propose a single line `{text, confidence 0..1, replaces?}`. `max_self_image_lines` caps the lines kept (least confident forgotten first), `self_image_min_confidence` rejects weak proposals, `max_self_image_length` truncates text. Lines are stored like preferences (table `self_image`) and logged with their outcome in the sleep trace.
- `sleep_log.sim_date` is the date the sleep began.

## Brain offline (`cat/engine.py`)
- Each tick starts with `GET /api/tags` (`health_timeout_seconds`), cached for that tick. If it fails, or the brain call still fails after `llm_retries` retries, the tick is a **doze**: events, clock and decay still run, `doze.energy_restore` is added to energy, the tick is stored with `kind='doze'`, `outcome='dozing'`, and `state.brain_online=0`. No LLM calls are made.
- When the brain returns, the first prompt says the creature just came round from a doze (and omits the stale "last outcome").
- A creature that is exhausted at night still falls asleep while the brain is offline; the sleep cycle waits (see Sleep).
- If a tick raises an unexpected error, the state is reloaded from the DB and the loop waits at least `error_backoff_seconds` before the next one.

## Website (`cat/web/`)
- `character.yaml` optional fields used by the page: `self_image.seed` (shown as a first self-image line), per-need `meaning` (hover text), `tendency_labels`, `pronoun`. The portrait frame shows an abstract presence tuned by `settings.yaml` `web_ui.presence` (colour from mood, breathing speed from energy, dimmer asleep, desaturated dozing). The About panel lists the learned self-image lines (`self_image` in `/api/state`).
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
- `tick_traces` (status ok/doze/error, total and per-stage ms: world, director, recall, brain, create, apply, journal, record, sleep; a tick that raised before being recorded is stored under a **negative** `tick_id`),
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

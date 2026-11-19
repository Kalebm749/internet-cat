# Config reference

Three files in `config/`. Loaded and validated by `cat/config.py`; errors name the file and field.

## character.yaml
| Field | Req | Meaning |
|---|---|---|
| `name`, `species` | ✔ | Identity. |
| `pronoun` | | Used in prompts/web copy (default `they`). |
| `appearance`, `backstory`, `voice` | | Prompt text for the brain. |
| `core_traits` | ✔ | List of strings. Never change. |
| `tendencies` | ✔ | `{name: 0..1}`. Drift ≤ `sleep.tendency_max_step` per night. |
| `tendency_labels` | | `{tendency: [low_label, high_label]}` for the web sliders. |
| `start_area` | ✔ | Area id the creature starts in. |
| `portrait` | | Static path of the portrait image (default `img/cat.png`). |
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
| `objects` ✔ | List of `{id, name, area, tags, state}`. `area` may be `inventory`. Tags are free strings; `movable` and `edible` have rules (pi-architecture §9). |
| `weather` | List of weather names; the first is the starting weather. Default `[clear]`. |
| `events` | List of `{text, chance (0..1 per tick), area?, hours? [from, to), only_weather?, set_weather?, mood? (float nudge to mood, decays), spawn?: {name, tags, area}}`. An event with `area` only fires while the creature is in that area (it must perceive it); `spawn.area` is where the object appears. Spawns are skipped when the area holds `max_objects_per_area` objects. |

## settings.yaml
Every tuning number. Groups:
- **LLM**: `llm_temperature` (per task), `llm_num_ctx`, `tendency_words`, `preferences_shown`, `ollama_host`, `brain_model`, `narrator_model`, `sleep_model`, `embed_model`, `ollama_think`, `llm_timeout_seconds`, `health_timeout_seconds`, `llm_retries`.
- **Pacing**: `tick_interval_seconds` (real), `sim_start`, `sim_minutes_per_tick_default`, `sim_minutes_min/max` (narrator duration clamp), `phases` (start hour of dawn/day/dusk/night).
- **Effects**: `gain_values`, `effort_energy_cost`, `repetition_penalty`, `create_limit_per_tick`, `allowed_tags`, `state_value_max_length`.
- **Mood**: `mood.*` weights, `feeling_values`, word thresholds. `default_need_words` / `energy_words` map values to words. `events_max_per_tick`, `max_objects_per_area` cap events.
- **Doze/sleep**: `doze.*`, `sleep.*` (thresholds, restore rate, `tendency_max_step`).
- **Memory**: `notable.*` (what counts as a notable tick, importance of short-term memories), `recall`, `recency_half_life_sim_hours`, `short_term_shown`, `notable`, `long_term_cap`.
- **Web**: `web_port`, `diary_page_size`, `keepsakes_first_page`.
- `db_path`, `backup_keep`.

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

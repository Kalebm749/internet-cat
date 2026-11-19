# Behavior Guide: how the simulation works, how to tune it, how to reset it

This is the "how does it all fit together" document. It explains, in order: who decides what, what happens in one tick, how needs, energy, sleep, mood, memory and the world affect each other, every trigger and its effect, how to tune each parameter, and how to reset the simulation.

It describes the system as built. Numbers are the defaults in `config/*.yaml` at the time of writing; the YAML is always the authority. For field-by-field reference see `config-reference.md`; for the build and deployment see `server-architecture.md`; for the concepts and design rules see `creature-architecture.md`.

**Contents**
1. [The big picture](#1-the-big-picture)
2. [Time: real time, sim time, ticks](#2-time-real-time-sim-time-ticks)
3. [Anatomy of one tick](#3-anatomy-of-one-tick)
4. [Needs](#4-needs)
5. [Energy, sleep and dozing](#5-energy-sleep-and-dozing)
6. [Mood](#6-mood)
7. [Memory and recall](#7-memory-and-recall)
8. [Slow change: tendencies, preferences, self-image](#8-slow-change-tendencies-preferences-self-image)
9. [The world: objects, events, and what the narrator may change](#9-the-world-objects-events-and-what-the-narrator-may-change)
10. [What each LLM sees](#10-what-each-llm-sees)
11. [When things go wrong: brain offline, downtime, crashes](#11-when-things-go-wrong-brain-offline-downtime-crashes)
12. [Master table: triggers and their impact](#12-master-table-triggers-and-their-impact)
13. [Tuning guide](#13-tuning-guide)
14. [Resetting the simulation](#14-resetting-the-simulation)
15. [Watching and debugging](#15-watching-and-debugging)

---

## 1. The big picture

One rule runs through the whole design: **plain code owns time, needs and rules; the LLMs only describe.** Three kinds of thing cooperate:

| Who | Owns | Never does |
|---|---|---|
| **Code (the engine)** | the clock, need levels and decay, energy, mood arithmetic, which memories are kept, validation of every world change, the saved state | invent story or judge what an action meant |
| **The brain (LLM call #1)** | what the entity thinks, feels and chooses to do next, in free text | say what happened, change a need, or change the world |
| **The narrator (LLM call #2)** | what actually happened, which needs the action *helped* (coarse buckets), effort, duration, and a list of proposed world changes | subtract from a need, or change anything the code does not validate |

A third LLM role, **the sleeping mind**, runs once per night and does the slow work: consolidating memories, nudging tendencies, forming preferences, and proposing at most one self-image line.

Some consequences that explain almost every behavior you will see:

- **Actions are open, effects are closed.** The entity can try anything ("climb the old tree", "sing to the rain"). But the saved world can only change through seven validated operations (§9). A story can say a fort was built; if the narrator did not also return a valid `create` change, no fort exists.
- **The entity never grades itself, and the narrator never drains.** Good outcomes come from the narrator; losses come from the world (decay, events, repetition, dislikes).
- **Need levels shape what the entity is *told*, never what it *does*.** Code does not pick, suggest or rank actions by need. A very hungry entity is told "you are running on nothing"; whether it then looks for food is its own choice.
- **The entity does not know what it is.** There is no species or body in the config. Its self-image starts empty and is learned (§8).
- **The database is the truth.** Every prompt is rebuilt from the saved state each tick, so a bad LLM answer can never put the world into an impossible state; it is just rejected and logged.

The pieces, as they run on one laptop:

```
 ollama (the LLMs)  ◄──── engine (tick loop, SQLite, the only writer)
                              │  writes
                              ▼
                        data/cat.db  ◄──── web (read-only website, :8080)
                              ▲        ◄──── devdash (read-only dashboard, :8090)
```

---

## 2. Time: real time, sim time, ticks

There are two clocks.

- **Real time** is wall-clock time. The engine does one **tick** every `tick_interval_seconds` (default **90 s**).
- **Sim time** is the entity's own clock. It starts at `sim_start` (default `2026-11-18T07:00`) and advances only when the engine says so. Each tick advances it by that tick's **duration**:

| Tick kind | Sim minutes advanced | Who decides |
|---|---|---|
| Normal (awake, brain online) | the narrator's `duration`, clamped to `sim_minutes_min`..`sim_minutes_max` (5..90). If the narrator gives nothing usable: `sim_minutes_per_tick_default` (20) | narrator, then code |
| Doze (brain offline) | `doze.sim_minutes` (20) | code |
| Asleep | `sleep.sim_minutes_per_tick` (60) | code |
| Catch-up (after downtime, §11) | the gap converted at the default pace, capped | code |

So the pace of the simulation is **not fixed**: roughly, with 20-minute actions and a 90-second interval, 40 ticks happen per real hour, which is about 13 sim hours per real hour (a sim day passes in a bit under two real hours). A narrator that picks longer durations makes the world move faster per tick; a sleeping night is only about 8-12 ticks (≈ 12-18 real minutes).

**Phases** (`phases`, by sim hour): `dawn` 5, `day` 8, `dusk` 18, `night` 21 (night lasts until dawn). The phase matters for exactly two things: falling asleep (§5) and the website's colour palette. The clock also gives the entity a day number and "part of day" words in its prompt.

`tick_interval_seconds: 0` runs as fast as the machine allows (used for tests and `--interval 0`).

---

## 3. Anatomy of one tick

Everything below happens in this order. The LLM calls happen **outside** any database transaction; the apply-and-record step is **one transaction**, so a crash leaves the previous state intact.

```
  1. World events roll            ── events may fire; may carry mood nudge, weather change, spawn, drains
  2. Rest check                   ── asleep? or exhausted at night?  → a rest tick (no LLM), skip to 6
  3. Brain online?                ── cached health check (GET /api/tags); offline → doze tick, skip to 5
  4a. Recall + Brain call         ── builds the prompt, picks the need expressions, asks for JSON
  4b. Narrator call               ── action + world facts → outcome, gains, effort, duration, changes
  5. Apply (code)                 ── validate changes → decay → event drains → dislike drains → gains
                                     → spend energy → clamp → mood
  6. Record (one transaction)     ── tick row, events, object changes, short-term memory, new state
  7. Maybe sleep                  ── if the brain chose sleep (and it is allowed), run the sleep cycle
```

### Step 1: world events
Each event in `world.yaml` is rolled once per tick, in file order, unless `events_max_per_tick` (2) events have already fired. An event is skipped (and the reason is traced) if:
- it names an `area` and the entity is somewhere else (the entity must be there to perceive it);
- it has `hours` and the sim time is outside `[from, to)`;
- it has `only_weather` and the weather is different;
- it would set the weather to what it already is.

Otherwise it fires with probability `chance`. A fired event can:
- add its text to what the entity "just perceived" (it appears in the next prompt),
- add a **mood nudge** (`mood`, −1..1; see §6),
- change the **weather**,
- **spawn an object** (skipped if the area already holds `max_objects_per_area` objects),
- **drain needs** (`drains: {need: points}`): subtracted from those needs in step 5.

Events roll on *every* tick type, including sleeping and dozing, so a loud noise can drain peace while the entity sleeps.

### Step 2: rest check
- If the entity is **asleep**, the tick is a rest tick.
- If it is **night** and energy is below `sleep.fall_asleep_energy_below` (−25), it **falls asleep** now: a rest tick with onset, and **no brain call**. Exhaustion at night overrides choice.
- Exhaustion by *day* does not force sleep; it only makes sleep available to the brain (§5).

### Step 3: is the brain reachable?
A cheap check against Ollama (`/api/tags`, `health_timeout_seconds`), cached for the tick. With `health_require_models: true` the brain counts as online only if all configured models are pulled. If it is offline, or a call still fails after `llm_retries` (1) retry, the tick becomes a **doze** (§5, §11).

### Step 4a: recall and the brain call
1. **Recall** (only if there are long-term memories): the engine builds a short query from the current area, the objects here, the mood word and what just happened, embeds it, and scores every long-term memory (§7). The top `recall.k` (5) go into the prompt.
2. **Prompt** (§10): self-image, tendencies, likes/dislikes, time and place, mood and energy words, **"What's pulling at you right now"** (the need expressions), objects around, what just happened, recent actions, memories.
3. The brain returns JSON: `reaction, thought, action, intent, feeling (negative|neutral|positive), wants_sleep`. An answer with no `action` is invalid and counts as a failed call.

### Step 4b: the narrator call
The narrator receives the action, the area and its objects (with ids, tags and state), the inventory, exits, time, weather, and each need's **meaning and current band as a word**. It returns `outcome, gains, effort, duration, changes[]`. Invalid buckets are dropped; effort defaults to `medium`; duration is clamped.

### Step 5: apply, in this exact order
1. **Validate and apply world changes** (§9). Rejected changes are logged and do not happen.
2. **Decay** every need by `decay_per_hour × duration/60`.
3. **Event drains**: subtract the drains of the events that fired.
4. **Dislike drains**: if something the entity dislikes is present, subtract from the configured needs (§4). (Awake and dozing ticks only; a sleeping entity is spared.)
5. **Gains**: convert the narrator's buckets (`+` = 15, `++` = 30) to points, **adding only**, with the two special rules: sustenance rises only if something edible was consumed; the gains of the repetition-penalised needs shrink if the action repeats.
6. **Spend energy**: the action's `effort` costs 2 / 6 / 12 energy.
7. **Clamp**: needs to 0..100, energy to −50..50.
8. **Mood** is recomputed from the new bands, the event nudge, the brain's `feeling` and the energy (§6).

### Step 6: record
One transaction writes the diary tick, the events that fired, object changes, a **short-term memory** if the tick was notable (§7), the new state, and (for stale tracking) how long the entity has been in this area. A full trace (prompts, responses, recall scores, need arithmetic, mood arithmetic, event rolls) is stored for the dashboard. Tracing never breaks the simulation: a failed trace write is only a warning.

### Step 7: maybe sleep
If the brain said `wants_sleep: true` **and** (energy < `sleep.wants_sleep_energy_below` (10) **or** it is night), the entity falls asleep and the sleep cycle (§5) runs straight after the tick commits.

---

## 4. Needs

Five needs in the placeholder character, each 0..100: **sustenance, wonder, joy, peace, curiosity**. Each has a `decay_per_hour`, a `meaning` and four text `expressions` (`full`, `okay`, `low`, `empty`). Which needs exist, and what they mean, is entirely up to `character.yaml`.

### Levels become bands
`need_bands` turns a level into a band: below `empty_below` (15) is **empty**, below `low_below` (40) is **low**, below `okay_below` (75) is **okay**, otherwise **full**. The *only* things that ever see a band's word are the prompts, the website, and the mood mapping. Raw numbers go to the database and the dashboard.

### What changes a need
| Cause | Direction | Details |
|---|---|---|
| **Decay** | down | `decay_per_hour` × sim hours. The baseline: needs always fall. Slower while asleep (`sleep.decay_multiplier` 0.5). |
| **Narrator gains** | up | The narrator judges which needs the action helped and says `+` (+15) or `++` (+30). It can only add. |
| **Event drains** | down | `drains` on a world event, applied when it fires. |
| **Dislike drains** | down | While something it dislikes is present (§8). |
| **Repetition** | shrinks *gains* | A repeated action pays less (below). |

The narrator never drains. Bad things that happen are story; the harm lands through decay, events or dislikes, and through mood (§6).

### The sustenance rule (hard rule)
`sustenance_need` (default `sustenance`) rises **only** when an `edible` object was consumed this tick (a validated `consume` change). If the narrator scores sustenance but nothing was consumed, the gain is ignored (the dashboard shows "ignored"). If something was consumed and the narrator forgot to score it, the entity still gets at least `consume_min_gain` (`+`).

### Repetition pays less
The action text is embedded and compared with the entity's last `repetition_penalty.window` (6) actions **taken in the same place**. If any is at least `similarity_threshold` (0.85) similar, the gains of the needs listed in `repetition_penalty.needs` (**wonder, curiosity**) are multiplied by `multiplier` (0.4). Other needs, and sustenance, are never shrunk. Effect: the same trick in the same room stops feeding wonder and curiosity, which nudges variety without ever forbidding anything. Moving to another place resets the comparison. If embedding fails, there is no penalty that tick.

### Stale places
The engine counts consecutive action ticks in the current area. After `stale_after_ticks` (3), the prompt gains the `stale_here` sentence from `character.yaml` ("…everything about this place feels familiar and used up."). This is **only a sentence in the prompt**: it changes no need value. Moving resets the count; `0` turns the feature off. The count survives restarts.

### What the entity is told
Each tick the engine selects up to `max_pressing_needs` (2) needs whose band is **empty or low**, lowest band first (empty before low), ties broken by config order, and puts their `expressions` text under **"What's pulling at you right now"**. If nothing is pressing, the prompt says "Nothing in particular is pulling at you right now" and may show one `full` expression. `okay` expressions are never shown. The text never contains numbers and never suggests an action. The dashboard's tick page lists all five needs with their band, whether they were selected, and why.

### How fast do the defaults move? (no gains, no events)
Sim hours from the starting levels until each band is crossed:

| Need | Start | Decay/h | → low (<40) | → empty (<15) |
|---|---|---|---|---|
| sustenance | 70 | 5 | 6 h | 11 h |
| wonder | 60 | 4 | 5 h | 11.25 h |
| joy | 60 | 6 | 3.3 h | 7.5 h |
| peace | 70 | 2 | 15 h | 27.5 h |
| curiosity | 50 | 5 | 2 h | 7 h |

At ~13 sim hours per real hour, an entity that never eats and never has a good moment is empty on sustenance about 50 real minutes in. Gains (+15/+30) and the narrator's judgement are what push levels back up. To reach `full` (≥ 75) a need must be fed well above its start.

---

## 5. Energy, sleep and dozing

### Energy is not a need
**Energy** is a separate resource, a single number from **−50 to 50**, starting at `energy_start` (30). It is **spent by effort** (each action's effort: low 2, medium 6, high 12 points, `effort_energy_cost`) and **restored only by sleeping or dozing**. It is never derived from needs and the narrator cannot touch it. Its words (`energy_words`): exhausted (< −25), tired (< 0), fine (< 25), wide awake (≥ 25). The words go into the prompt ("Energy: tired"); the number does not.

### Falling asleep: three ways
1. **Exhaustion at night:** night **and** energy < −25 → falls asleep immediately, with no brain call.
2. **The brain's choice:** it sets `wants_sleep` and either energy < 10 *or* it is night.
3. **Forced:** the dev dashboard's *Force sleep cycle* runs the nightly work now (it does not by itself put the entity to bed).

A rested entity by day, even if it asks to sleep, is ignored.

### Being asleep
Each asleep tick is 60 sim minutes, skips the brain and narrator, restores `sleep.restore_per_sim_hour` (12) energy per hour, decays needs at half speed, and still rolls events (which can still drain needs).

### Waking
It wakes when **energy ≥ `wake_energy_at_least` (40)** *and* it is past `wake_not_before_hour` (6) *and* it is not night, or after `sleep.max_hours` (12 sim hours) regardless. Starting from −25 at 12 energy/hour, reaching 40 takes about 5½ hours, so the 06:00 floor usually decides the wake time for a normal night.

### The sleep cycle (the nightly work)
When sleep begins, the sleeping mind makes **four LLM calls**. **Nothing is written unless all four succeed**; if the brain is offline the cycle stays *pending* and is retried on every sleeping tick (and it will run at the next onset otherwise). It uses the day's short-term memories and the log of ticks since the last completed sleep (the newest `day_log_max_ticks`, 40).

| Step | What the LLM says | What code does with it |
|---|---|---|
| 1. Consolidate | a one-or-two sentence summary and up to `max_new_memories` (5) lasting memories with an importance bucket | saved as long-term memories (importance low/medium/high → 0.25/0.55/0.85), embedded; short-term memories cleared; long-term pruned to `long_term_cap` (500) |
| 2. Tendencies | a nudge from −1 to 1 per tendency | scaled by `tendency_max_step` (0.05) and clamped to 0..1: a trait moves at most 0.05 a night. Core traits never change. |
| 3. Preferences | up to `max_preferences_per_night` (3) things liked or disliked, with a strength | blended with any existing opinion (`preference_blend` 0.5). A new opinion starts weak and strengthens only if the experience repeats; opinions weaker than `preference_min_abs` (0.1) are forgotten; at most `max_preferences_total` (40) kept |
| 4. Self-image | at most one first-person line with a confidence, optionally naming an existing line to revise or contradict | see §8 |

Every step's input, the LLM's raw proposal and what code actually applied are stored (the dashboard's **Sleep** page and a tick's *Sleep cycle* section).

### Dozing (the brain is offline)
If the brain cannot be reached the tick is a **doze**: 20 sim minutes, no LLM calls, needs decay, events still roll, `doze.energy_restore` (2) energy is restored, the diary shows "dozing". Consecutive dozes collapse into one diary entry on the website. When the brain returns, the first prompt says the entity has just come round from a hazy doze and omits the stale "last outcome".

---

## 6. Mood

Mood is **two numbers**, each from −50 to 50:

- **valence**: unpleasant (−) to pleasant (+)
- **arousal**: calm (−) to lively (+)

It is never set directly. It is recomputed every tick from:

**Valence** = the points `mood.valence_bands` gives each listed need for its *band* + the event nudge × `event_points` + the brain's own `feeling` (`feeling_values`).

| Need | empty | low | okay | full |
|---|---|---|---|---|
| sustenance | −20 | −6 | 0 | 0 |
| joy | −15 | −6 | +4 | +15 |
| peace | −12 | −5 | +3 | +12 |
| wonder, curiosity | not listed: add nothing | | | |

The event nudge is the sum of the `mood` values of events that fired, decayed by `event_decay` (0.7) each tick. The feeling adds −10 / 0 / +10.

**Arousal** = `arousal_base` (−10) + `arousal_energy_weight` (0.4) × energy + any matching `arousal_rules` (default: curiosity `full` **and** peace `low` or `empty` → +25, "curious and unsettled").

**Words.** If |valence| > `word_threshold` (12): positive → *content* (or *excited* if arousal ≥ `arousal_high` (5)), negative → *low* (or *tense*). Otherwise *calm* (or *restless*). The description adds the most pressing need by band ("calm, though peace is low") or the energy word. Mood only reaches the entity as words.

**Worked example (a fresh start).** All needs okay: valence = 0 (sustenance) + 4 (joy okay) + 3 (peace okay) = 7; feeling neutral → 7. Arousal = −10 + 0.4 × 30 = 2. |7| < 12 and 2 < 5 → **calm**.

**Where mood matters.** It never changes needs or choices directly. It affects: the words in the next prompt; the recall query (the mood word is part of it); the website's mood pad, mood word and the colour and motion of the abstract presence (colour from valence, breathing speed from arousal); and the dev dashboard.

---

## 7. Memory and recall

| Layer | Contents | How it is made | Lifetime |
|---|---|---|---|
| **Diary** | every tick: action, outcome, reaction, events, need changes | automatic | forever (it is what the website shows) |
| **Short-term** | "notable" ticks, today | during a tick | until the next sleep, which consolidates and clears them |
| **Long-term** | consolidated memories ("keepsakes") | the sleep cycle | kept up to `long_term_cap` (500); beyond that the least valuable (importance × recency) are dropped |

**A tick is notable** (and becomes a short-term memory) if any of these hold: a need moved at least `notable.need_change` (20) points in that tick; the brain's feeling is in `strong_feelings` (negative or positive); something new was created; a world change was rejected ("odd outcome"). Its importance is `importance_base` (0.3) + `importance_per_trigger` (0.2) × the number of triggers, capped at 1. The brain sees the most recent `short_term_shown` (8) short-term memories as "Earlier today".

**Recall** scores *every* long-term memory:

`score = w_recency × recency + w_importance × importance + w_relevance × relevance`

- *recency* halves every `recency_half_life_sim_hours` (48) sim hours since the memory was created or last recalled;
- *importance* is the stored 0..1 number;
- *relevance* is the cosine similarity between the memory and the situation query.

Defaults weigh relevance 1.5, recency 1.0, importance 1.0. The top `recall.k` (5) appear as "You remember". If the embedding model is unavailable, relevance is 0 and recall falls back to recency and importance. Recalled memories get their last-recalled time refreshed, so remembering something keeps it fresh.

---

## 8. Slow change: tendencies, preferences, self-image

Everything here changes only during sleep, a little at a time.

**Core traits** (`character.yaml`) never change. They are always in the prompt.

**Tendencies** (e.g. boldness, sociability, tidiness; each 0..1) drift ≤ 0.05 per night. They enter the prompt as labels ("bold", "neither timid nor bold") using `tendency_labels` and `tendency_words` (below 0.35 / above 0.65).

**Preferences** are learned likes and dislikes ("the old tree", "rain"). The brain sees up to `preferences_shown` (5) of each. Dislikes also have a **mechanical effect**: while a disliked thing is present, the world drains needs (`dislike_drain`). A dislike matches when its subject text appears (case-insensitive) in the current area's name, the name of an object here, the weather, or the entity's own action text. Up to `max_matches` (2) of the strongest dislikes count; each drains `points_per_hour × hours × |valence|` from every need in `dislike_drain.needs` (default peace and joy). So a strongly disliked "rain" costs about 4 peace and 4 joy per sim hour of rain; "the kitchen" disliked costs that whenever it is in the kitchen.

**Self-image** answers "what do I think I am?". It starts **empty**; an optional text hint can be set in `character.yaml` (`self_image.seed`). Each night the sleeping mind may propose **at most one** first-person line with a confidence. Code then:
- rejects empty text, non-numeric confidence, confidence below `self_image_min_confidence` (0.2), and duplicates;
- truncates to `max_self_image_length` (160);
- if the proposal names an existing line in `replaces`, **replaces** it (this is how a later line revises or contradicts an earlier one);
- keeps at most `max_self_image_lines` (5): over the cap the least confident (then oldest) is forgotten, which can be the new proposal itself.

The brain sees the lines (text only) and is told it must not assert a form, species or body that is not in them. If there are none, the prompt says the self-image is empty. The website's *What it thinks it is* panel shows the same lines.

---

## 9. The world: objects, events, and what the narrator may change

**Areas** (`world.yaml`): a name, a description and exits; exits must be mirrored. **Objects**: an id, name, area (or `inventory`), **tags** and a small state. Tags decide what is possible: `edible` (can be consumed), `movable` (can be taken and moved). Other tags (`warm`, `climbable`, `soft`, `toy`, …) are information for the narrator and entity. Objects are **seeded once**, on the very first start.

The narrator may only change the saved world through these operations (the same list is in the narrator's prompt). Changes are checked **in order**; a rejected one is logged and skipped, the rest still apply, and at most `max_changes_per_tick` (6) are accepted.

| Operation | Allowed when | Effect |
|---|---|---|
| `move_self {area}` | the area is connected to the current one | the entity moves (resets the stale counter) |
| `move_object {object_id, area}` | object movable and within reach; target here or adjacent | moves it |
| `take {object_id}` | movable, here, and the inventory holds fewer than `inventory_max` (3) | into the inventory |
| `drop {object_id}` | carried | into the current area |
| `consume {object_id}` | within reach and `edible` | destroys it; enables the sustenance gain |
| `set_state {object_id, key, value}` | within reach; key/value length-limited (`name_max_length`, `state_value_max_length`) | changes the object's state |
| `create {name, tags, area}` | current area; at most `create_limit_per_tick` (1); tags limited to `allowed_tags` | a new object |

If the story says the entity ate something but no valid `consume` came with it, the food is still there and sustenance does not rise. This is by design: reality wins. Objects spawned by world events are recorded the same way, so every object appearing or vanishing is auditable.

---

## 10. What each LLM sees

All fixed wording lives in `config/prompts.yaml` (see §13). The dynamic parts are filled in by code.

**The brain's prompt (system + situation)**
- *System:* who it is (name, backstory if any), the **Identity** statement (it has a name, a voice, core traits and tendencies, does not know what it is, must not assert a form that is not in its self-image), core traits, voice, and the JSON format.
- *Situation, every tick:* **Who you are right now** (self-image or "empty", tendencies, likes and dislikes) → **Now** (time, weather, place, mood and energy words) → **What's pulling at you right now** (§4) → **Around you** (objects here, carried items, exits) → **What just happened** (events, last outcome, recent actions) → **Memories** → "What do you do next?".
- It never contains need numbers, ids of objects, or a list of options.

**The narrator's prompt:** a neutral system prompt (no personality; it must respect the world as given, do not invent objects or exits; it only adds to needs, never subtracts; the exact JSON and the seven operations) plus the world description, each need's meaning and band word, and "The creature does: <action>".

**The sleeping mind's prompts:** the day's summary material for each of the four steps (§5).

---

## 11. When things go wrong: brain offline, downtime, crashes

| Situation | Behavior |
|---|---|
| **Ollama unreachable, or a call fails twice** | The tick is a **doze** (§5). The simulation keeps running; the website shows "dozing". Nothing is lost. The engine rechecks every tick. |
| **Invalid JSON from a model** | Counts as a failed call; retried `llm_retries` (1) times, then doze. |
| **Sleep cycle call fails** | Nothing from that night is written; the cycle stays pending and retries on the next sleeping tick. |
| **An unexpected error in a tick** | Logged with its trace; the engine waits an extra `error_backoff_seconds` (30) and carries on. |
| **Engine or laptop stopped** | Governed by `downtime_policy`: **`pause`** (default): sim time resumes exactly where it stopped and nothing decays during the gap. **`catch_up`**: the real gap is converted to sim time at the normal pace (`sim_minutes_per_tick_default` per `tick_interval_seconds`), capped at `max_catch_up_hours` (8), needs decay for it (× `catch_up_decay_multiplier`; slower if it was asleep, with energy restored), and **one** "dozed through…" diary entry is written. Gaps shorter than `catch_up_min_gap_ticks` (3) tick intervals count as normal restarts. A pause from the dashboard is not downtime. |
| **A crash mid-tick** | The tick's transaction never committed, so the previous state is intact. |
| **Config edited while a database exists** | At startup the engine reconciles the saved state with the YAML: new needs/tendencies start at their configured value, removed ones are dropped, an entity standing in a removed area goes to `start_area`. Objects are not re-seeded. |
| **Invalid config** | The engine refuses to start and prints every problem at once. |

---

## 12. Master table: triggers and their impact

| Trigger | When it fires | Immediate effect | Longer-term impact |
|---|---|---|---|
| **Tick timer** | every `tick_interval_seconds` | one tick (§3) | the simulation advances |
| **Event fires** | per-tick roll < `chance`, filters pass | text in next prompt; optional mood nudge, weather change, spawn, need drains | mood shifts; needs fall; new objects to use |
| **Need crosses into low/empty** | decay, drains or dislikes push a level below a threshold | its expression may be shown to the brain; its mood points change | the entity is *told* it is pulled; mood falls if joy/peace/sustenance |
| **Need crosses into full** | gains lift it past 75 | joy/peace add mood points; a full expression may show when nothing is pressing | the entity feels well |
| **Edible consumed** | a valid `consume` of an `edible` object | sustenance rises (+15 minimum); object destroyed | the only way sustenance recovers |
| **Repeated action in the same place** | similar to one of the last 6 actions there | wonder/curiosity gains × 0.4 | pressure toward variety |
| **Ticks in one area ≥ `stale_after_ticks`** | after 3 action ticks without moving | `stale_here` sentence in the prompt | nothing mechanical; only wording |
| **Disliked thing present** | a stored dislike matches area, object, weather or action | peace and joy drained per hour × strength | learned aversions have real cost |
| **Notable tick** | need jump ≥ 20, strong feeling, creation, rejected change | a short-term memory | consolidated into long-term at sleep |
| **Energy low (< −25) at night** | each night tick | falls asleep without a brain call | the nightly cycle runs |
| **Brain says `wants_sleep`** | energy < 10 or night | falls asleep | the nightly cycle runs |
| **Asleep** | each asleep tick | +12 energy/h, needs decay at half speed | wakes at energy ≥ 40 after 06:00 (or 12 h) |
| **Sleep cycle succeeds** | at sleep onset, brain online | memories consolidated; tendencies drift ≤ 0.05; preferences updated; ≤ 1 self-image line | personality, tastes and self-image evolve slowly; diary "Last night…" card |
| **Brain offline** | health check or call fails | doze tick: +2 energy, needs decay | a gap in the diary; nothing else lost |
| **Engine down** | process or host stopped | per `downtime_policy` | pause: nothing; catch_up: one entry and decay |
| **Config changed** | edit + restart/reload | reconciled or applied | see §13 for what applies when |

---

## 13. Tuning guide

### Where to edit, and when it applies

| File | What it holds | Applies |
|---|---|---|
| `config/settings.yaml` | every tuning number | live with the dashboard's **Reload settings**, or on restart |
| `config/prompts.yaml` | all fixed prompt text | live with **Reload settings**, or on restart |
| `config/character.yaml` | name, traits, tendencies, needs (meaning, expressions, decay), `stale_here`, self-image seed | **restart** (reconciled with the saved state) |
| `config/world.yaml` | areas, objects, weather, events | **restart**; objects only change on a **reset** (§14) |

Paths and models (`db_path`, `ollama_host`, model names) are best changed with a restart. If a file is invalid the reload changes nothing and the dashboard shows the error; a restart refuses to start and prints every problem. Edit, then watch the dashboard's **Config** page (loaded vs on-disk diff) and the **Needs & mood** page to see the effect.

Tuning principle: change **one thing at a time**, let it run for a few sim days, and read the **Needs & mood** charts and the tick pages before changing more. Most behavior comes from the *balance* between decay (always falling) and gains (the narrator's judgement), so the same number can feel different after the narrator model changes.

### Pace

| Parameter | Effect | Guidance |
|---|---|---|
| `tick_interval_seconds` | real seconds per tick | lower = faster life and more LLM load. Keep it above the time a tick takes (see the tick time on the dashboard's Overview), or ticks queue up |
| `sim_minutes_per_tick_default` | sim minutes when the narrator gives no duration; also the catch-up pace | |
| `sim_minutes_min` / `max` | clamp on the narrator's duration | raising `max` lets single actions pass more world time (and more decay) |
| `phases` | when night starts | moving `night` earlier/later changes when exhaustion forces sleep |
| `sim_start` | the clock's start | only used on the first start (a reset to change it) |

### Needs: how hungry, how bored

| Parameter | Effect | Raise → | Lower → |
|---|---|---|---|
| `decay_per_hour` (per need, `character.yaml`) | how fast a need falls | the entity is pulled by it sooner and more often | the need rarely matters. Set 0 to freeze a need |
| `start` (per need) | initial level | starts better off | starts already pulled |
| `need_bands.*_below` | where low/empty/okay/full begin | *higher* `low_below`: needs count as low earlier (more pulling, more mood loss) | lower: tolerates more before it speaks up |
| `max_pressing_needs` | how many expressions are shown | a noisier, more conflicted inner voice | a single dominant concern. `1` is very focused |
| `gain_values` | points per `+` / `++` | actions refill needs faster (needs stay full) | refilling is slow; needs stay pulled |
| `consume_min_gain` | minimum sustenance for eating | | |
| `repetition_penalty.multiplier` | how hard repeats are penalised | closer to 1 = barely any penalty | closer to 0 = repeated actions give nothing |
| `repetition_penalty.window` / `similarity_threshold` | how many past actions, how alike counts as the same | lower threshold = more things count as repeats | |
| `repetition_penalty.needs` | which needs get penalised | add needs to make them depend on novelty | |
| `stale_after_ticks` | how soon "this place is used up" appears | | `0` disables |
| `dislike_drain.points_per_hour` / `max_matches` / `needs` | how costly dislikes are | stronger aversion | `0` makes dislikes cosmetic |
| event `drains` (`world.yaml`) | one-off hits | e.g. `{peace: 15}` for a loud event | |

Reading the numbers: a need that decays 5/hour needs about +15 every 3 sim hours just to hold steady, i.e. one `+` per ~3 hours of activity.

### Energy and sleep

| Parameter | Effect |
|---|---|
| `energy_start` | starting energy (−50..50) |
| `effort_energy_cost` | what low/medium/high effort costs. Raising it makes the entity tire sooner and sleep more |
| `sleep.fall_asleep_energy_below` | night exhaustion threshold. Raise it (e.g. −10) to make sleep come earlier; lower (−40) to let it stay up |
| `sleep.wants_sleep_energy_below` | how tired before the brain's request is honoured by day. Set to −50 to allow sleep only at night |
| `sleep.restore_per_sim_hour` | how fast sleep restores energy (and so how long the night is, together with `wake_*`) |
| `sleep.wake_energy_at_least` / `wake_not_before_hour` / `max_hours` | when it wakes |
| `sleep.decay_multiplier` | how much needs fall while asleep (1 = same as awake; 0 = frozen) |
| `doze.energy_restore`, `doze.sim_minutes` | what an outage tick is worth |

### Mood

| Parameter | Effect |
|---|---|
| `mood.valence_bands` | the whole mood map: which needs and bands move valence, by how many points. Add `wonder: {empty: -8}` to let wonder matter, or remove joy to decouple it |
| `mood.arousal_base`, `arousal_energy_weight` | baseline liveliness; tired entities calm down |
| `mood.arousal_rules` | band combinations that make it lively or flat. Add `{when: {sustenance: [empty]}, add: 15}` for "frantic when starving" |
| `event_points`, `event_decay` | how strongly and how long events colour mood |
| `feeling_values` | how much the brain's own feeling counts |
| `word_threshold`, `arousal_high` | where the mood words flip |

### Memory, personality

| Parameter | Effect |
|---|---|
| `notable.need_change` etc. | what counts as a memorable moment (lower = more short-term memories) |
| `recall.k`, `w_recency`, `w_importance`, `w_relevance`, `recency_half_life_sim_hours` | what is remembered when; raise `w_relevance` to remember on association, raise the half-life to remember longer |
| `long_term_cap` | how many keepsakes are kept |
| `sleep.tendency_max_step` | how fast personality can drift (0 freezes it) |
| `sleep.preference_blend`, `max_preferences_per_night`, `preference_min_abs`, `max_preferences_total` | how fast tastes form and fade |
| `sleep.max_self_image_lines`, `self_image_min_confidence`, `max_self_image_length` | how much self-image it can hold and how sure it must be |
| `recent_actions_shown`, `preferences_shown`, `short_term_shown` | how much context the brain gets (more = longer prompts; mind `llm_num_ctx`) |

### The LLMs
`brain_model`, `narrator_model`, `sleep_model`, `embed_model`, `llm_temperature` (brain 0.9 = adventurous, narrator 0.5 = steady), `llm_num_ctx` (4096; raise if prompts get truncated), `llm_retries`, `llm_timeout_seconds`. Changing `embed_model` requires re-embedding stored memories: `scripts/reembed.py`.

### The words (prompts)
All of the following are in `config/prompts.yaml`; each entry's comment lists its `{placeholders}`:
- **Brain system:** `intro`, `identity`, `core_traits`, `voice`, `output` (the JSON contract).
- **Brain situation:** headings and sentences, including `heading_pulling` and `none_pulling`.
- **Narrator:** `system` (rules, the seven operations; keep the doubled braces `{{ }}`), `needs_heading`, `need_line`, `need_consume_note`, `user`.
- **Sleep:** `system`, the four `jobs`, and the four step prompts.

The validator refuses an empty or missing entry or an unknown `{placeholder}` and reports every problem. Keep the JSON-format instructions intact (the models are also given a JSON schema, but the text helps). Never put need numbers or a list of options in a prompt: that breaks the design rule that needs shape what is *said*, not what is *chosen*.

For the character's voice and wording of needs, edit `character.yaml`: `meaning` (what a need is; shown to the narrator and on the website), `expressions` (the four band texts the brain reads), `stale_here`, `voice`, `backstory`, `core_traits`.

### Recipes

| Goal | Change |
|---|---|
| A **hungrier**, more driven entity | raise `sustenance` `decay_per_hour` to 8; raise `need_bands.low_below` to 50 |
| An entity that **rarely feels anything pressing** | lower `need_bands.low_below` to 30 and `empty_below` to 10; set `max_pressing_needs: 1` |
| **Calmer, slower life** | `tick_interval_seconds: 180`; lower `joy` and `curiosity` decay |
| **More varied behavior** | `repetition_penalty.multiplier: 0.2`, `stale_after_ticks: 2`, raise `llm_temperature.brain` to 1.0 |
| **Sleeps more** | `effort_energy_cost: {low: 3, medium: 9, high: 18}`; `sleep.fall_asleep_energy_below: -10` |
| **Never sleeps by day** | `sleep.wants_sleep_energy_below: -50` |
| **Moodier** | raise `valence_bands` magnitudes (e.g. joy full 25, empty −25) and `event_points` to 30 |
| **Remembers more** | raise `recall.k` to 8 and `recency_half_life_sim_hours` to 120 |
| **Faster personality drift** | `sleep.tendency_max_step: 0.1` |
| **No dislike penalty** | `dislike_drain.points_per_hour: 0` |
| Make a **new need** | add it to `character.yaml` (start, decay_per_hour, meaning, all required expressions); it joins at its start value after a restart. Optionally list it in `mood.valence_bands`, `repetition_penalty.needs`, `dislike_drain.needs` |
| **Remove a need** | delete it from `character.yaml` *and* from any `settings.yaml` / `world.yaml` list that names it (the validator names each leftover) |

### Safety notes
- The dashboard's **Reload settings** validates all four YAML files first; an error changes nothing.
- Values outside their range are refused at startup (e.g. band thresholds must ascend; mood points −50..50; `max_pressing_needs` ≥ 1).
- Raw tweaks to `decay_per_hour` etc. change the *whole feel*; keep a backup of the YAML (it is in git) so you can diff.

---

## 14. Resetting the simulation

Everything the entity *is* (its memories, tastes, self-image, needs, location, clock and history) lives in **one SQLite file**, `data/cat.db` (plus the `-wal` and `-shm` files SQLite keeps beside it). Configuration lives in `config/` and the language models live in the `ollama` Docker volume. A reset only touches the first.

> Do these on the host; the repo's rules say not to run Docker or deploy commands on your behalf, so they are written out here for you to run.

### A. Full reset (start the life over)

What it does: back to `sim_start`, at `start_area`, needs at their `start` values, energy at `energy_start`, tendencies at their configured values, **no memories, no preferences, empty self-image, empty diary**, and the world's objects re-seeded from `world.yaml`. The website shows "the journal is empty. <name> hasn't woken up yet." until the first tick.

```bash
cd /path/to/internet-cat

# 1. Stop the writer and the readers (devdash is read-only but keeps the DB open)
docker compose stop engine web devdash

# 2. Keep a copy of the old life (optional but recommended)
mkdir -p data/old-$(date +%F-%H%M)
mv data/cat.db data/cat.db-wal data/cat.db-shm data/old-$(date +%F-%H%M)/ 2>/dev/null

# 3. (optional) also clear the dashboard's own database, logs and heartbeat
mv data/devdash.db data/engine.heartbeat data/old-$(date +%F-%H%M)/ 2>/dev/null
# rm -rf data/logs            # structured logs; they are recreated

# 4. Start again: the engine creates a new database and seeds the world
docker compose up -d engine web devdash
docker compose logs -f engine
```

Notes:
- Keep `data/backups/`: it holds your snapshots (`deploy/backup.sh`, dashboard *Snapshot DB*).
- `data/devdash.db` holds the dashboard's command queue, playground runs and engine status; clearing it is harmless and optional.
- The models in the `ollama` volume are untouched; no re-download.
- To change the starting point, edit `config/*.yaml` **before** step 4: `sim_start`, `start_area`, needs' `start`, `energy_start`, `tendencies`, the `objects` list. These are only read when a database is created.
- Without Docker: stop the `python -m cat.main` process (and the web/devdash processes), `mv data/cat.db* data/old/`, start it again.
- To go back to an earlier life instead, restore a backup (stop engine/web, `cp data/backups/cat-YYYY-MM-DD.db data/cat.db`, `rm -f data/cat.db-wal data/cat.db-shm`, start).

**Check it worked:** the engine log shows the world being seeded; the website shows the empty journal; the dashboard *Overview* shows tick 0; after the first tick the sim clock reads `sim_start`.

### B. Partial resets (engine stopped, `sqlite3` on the host)

Use these to wipe one aspect while keeping the rest. Stop the engine first and take a copy (`cp data/cat.db data/cat.db.bak`). Restart afterwards.

| You want | Run against `data/cat.db` | Effect |
|---|---|---|
| Forget all memories (short- and long-term) | `DELETE FROM memories;` | no keepsakes; recall returns nothing until the next sleep |
| Forget what it thinks it is | `DELETE FROM self_image;` | self-image is empty again (the config hint, if any, remains) |
| Forget likes and dislikes | `DELETE FROM preferences;` | no preferences; dislike drains stop |
| Reset all needs to their starting values | `UPDATE state SET needs = json_object('sustenance',70,'wonder',60,'joy',60,'peace',70,'curiosity',50);` (use your own ids and `start` values) | the needs only; energy and mood are recomputed on the next tick |
| Reset energy | `UPDATE state SET energy = 30;` | back to `energy_start` |
| Reset tendencies to config | `UPDATE state SET tendencies = json_object('boldness',0.5,'sociability',0.5,'tidiness',0.5);` | |
| Send it home | `UPDATE state SET area = 'living_room';` | |
| Wake it / clear a stuck sleep | `UPDATE state SET is_asleep = 0;` | |
| Clear the pending sleep cycle | `UPDATE meta SET value='0' WHERE key='sleep_pending';` | skips an unfinished night |

Do **not** delete rows from `ticks`, `objects` or `state`: the diary, the world and the live state depend on each other (the nightly log reads the ticks since the last sleep, and objects are only seeded on a fresh database). For a clean diary or a re-seeded world use the full reset (A).

### C. Other things people mean by "reset"
- **Change the name or character** without losing the life: edit `character.yaml` and restart. The name appears in prompts straight away; old memories still reflect the old circumstances. New needs/tendencies are added at their start value.
- **Change the world's objects**: needs a full reset (objects are seeded once).
- **Re-do a night**: not supported; use the dashboard's *Force sleep cycle* to run the nightly work again on the current day.
- **Wipe the LLM models**: only if you want to (`docker volume rm` of the `ollama` volume, then `deploy/pull-models.sh`). It is never part of a simulation reset.

---

## 15. Watching and debugging

The **dev dashboard** (`http://127.0.0.1:8090`, see the README) explains every number:

| To answer… | Look at |
|---|---|
| Why did it do/feel that? | **Ticks → tick detail**: the prompts, raw responses, which need expressions were selected and why, recall scores, event rolls, effect validation |
| Why did a need go up or down? | tick detail → **Need math** (before, decay, event drain, dislike drain, bucket, repetition, clamp, after) |
| Why is the mood *calm*? | tick detail → **Mood math** (every band term, event nudge, feeling, arousal terms) |
| Is it running well? | **Overview** (tick time, doze count, errors, service health); **LLM calls** (latency, invalid JSON) |
| How are needs and mood trending? | **Needs & mood** (charts over 24 h / 7 d / all, sleep and doze shaded, critical time per need) |
| What did last night do? | **Sleep** (each step's input, proposal and what code applied; tendency history) |
| What does it remember? | **Memory** (short/long-term, recall stats, embedding map, search) |
| Are my edits live? | **Config** (loaded vs on disk; *Reload settings*) |
| Try a different prompt or model | **Playground** (replay any stored call, edit and compare) |

Dev controls (pause, resume, step one tick, set interval, force sleep cycle, reload settings, inject an event, snapshot DB) are queued in `data/devdash.db` and run by the engine within about two seconds. **Step** is the best tool for tuning: pause, step once, read the tick page, adjust, reload, repeat.

From a terminal, `.venv/bin/python scripts/show_diary.py -n 50` prints the diary, and `.venv/bin/python -m cat.main --mock --ticks 100` runs a whole day-cycle with no Ollama (a mock brain) to check a config change without spending LLM time.

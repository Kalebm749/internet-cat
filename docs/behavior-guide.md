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
8. [Slow change: tendencies, preferences, self-image, projects](#8-slow-change-tendencies-preferences-self-image-projects)
9. [The world: objects, events, and what changes the world](#9-the-world-objects-events-and-what-changes-the-world)
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
| **Code (the engine)** | the clock, need levels and decay, energy, mood arithmetic, **the menu of moves and what each move does** (gains, effort, duration, world change), which memories are kept, validation of every world change, the saved state | invent story |
| **The brain (LLM call #1)** | what the entity thinks and feels, and which **move from the menu** it chooses and why | say what happened, change a need, or change the world |
| **The journal (LLM call #2, in the entity's own voice)** | its first-person entry for the moment, written **after** code decided what happened, from facts only | add any detail the facts do not contain, change any number or the world |

A third LLM role, **the world director**, proposes the occasional small happening in the entity's area (§3, §9). A fourth, **the sleeping mind**, runs once per night and does the slow work: consolidating memories, nudging tendencies, forming preferences, and proposing at most one self-image line.

Some consequences that explain almost every behavior you will see:

- **Moves are closed.** The entity chooses from a menu built by code from config/moves.yaml (go somewhere, eat, play with, rest on, climb, examine, take, drop, look out, wait, work on a project, sleep). Code decides each move's effect and the saved world changes only through validated operations (§9), so a story can never invent a fort that does not exist. The menu is never ranked or marked by need.
- **The entity never grades itself.** Gains come from the move's configured effects; losses come from the world (decay, events, dislikes), and repetition shrinks gains.
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
| Normal (awake, brain online) | the move's duration (drawn by code from the verb's `minutes`), clamped to `sim_minutes_min`..`sim_minutes_max` (5..90). A move longer than `activity.step_minutes` spreads over carry ticks of that length | code |
| Doze (brain offline) | `doze.sim_minutes` (20) | code |
| Asleep | `sleep.sim_minutes_per_tick` (60) | code |
| Catch-up (after downtime, §11) | the gap converted at the default pace, capped | code |

So the pace of the simulation is **not fixed**: roughly, with 20-minute actions and a 90-second interval, 40 ticks happen per real hour, which is about 13 sim hours per real hour (a sim day passes in a bit under two real hours). Longer moves make the world move faster per decision, and carry ticks make no LLM calls; a sleeping night is only about 8-12 ticks (≈ 12-18 real minutes).

**Phases** (`phases`, by sim hour): `dawn` 5, `day` 8, `dusk` 18, `night` 21 (night lasts until dawn). The phase matters for exactly two things: falling asleep (§5) and the website's colour palette. The clock also gives the entity a day number and "part of day" words in its prompt.

`tick_interval_seconds: 0` runs as fast as the machine allows (used for tests and `--interval 0`).

---

## 3. Anatomy of one tick

Everything below happens in this order. The LLM calls happen **outside** any database transaction; the apply-and-record step is **one transaction**, so a crash leaves the previous state intact.

```
  1. World events roll            ── events may fire; may carry mood nudge, weather change, spawn, drains
     + the world director        ── every few ticks, one LLM-proposed happening (validated by code)
  2. Rest check                   ── asleep? or exhausted at night?  → a rest tick (no LLM), skip to 6
  3. Brain online?                ── cached health check (GET /api/tags); offline → doze tick, skip to 5
  4a. Recall + Brain call         ── builds the prompt, picks the need expressions, asks for JSON
  4b. Resolve (code)              ── the move → gains, effort, duration, world change, plain outcome text
  5. Apply (code)                 ── validate changes → decay → event drains → dislike drains → gains
                                     → spend energy → clamp → mood
  5b. Journal call                ── facts of what happened → its own first-person entry (no numbers)
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
- **spawn an object** (skipped if the area already holds `max_objects_per_area` objects). What the world brings does not last for ever: after `object_lifetime_hours` sim hours (by who made it: `event` 18, `director` 30) it is gone at the start of a tick, recorded in that tick's changes as a `despawn` (the diary shows "− a fresh loaf of bread (gone)" when it was in the creature's place). Carried things and the objects the world started with never expire. Without this the areas fill up and later spawns are silently skipped,
- **drain needs** (`drains: {need: points}`): subtracted from those needs in step 5.

Events roll on *every* tick type, including sleeping and dozing, so a loud noise can drain peace while the entity sleeps.

**The world director.** The hand-written events are a small, fixed table, so on their own the world runs out of novelty. After they roll, an LLM role called **the director** may add one more happening, in the same shape as an event:
- **When:** every `director.every_ticks` (6) action ticks, or every `stale_every_ticks` (2) when the entity has stayed in one place (the stale flag, §4). Never while asleep or exhausted at night, never with the brain offline, and only if the tick has not already used `events_max_per_tick`.
- **What it sees:** the place and its objects, the time, the weather, the last `avoid_repeat` (10) happenings, and its last 3 actions. It **never sees needs or mood** and is told not to rescue, reward or punish: it answers to the place, the hour and the weather, so the world cannot quietly steer the entity by its state.
- **What it may propose:** one or two sentences of what happens; a small mood nudge (clamped to ±`max_mood`, 0.5); a weather change (only to a weather name in `world.yaml`); one small object (only `allowed_tags`, only if the area is not full); and optionally a small drain on at most `max_drain_needs` (2) needs, at most `max_drain_points` (6) each. Most of the time it should say nothing happens.
- **What code does:** rejects empty text and anything that repeats a recent happening; clamps and filters the rest (notes say what was ignored); applies the result exactly like an event, so the brain perceives it in "What just happened", spawns obey the area cap, drains show in the need math, and it appears in the diary as a margin note.
- **If the call fails** the tick simply has no director happening; it never dozes the tick. Every run and outcome is traced (the dashboard's tick page shows it).
- **Turn it off** with `director.enabled: false` to get only the hand-written events. The hand-written events keep rolling either way; use them for things you always want possible.

### Step 2: rest check
- If the entity is **asleep**, the tick is a rest tick.
- If it is **night** and energy is below `sleep.fall_asleep_energy_below` (−25), it **falls asleep** now: a rest tick with onset, and **no brain call**. Exhaustion at night overrides choice.
- Exhaustion by *day* does not force sleep; it only makes sleep available to the brain (§5).

### Step 3: is the brain reachable?
A cheap check against Ollama (`/api/tags`, `health_timeout_seconds`), cached for the tick. With `health_require_models: true` the brain counts as online only if all configured models are pulled. If it is offline, or a call still fails after `llm_retries` (1) retry, the tick becomes a **doze** (§5, §11).

### Step 4a: recall, the menu and the brain call
1. **Recall** (only if there are long-term memories): the engine builds a short query from the current area, the objects here, the mood word and what just happened, embeds it, and scores every long-term memory (§7). The top `recall.k` (5) go into the prompt.
2. **The menu**: code builds the list of moves that are possible right now from config/moves.yaml (go to each area, eat/play with/rest on/climb/examine/take each fitting object, drop a carried thing, look out, wait, work on each active project, and **sleep only when sleep is allowed**). The order is a **seeded shuffle** per decision (`moves.shuffle`; same time and place, same order): in file order the first real run took 40% of its picks from the top tenth of the menu, so list position was steering it. It is **never ranked, filtered or marked by need**. At most `moves.max_offered` are shown. The menu opens with "Every move listed here is something you can actually do. What a move does to you, you find out by doing it." (`brain.situation.moves_note`), and a move whose verb it has never made ends with "(never tried)" (`moves.mark_untried`): a fact of its own history that gives curiosity something to find out, never a ranking by need.
3. **Prompt** (§10): self-image, tendencies, habits ("you tend to…"), likes/dislikes, time and place, mood and energy words, **"What's pulling at you right now"**, objects around, what just happened, recent actions, memories, active projects, and the menu.
4. The brain returns JSON: `reaction, thought, move (a menu id such as m3), why, feeling (negative|neutral|positive)`. A `move` that is not on the menu is invalid and counts as a failed call. It does **not** write the journal entry: it decides before anything has happened, so it could only say what it means to do.

**What the creature perceives and remembers is what happened, not what an LLM wrote about it.** Code's plain account of the move (`ticks.result`, the same text as `ticks.outcome`) is what the diary shows as having happened, what the next prompt's "What just happened" says, and what the sleep cycle's day log (from which lasting memories are written) contains. Nothing invented (a glint, a smell, a frayed edge) can reach the diary's world line, the creature's perception or its lasting memories. In the consolidation prompt, words after "You wrote" are marked as what it said, not what happened.

### Step 4b: resolve the move (code)
**Code decides what the move does** (`cat/moves.py`): the need gains (`effects` and `by_tag` buckets, `novelty` for a place not visited for `moves.novelty_hours`), the effort, a duration drawn from the verb's `minutes`, the world change (go, take, drop, consume, validated by `cat/effects.py`), and the plain outcome text (the verb's `outcome`). A verb with `reveals: true` (look closely at, inspect, turn over, tap, …) also says what is true of the target, from world.yaml `tag_facts` ("You looked closely at a loose ball of yarn. It is something to play with and can be moved."); a thing with no such facts gives "Nothing more about it stands out." A project move reports validated progress. When a project milestone or the finish is reached, a separate call asks the brain, in its own voice, for the piece it made.

### Step 5b: the journal (LLM, its own voice)
After code has decided everything, **one call writes the creature's own entry** (`cat/journal.py`, `brain_model`, prompts under `journal` in prompts.yaml). It is given facts only: what it chose and the reason it gave (or that an impulse took over), code's plain account of what happened, events around it, which needs crossed into another band (as that band's own expression, never a need's name or a number), how it is now (the same impression lines as the brain), and any memories that came to mind. It is told not to add details the facts do not contain, to mention memories, likes, tendencies or projects only when they bear on the moment, never to name its own traits or needs as labels, and that saying little ("I don't know") is fine. It decides nothing and changes nothing. If the call fails (or `journal.enabled` is false) the moment has no entry and the diary shows the reason it gave instead; an impulse keeps its fixed first-person line. Carried-on ticks, rest ticks and dozes make no journal call. A decision therefore still costs two chat calls (brain, journal).

### Step 5: apply, in this exact order
1. **Validate and apply world changes** (§9). Rejected changes are logged and do not happen.
2. **Decay** every need by `decay_per_hour × duration/60`.
3. **Event drains**: subtract the drains of the events that fired.
4. **Dislike drains**: if something the entity dislikes is present, subtract from the configured needs (§4). (Awake and dozing ticks only; a sleeping entity is spared.)
5. **Project work** (§8): if the move is `work_on` an active project, advance it (a tapering step), keep the note, save any artifact, and **exempt this action from the repetition penalty**; finishing a project pays `projects.completion_gains`.
6. **Gains**: convert the move's buckets (`+` = 7, `++` = 14, `meal` = 28) to points, **scaled by the move's length** (a move of `moves.reference_minutes`, 20, gives its full amount; shorter gives proportionally less, down to `moves.min_scale`; longer more, up to `moves.max_scale`), **adding only**, with the two special rules: sustenance rises only through a `consume` move; the gains of the repetition-penalised needs shrink if the same move repeats.
7. **Spend energy**: the action's `effort` costs 2 / 6 / 12 energy.
8. **Clamp**: needs to 0..100, energy to −50..50.
9. **Mood** is recomputed from the new bands, the event nudge, the brain's `feeling` and the energy (§6).

### Step 6: record
One transaction writes the diary tick, the events that fired, object changes, a **short-term memory** if the tick was notable (§7), the new state, and (for stale tracking) how long the entity has been in this area. A full trace (prompts, responses, recall scores, need arithmetic, mood arithmetic, event rolls) is stored for the dashboard. Tracing never breaks the simulation: a failed trace write is only a warning.

### Step 7: maybe sleep
If the move chosen was **sleep** (offered only when energy < `sleep.wants_sleep_energy_below` (10) **or** it is night), the entity falls asleep and the sleep cycle (§5) runs straight after the tick commits.

### Activities: a long move is one decision
A move longer than `activity.step_minutes` (20) becomes an **activity**. Its gains and effort are paid when it is chosen; the following **carry ticks** advance 20 sim minutes each, decay needs and apply drains, and make **no LLM calls**. An activity ends when its time is up, or is **interrupted** by any event (`interrupt_on_events`) or a need newly dropping into *empty* (`interrupt_on_empty_need`); falling asleep or exhaustion also ends it. After an interruption the next prompt says what interrupted it. Carry ticks wait `carry_interval_seconds` instead of a full tick interval. The diary shows one entry ("about N min", "because: …", "carried on for N more steps"). Turn it off with `activity.enabled: false`.

---

## 4. Needs

Five needs in the placeholder character, each 0..100: **sustenance, wonder, joy, peace, curiosity**. Each has a `decay_per_hour`, a `meaning` and four text `expressions` (`full`, `okay`, `low`, `empty`). Which needs exist, and what they mean, is entirely up to `character.yaml`.

### Levels become bands
`need_bands` turns a level into a band: below `empty_below` (15) is **empty**, below `low_below` (40) is **low**, below `okay_below` (75) is **okay**, otherwise **full**. The *only* things that ever see a band's word are the prompts, the website, and the mood mapping. Raw numbers go to the database and the dashboard.

### What changes a need
| Cause | Direction | Details |
|---|---|---|
| **Decay** | down | `decay_per_hour` × sim hours. The baseline: needs always fall. Slower while asleep (`sleep.decay_multiplier` 0.3). |
| **Move gains** | up | The move's configured buckets, `+` (7), `++` (14) or `meal` (28), scaled by how long the move takes, decided by code from moves.yaml. They only add. |
| **Event drains** | down | `drains` on a world event, applied when it fires. |
| **Dislike drains** | down | While something it dislikes is present (§8). |
| **Repetition** | shrinks *gains* | A repeated action pays less (below). |

A move never subtracts: nothing a move does drains a need. Bad things that happen are story; the harm lands through decay, events or dislikes, and through mood (§6).

### The sustenance rule (hard rule)
`sustenance_need` (default `sustenance`) rises **only** through a `consume` move on an `edible` object (a validated `consume` change). It gets the move's gain, and at least `consume_min_gain` (`+`).

### Repetition pays less
Each move has a key (`verb` or `verb:target`). If the same key was chosen in the same place within the last `repetition_penalty.window` (6) decisions, the gains of the needs listed in `repetition_penalty.needs` (**wonder, curiosity**) are multiplied by `multiplier` (0.4). Other needs, and sustenance, are never shrunk. This is read from the `ticks` table, so it is deterministic and survives a restart; no embeddings are involved. Moving to another place resets it. The penalty is flat (not growing with repeats) and only touches the listed needs. `work_on` a project is exempt (§8). Carry ticks are not decisions and do not count.

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
| peace | 70 | 4 | 7.5 h | 13.75 h |
| curiosity | 50 | 5 | 2 h | 7 h |

At ~13 sim hours per real hour, an entity that never eats and never has a good moment is empty on sustenance about 50 real minutes in. Gains (+15/+30) from moves are what push levels back up. To reach `full` (≥ 75) a need must be fed well above its start.

---

## 5. Energy, sleep and dozing

### Energy is not a need
**Energy** is a separate resource, a single number from **−50 to 50**, starting at `energy_start` (30). It is **spent by effort** (low 2, medium 6, high 12 points per 20 minutes of the move, scaled by its length; resting costs none: `effort_energy_cost`) and **restored only by sleeping or dozing**. It is never derived from needs and no LLM can touch it. Its words (`energy_words`): exhausted (< −25), tired (< 0), fine (< 25), wide awake (≥ 25). The words go into the prompt ("Energy: tired"); the number does not.

### Falling asleep: three ways
1. **Exhaustion at night:** night **and** energy < −25 → falls asleep immediately, with no brain call.
2. **The brain's choice:** the **sleep** move is on the menu only when energy < `sleep.wants_sleep_energy_below` (10) *or* it is night, and the brain picks it.
3. **Forced:** the dev dashboard's *Force sleep cycle* runs the nightly work now (it does not by itself put the entity to bed).

A rested entity by day, even if it asks to sleep, is ignored.

### Being asleep
Each asleep tick is 60 sim minutes, skips the brain and journal, restores `sleep.restore_per_sim_hour` (12) energy per hour, decays needs at `sleep.decay_multiplier` (0.3) of the awake speed, and still rolls events (which can still drain needs).

### Waking
It wakes when **energy ≥ `wake_energy_at_least` (40)** *and* it is past `wake_not_before_hour` (6) *and* it is not night, or after `sleep.max_hours` (12 sim hours) regardless. Starting from −25 at 12 energy/hour, reaching 40 takes about 5½ hours, so the 06:00 floor usually decides the wake time for a normal night.

### The sleep cycle (the nightly work)
When sleep begins, the sleeping mind makes **up to six LLM calls**. **Nothing is written unless all succeed**; if the brain is offline the cycle stays *pending* and is retried on every sleeping tick (and it will run at the next onset otherwise). It uses the day's short-term memories and the log of ticks since the last completed sleep (the newest `day_log_max_ticks`, 40).

| Step | What the LLM says | What code does with it |
|---|---|---|
| 1. Consolidate | a one-or-two sentence summary and up to `max_new_memories` (5) lasting memories with an importance bucket | saved as long-term memories (importance low/medium/high → 0.25/0.55/0.85), embedded; short-term memories cleared; long-term pruned to `long_term_cap` (500) |
| 2. Tendencies | open-ended: the ways it tends to be, **in its own words** (a short phrase each), each with a nudge from −1 to 1; it may add at most `max_new_tendencies_per_night` (1) new one | nothing is predefined. A new one starts at `tendency_start` (0.3), then every nudge is scaled by `tendency_max_step` (0.05) and clamped to 0..1. At most `max_tendencies` (8) are kept; one that fades below `tendency_forget_below` (0.05) is forgotten. Core traits never change. |
| 3. Preferences | up to `max_preferences_per_night` (3) things liked or disliked, with a strength | blended with any existing opinion (`preference_blend` 0.5). A new opinion starts weak and strengthens only if the experience repeats; opinions weaker than `preference_min_abs` (0.1) are forgotten; at most `max_preferences_total` (40) kept |
| 4. Self-image | at most one first-person line with a confidence, optionally naming an existing line to revise or contradict | see §8 |
| 5. Projects | at most one change: start a long-term project from an enjoyed activity it keeps returning to, or pause, resume or drop one | see §8 |
| 6. Habits | one short second-person line per pattern **code** found (a move made at least `habits.min_count` times and at least `habits.min_share` of the decisions in one part of the day, over the last `habits.window_ticks` decisions). Skipped when nothing recurs or `habits.enabled: false` | the stored set is replaced: patterns that stopped recurring disappear, a failed or empty line falls back to the existing wording or the fallback template. The brain sees at most `habits.max_shown` lines |

Every step's input, the LLM's raw proposal and what code actually applied are stored (the dashboard's **Sleep** page and a tick's *Sleep cycle* section).

### Dozing (the brain is offline)
If the brain cannot be reached the tick is a **doze**: 20 sim minutes, no LLM calls, needs decay, events still roll, `doze.energy_restore` (2) energy is restored, the diary shows "dozing". Consecutive dozes collapse into one diary entry on the website. When the brain returns, the first prompt says the entity has just come round from a hazy doze and omits the stale "last outcome".

---

## 6. Mood

### Anxiety (the third gauge, beside mood and energy)
**Anxiety** (0..100) is how pressed the creature feels. Each tick it climbs toward a target (`anxiety.band_points`: every need in the *low* or *empty* band adds points, and `anxiety.energy_points`: being tired adds more) at `rise_per_hour`, and eases back at `fall_per_hour` when the target is lower (twice as fast asleep). So it builds while needs stay unmet and drains once they are fed. Consequences, all in settings `anxiety.*` and traced on the tick page:
- **Mood** loses `mood_weight` points per anxiety point.
- **Needs under pressure:** needs already low or empty decay up to `decay_pressure` (25%) faster at full anxiety; feeding them releases it (so there is no endless spiral).
- **The prompt** adds a short *impression* above the calm band (for example "Your thoughts keep circling, braced for something."), chosen by the band word in `anxiety.words`, wording in prompts.yaml. It never says "you feel…" and never names an emotion: states are conveyed as what they are like from inside, the same way need expressions are. The mood line works the same way ("Things are even and quiet in you.").
- **Sleep** restores up to `sleep_restore_penalty` (40%) less energy at full anxiety.
- **Impulses:** above `impulse_above` (40) each decision may be taken over, with a chance rising to `impulse_max_chance` (50%) at 100. Seeded code picks an impulse (`anxiety.impulses`: restless or freeze, each with a weight and the verbs it acts through) and one matching move from the *current menu*, never by what would feed a need. The roll comes **before** the brain: when an impulse fires there is no brain call at all (the fast path), so no thought, reaction or feeling is recorded for a move it did not choose. The diary then carries a first-person line (`impulse.*` in prompts.yaml), the dashboard shows what took over, the moment is notable (`notable.impulse`), and the sleep cycle's day log marks it "on an impulse". This is the relaxed rule from the cognitive plan.
- **Drives** (`drives`, not tied to anxiety): when a need is **low or empty**, its drive may take the decision before the brain is asked, at `chance.low` (0.25) or `chance.empty` (0.6). Only the most pressing need (empty before low, then the lowest) acts, and only needs in `drives.needs`. It takes the move on the menu that feeds that need most (from the verbs' `effects`/`by_tag`). Hungry with nothing to eat here, it **forages** (`drives.forage`): it goes towards the nearest place where it has eaten or has seen something edible, or, knowing none, to the neighbouring place it has not been to for longest. Needs that are okay or full never act, so it does not spend its life grazing. Drives are the **one place code picks a move because of a need**, added after real runs where it starved and stayed joyless. They are traced like an impulse ("an impulse (drive: sustenance)"), the diary carries `impulse.drive*` unless the journal writes its own entry, and `scripts/measure_run.py` counts meals a drive took. A need newly dropping into **low** also interrupts whatever it was doing (`activity.interrupt_on_low_need`).
The website shows it as a bar between mood and energy. Turn it all off with `anxiety.enabled: false`.

Mood is **one number**, a simple slider from **−50 (negative) to 50 (positive)**. It is never set directly. It is recomputed every tick as the sum of:

- the points `mood.bands` gives each listed need for its *band*,
- the event nudge × `event_points`,
- the brain's own `feeling` (`feeling_values`),

clamped to −50..50.

| Need | empty | low | okay | full |
|---|---|---|---|---|
| sustenance | −20 | −6 | 0 | 0 |
| joy | −15 | −6 | +4 | +15 |
| peace | −12 | −5 | +3 | +12 |
| wonder, curiosity | not listed: add nothing | | | |

The event nudge is the sum of the `mood` values of events that fired, decayed by `event_decay` (0.7) each tick. The feeling adds −10 / 0 / +10. Raw need levels and energy never enter the number.

**Words.** Above `word_threshold` (12) the word is *content*; below −12 it is *low*; in between it is *calm*. The description adds the most pressing need by band ("calm, though peace is low") or the energy word. Mood only reaches the entity as words.

**Worked example (a fresh start).** All needs okay: sustenance okay adds 0, joy okay +4, peace okay +3 → 7; feeling neutral → **7**. |7| < 12 → **calm**.

**Where mood matters.** It never changes needs or choices directly. It affects: the words in the next prompt; the recall query (the mood word is part of it); the website's mood slider and mood word, and the *colour* of the abstract presence (blue at −50 to amber at +50); and the dev dashboard. (How fast the presence breathes follows *energy*, not mood.)

---|---|---|---|---|
| sustenance | −20 | −6 | 0 | 0 |
| joy | −15 | −6 | +4 | +15 |
| peace | −12 | −5 | +3 | +12 |
| wonder, curiosity | not listed: add nothing | | | |

The event nudge is the sum of the `mood` values of events that fired, decayed by `event_decay` (0.7) each tick. The feeling adds −10 / 0 / +10.

**Arousal** = `arousal_base` (−10) + `arousal_energy_weight` (0.4) × energy + any matching `arousal_rules` (default: curiosity `full` **and** peace `low` or `empty` → +25, "curious and unsettled").

**Words.** If |valence| > `word_threshold` (12): positive → *content* (or *excited* if arousal ≥ `arousal_high` (5)), negative → *low* (or *tense*). Otherwise *calm* (or *restless*). The description adds the most pressing need by band ("calm, though peace is low") or the energy word. Mood only reaches the entity as words.

**Worked example (a fresh start).** All needs okay: valence = 0 (sustenance) + 4 (joy okay) + 3 (peace okay) = 7; feeling neutral → 7. Arousal = −10 + 0.4 × 30 = 2. |7| < 12 and 2 < 5 → **calm**.

**Where mood matters.** It never changes needs or choices directly. It affects: the words in the next prompt; the recall query (the mood word is part of it); the website's mood slider, mood word and the colour and motion of the abstract presence (colour from valence, breathing speed from arousal); and the dev dashboard.

---

## 7. Memory and recall

| Layer | Contents | How it is made | Lifetime |
|---|---|---|---|
| **Diary** | every tick: action, outcome, reaction, events, need changes | automatic | forever (it is what the website shows) |
| **Short-term** | "notable" ticks, today | during a tick | until the next sleep, which consolidates and clears them |
| **Long-term** | consolidated memories ("keepsakes") | the sleep cycle | kept up to `long_term_cap` (500); beyond that the least valuable (importance × recency) are dropped |

**A tick is notable** (and becomes a short-term memory) if any of these hold: a need moved at least `notable.need_change` (20) points in that tick; the brain's feeling is in `strong_feelings` (negative or positive); something new was created; a world change was rejected ("odd outcome"); an impulse took the decision over (`notable.impulse`); anxiety climbed into a higher band (`notable.anxiety_rise`: calm → uneasy → on edge → frantic, the bands of `anxiety.words`). Its importance is `importance_base` (0.3) + `importance_per_trigger` (0.2) × the number of triggers, capped at 1. The brain sees the most recent `short_term_shown` (8) short-term memories as "Earlier today".

**Recall** scores *every* long-term memory:

`score = w_recency × recency + w_importance × importance + w_relevance × relevance`

- *recency* halves every `recency_half_life_sim_hours` (48) sim hours since the memory was created or last recalled;
- *importance* is the stored 0..1 number;
- *relevance* is the cosine similarity between the memory and the situation query.

Defaults weigh relevance 1.5, recency 1.0, importance 1.0. The top `recall.k` (5) appear as "You remember". If the embedding model is unavailable, relevance is 0 and recall falls back to recency and importance. Recalled memories get their last-recalled time refreshed, so remembering something keeps it fresh.

---

## 8. Slow change: tendencies, preferences, self-image, projects

Everything here changes only during sleep, a little at a time.

**Core traits** (`character.yaml`) never change. They are always in the prompt.

**Tendencies** are not defined in the config: the creature learns what it tends to be, in its own words (for example "drawn to quiet corners"), during sleep. A new one starts weak and strengthens or fades with what it does (each 0..1, drifting ≤ 0.05 per night). They enter the prompt as their own phrase with a strength word ("a little …", plain, "very …", from `tendency_words` below 0.35 / above 0.65 and the `tendency_*` lines in `prompts.yaml`). You may still seed some by hand in `character.yaml` (`tendencies:`); the engine never drops a learned one when the config changes.

**Preferences** are learned likes and dislikes ("the old tree", "rain"). The brain sees up to `preferences_shown` (5) of each. Dislikes also have a **mechanical effect**: while a disliked thing is present, the world drains needs (`dislike_drain`). A dislike matches when its subject text appears (case-insensitive) in the current area's name, the name of an object here, the weather, or the entity's own action text. Up to `max_matches` (2) of the strongest dislikes count; each drains `points_per_hour × hours × |valence|` from every need in `dislike_drain.needs` (default peace and joy). So a strongly disliked "rain" costs about 4 peace and 4 joy per sim hour of rain; "the kitchen" disliked costs that whenever it is in the kitchen.

**Self-image** answers "what do I think I am?". It starts **empty**; an optional text hint can be set in `character.yaml` (`self_image.seed`). Each night the sleeping mind may propose **at most one** first-person line with a confidence. Code then:
- rejects empty text, non-numeric confidence, confidence below `self_image_min_confidence` (0.2), and duplicates;
- truncates to `max_self_image_length` (160);
- if the proposal names an existing line in `replaces`, **replaces** it (this is how a later line revises or contradicts an earlier one);
- keeps at most `max_self_image_lines` (5): over the cap the least confident (then oldest) is forgotten, which can be the new proposal itself.

The brain sees the lines (text only) and is told it must not assert a form, species or body that is not in them. If there are none, the prompt says the self-image is empty. The website's *What it thinks it is* panel shows the same lines.

### Projects: continuity across ticks and days

**Does the entity do something new every tick?** Yes. Every tick the brain chooses a fresh action from scratch; nothing "runs in the background". What gives it continuity is only what the prompt tells it: its last few actions (`recent_actions_shown`, 5), the last outcome, today's short-term memories, recalled long-term memories, and its likes. Without more, a task like "write a story" is re-decided every tick and tends to drift.

**Projects** are the long-term answer. A project is something it keeps coming back to, with a title, a reason ("why it likes it"), a stage, a running note ("where it got to") and the pieces it has made.

- **Forming.** During sleep (step 5 of the cycle) the sleeping mind looks at the day's log and its likes. If the entity returned again and again to one activity it seemed to enjoy (researching, drawing, writing, making music, building...), it may start **one** project from it. It may instead pause, resume or drop one existing project. At most one change a night. Code refuses a duplicate title, an empty title, or a start beyond `projects.max_active` (1). Every proposal and outcome is in the sleep trace.
- **Being told.** While a project is active the brain's prompt gains **"What you're in the middle of"**: the title, why, its stage in words (`projects.stages`: just begun, underway, well along, nearly done), where it got to, and, if it has been left for `idle_hours_note` (24) sim hours, "You last worked on it about 2 days ago." The brain still chooses freely; the section is a reminder, never an instruction. The brain's output format is unchanged.
- **Advancing.** Each active project appears on the menu as a **work on** move (the brain's `why` becomes the note of where it stands). Code sizes the step from `projects.work_bucket` and `projects.progress_values` (8 / 16 points of 100) **tapering as it nears the end** (× 1 − `taper` × progress/100, so a 16-point step is 12 at the halfway mark), stores the note and artifact, and stamps when it was last worked on.
- **Instead of the repetition penalty.** An action that moves a project forward is **exempt** from the repetition penalty, so continuing is not punished. The returns taper through the shrinking steps instead. A report that moves nothing (`0`) does not exempt anything. Working on a project also does not make the place "stale" (§4).
- **Milestones and finishing.** Crossing each `milestone_every` (25) points and finishing are notable moments (a short-term memory, consolidated at sleep). Reaching 100 marks the project **done** and code pays `projects.completion_gains` (joy +20, wonder +10); it is always code that pays.
- **Neglect.** Each night, an active project that was not worked that day counts an idle night; after `pause_after_idle_nights` (3) it is **paused** (a later night can resume it). A paused project is not shown to the brain or offered as a move.
- **What it leaves behind.** Artifacts are short saved pieces (up to `artifact_max_length`, 1200 characters). The website's **Projects** panel lists what it is working on (stage in words, never the number) and the latest `artifacts_shown` (6) pieces; a made piece also tags its diary entry. The dashboard's **Projects** page shows every number and artifact, and each tick page explains the project step and what code did with it.

Nothing here picks an action: code never chooses, suggests or ranks actions by project. It only remembers, reminds, and counts.

### Drawing: things it makes that stay in the world

In the **stationer's back room** (off the fountain) there is a stack of drawing paper and charcoal (tag `paper`), a tin of watercolours (tag `paints`), a work table and a cork pin board (tag `board`). Drawing is made of ordinary moves (moves.yaml), and code (`cat/drawings.py`) owns everything they do:

| Move | Offered when | What happens |
|---|---|---|
| **start a drawing of …** | `paper` is here, fewer than `drawing.max_in_progress` (3) unfinished drawings | A new object, *a drawing of …* (tag `drawing`, state `status: in progress`, `sittings: 1`), and a row in `drawings` with its first sitting |
| **add to …** | an unfinished drawing is in reach (always kept on the menu) | Another sitting on the same drawing |
| **call … finished** | it is unfinished | `status: finished`. Its journal is asked for a title in its own words, which is kept |
| **pin … on the board** | it is finished, a `board` is here, the board holds fewer than `drawing.board_capacity` (6) | `status: pinned`. It shows on the website's board |
| **throw away …** | it is unfinished or finished | The object is gone (`discard`: it can only throw away its own things), and the row is kept as *thrown away* |

- **Only what it has known.** A new drawing's subject is one of `drawing.subjects_offered` (4) picked from its own history: places it has been, things it has handled or looked at closely (still in the world, not its own drawings), and the last `happenings_considered` (30) things it witnessed ("the moment when the clock strikes"). It cannot draw what it has never come across.
- **A drawing is ongoing.** Each sitting is a move of 20–60 minutes, so it runs as an activity. If hunger, an event or sleep interrupts it, the sitting is recorded as shorter (`interrupted: true`), and the drawing waits on the table for the next sitting. Its drawings never expire.
- **It draws with a tool** (`cat/drawings.py` `sketch`, one LLM call per sitting, prompts under `sketch`). It places its own marks: up to `drawing.marks_max` (20) for a 60-minute sitting, at least `marks_min`. Each mark has a shape (line, curve, outline, circle, scribble, spiral, wave, rect, hatch, crosshatch, stipple, dots, blot, wash), a place and size on a 0–100 grid, an angle, a weight, a colour, a layer (back, middle or front) and what part of the subject it is. A line, curve or outline may instead follow up to 10 freehand `points`, for real contours. It also says in a few words what it was going for.
  - **What it has to draw with is the world's.** Charcoal (greys) is always there with the paper. The colours in `drawing.colours.paints` are on offer only while something tagged `paints` is here. A mark in a colour it doesn't have is dropped, and code clamps everything to the sheet.
  - **What it is told:** the subject and what code holds as true of it (a thing's `tag_facts`, a place's description, a happening's words); this drawing so far (each sitting's marks, shapes, colours and what it was going for); its last `history_shown` (4) drawings (subject, colours and shapes it used, whether it pinned or threw them away), so a style can grow from its own history; and how it is now (the same impression lines as the brain).
  - If the call fails (or `drawing.sketch` is false), code draws that sitting from the subject alone, as below.
- **Code draws the picture** (`cat/art.py`, served as `/drawings/<id>.svg`), from the drawing's history and nothing else. Its own marks are drawn exactly as placed, back layer first, and they look like the tool that made them: paper grain; charcoal as rough, tapered strands broken by the grain, smudged under heavy marks, with areas hatched rather than filled; watercolour as translucent glazes that bleed and pool darker at the rim, with tapered brush strokes; back-layer marks paler and softer. They are drawn in a hand its state moves: anxiety shakes the line, energy makes it heavier, and an interrupted sitting shows only the share of its marks it had time for. A sitting without its own marks is drawn like this:
  - *What* it drew shapes the marks: a thing's tags give its motifs (round things are rings, climbable or standing things are uprights, wet things ripple, warm things radiate, fragile things are broken lines, soft things are blots), a place is a horizon with forms on it, and a happening is a burst.
  - *How it was* at each sitting sets the style: mood sets the colour (the presence hues), anxiety makes the line shake (`web_ui.drawing.jitter_max`), energy makes it heavier, and the minutes spent set how many marks (`drawing.marks_per_sitting`). A drawing started calm and finished anxious shows both.
- **Drawing projects.** If the day's log shows it drawing, the sleeping mind may start a project with `medium: drawing`. Code accepts that only if it really drew that day. A drawing project has no separate work-on move: each sitting is its work (progress, milestones, exempt from the repetition penalty), and each finished drawing becomes one of its pieces (an artifact with a `drawing_id`, shown as the picture). No LLM writes a drawing project's pieces.
- **What it sees.** Its drawings are objects in the prompt like anything else ("a drawing of the fountain [drawing] (status: in progress, sittings: 2)"). Whether it comes back to them, finishes them, pins them or throws them away is its choice.
- **An older database** gets the back room's objects at the next start: objects of an area that the world had never known are added (`_reconcile_config`).

---

## 9. The world: objects, events, and what changes the world

**Areas** (`world.yaml`): a name, a description and exits; exits must be mirrored. (Happenings come from two sources: the hand-written `events` in `world.yaml` and the world director, §3. Objects it spawns are marked `created_by = director`.) **Objects**: an id, name, area (or `inventory`), **tags** and a small state. Tags decide what is possible: `edible` (can be consumed), `movable` (can be taken and moved). Other tags (`warm`, `climbable`, `soft`, `toy`, …) steer which verbs apply (`any_tags`) and the extra gains (`by_tag`) in moves.yaml. Objects are **seeded once**, on the very first start.

Only code, through the chosen move, changes the saved world, and only through these operations (no LLM can). Changes are checked **in order**; a rejected one is logged and skipped, the rest still apply, and at most `max_changes_per_tick` (6) are accepted.

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
- *Situation, every tick:* **Who you are right now** (self-image or "empty", tendencies, likes and dislikes) → **Now** (time, weather, place, mood and energy words) → **What's pulling at you right now** (§4) → **What you're in the middle of** (active projects, if any; §8) → **Around you** (objects here, carried items, exits) → **What just happened** (events, last outcome, recent actions, or what interrupted an activity) → **Memories** → **the menu of moves** → "What do you do next?".
- *Who you are* also carries the habits line ("Things you have found yourself doing again and again: …"), a description of the past only.
- It never contains need numbers. The menu ids (m1, m2…) are the only options, in a seeded shuffled order that is never ranked or marked by need (only "(never tried)", from its history).

**The journal's prompt:** the brain's identity lines (name, backstory, identity, core traits) and the journal rules as its system prompt; the facts of the moment (Step 5b) as its user message. It returns only `entry`; it cannot change a number or the world. Project pieces come from a separate call in the brain's own voice.

**The world director's prompt:** the place, time, weather, objects, recent happenings and its last few actions, with the rules above in its system prompt. No needs, no mood.

**The sleeping mind's prompts:** the day's summary material for each of the six steps (§5): consolidate, tendencies, preferences, self-image, projects, and **habits** (code lists the recurring patterns it counted; the model only words them).

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
| **Project advanced** | the brain picks the project's **work on** move | progress steps up (tapering); note and artifact saved; this action skips the repetition penalty; no stale tick | the brain is reminded where it got to; pieces accumulate on the site |
| **Project milestone / finished** | progress crosses a multiple of 25 / reaches 100 | a short-term memory; finishing pays joy +20, wonder +10 | consolidated at sleep; finished projects stay listed |
| **Project untouched for 3 nights** | each sleep | the project is paused | the brain stops being reminded; a later night may resume it |
| **Director due** | every 6 action ticks, or 2 when the place is stale | one LLM call proposes at most one happening; code validates and applies it as an event | novelty: new sounds, light, visitors and objects; its mood nudges and capped drains colour the next ticks |
| **Notable tick** | need jump ≥ 20, strong feeling, creation, rejected change | a short-term memory | consolidated into long-term at sleep |
| **Energy low (< −25) at night** | each night tick | falls asleep without a brain call | the nightly cycle runs |
| **Brain picks the sleep move** | offered only when energy < 10 or night | falls asleep | the nightly cycle runs |
| **Long move chosen** | its length > `activity.step_minutes` | carry ticks without LLM calls until it ends or is interrupted | one diary entry; fewer calls |
| **Event or a need hits empty during an activity** | any event; a need newly empty | the activity ends; the next prompt says why | the brain chooses again |
| **Same move, same place, within the window** | `repetition_penalty.window` | wonder and curiosity gains ×0.4 | variety, never a ban |
| **Sleep cycle: habits** | each sleep | code mines recurring (move, part of day) pairs; the model words them | "you tend to…" in the prompt, site and dashboard |
| **Asleep** | each asleep tick | +12 energy/h, needs decay at half speed | wakes at energy ≥ 40 after 06:00 (or 12 h) |
| **Director call fails** | the model is unreachable or returns junk | no happening this time; the tick carries on normally | none; the hand-written events still roll |
| **Sleep cycle succeeds** | at sleep onset, brain online | memories consolidated; tendencies drift ≤ 0.05; preferences updated; ≤ 1 self-image line; ≤ 1 project change | personality, tastes and self-image evolve slowly; diary "Last night…" card |
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

Tuning principle: change **one thing at a time**, let it run for a few sim days, and read the **Needs & mood** charts and the tick pages before changing more. Most behavior comes from the *balance* between decay (always falling) and the move gains in moves.yaml. Run `scripts/simulate_balance.py` (no LLM) to see how long each need sits in each band under scripted play before and after a change.

### Pace

| Parameter | Effect | Guidance |
|---|---|---|
| `tick_interval_seconds` | real seconds per tick | lower = faster life and more LLM load. Keep it above the time a tick takes (see the tick time on the dashboard's Overview), or ticks queue up |
| `sim_minutes_per_tick_default` | fallback sim minutes per tick; also the catch-up pace | |
| `sim_minutes_min` / `max` | clamp on a move's duration | raising `max` lets single actions pass more world time (and more decay) |
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
| `repetition_penalty.window` | how many past decisions are looked at for the same move in the same place | longer = repeats stay penalised for longer | shorter = forgiven sooner |
| `repetition_penalty.needs` | which needs get penalised | add needs to make them depend on novelty | |
| `stale_after_ticks` | how soon "this place is used up" appears | | `0` disables |
| `dislike_drain.points_per_hour` / `max_matches` / `needs` | how costly dislikes are | stronger aversion | `0` makes dislikes cosmetic |
| event `drains` (`world.yaml`) | one-off hits | e.g. `{peace: 15}` for a loud event | |

Reading the numbers: a need that decays 5/hour loses 25 points in 5 hours, so it needs about one `++` per 3 hours of full-length moves just to hold steady.

### Energy and sleep

| Parameter | Effect |
|---|---|
| `energy_start` | starting energy (−50..50) |
| `effort_energy_cost` | what low/medium/high effort costs. Raising it makes the entity tire sooner and sleep more |
| `sleep.fall_asleep_energy_below` | night exhaustion threshold. Raise it (e.g. −10) to make sleep come earlier; lower (−40) to let it stay up |
| `sleep.wants_sleep_energy_below` | how tired before the sleep move is offered by day. Set to −50 to offer it only at night |
| `sleep.restore_per_sim_hour` | how fast sleep restores energy (and so how long the night is, together with `wake_*`) |
| `sleep.wake_energy_at_least` / `wake_not_before_hour` / `max_hours` | when it wakes |
| `sleep.decay_multiplier` | how much needs fall while asleep (1 = same as awake; 0 = frozen) |
| `doze.energy_restore`, `doze.sim_minutes` | what an outage tick is worth |

### Mood

| Parameter | Effect |
|---|---|
| `mood.bands` | the whole mood map: which needs and bands move the mood, by how many points. Add `wonder: {empty: -8}` to let wonder matter, or remove joy to decouple it |
| `event_points`, `event_decay` | how strongly and how long events colour mood |
| `feeling_values` | how much the brain's own feeling counts |
| `word_threshold` | where the mood word flips between low, calm and content |

### Memory, personality

| Parameter | Effect |
|---|---|
| `notable.need_change` etc. | what counts as a memorable moment (lower = more short-term memories) |
| `recall.k`, `w_recency`, `w_importance`, `w_relevance`, `recency_half_life_sim_hours` | what is remembered when; raise `w_relevance` to remember on association, raise the half-life to remember longer |
| `long_term_cap` | how many keepsakes are kept |
| `sleep.tendency_max_step` | how fast personality can drift (0 freezes it) |
| `sleep.tendency_start`, `max_tendencies`, `max_new_tendencies_per_night`, `tendency_forget_below`, `tendency_name_max_length` | how readily it notices new things about itself, how many it keeps, when a faded one is forgotten |
| `sleep.preference_blend`, `max_preferences_per_night`, `preference_min_abs`, `max_preferences_total` | how fast tastes form and fade |
| `sleep.max_self_image_lines`, `self_image_min_confidence`, `max_self_image_length` | how much self-image it can hold and how sure it must be |
| `recent_actions_shown`, `preferences_shown`, `short_term_shown` | how much context the brain gets (more = longer prompts; mind `llm_num_ctx`) |

### Moves, activities and habits

| Parameter | Effect |
|---|---|
| config/moves.yaml verbs: `effects`, `by_tag`, `novelty` | what each move feeds. This is where "how restoring is resting" is decided; check it with `scripts/simulate_balance.py` |
| verb `minutes`, `effort` | how long a move takes and what it costs in energy |
| `moves.max_offered` | menu length cap (earlier verbs, nearer targets win) |
| `moves.novelty_hours` | how long before a place counts as new again (feeds the `go` novelty gains) |
| `journal.enabled` | `false` = no journal entry (the diary shows the reason it gave), one fewer LLM call per decision |
| world.yaml `tag_facts`, moves.yaml `reveals` | what looking closely at a thing reports, and which verbs report it |
| `activity.enabled`, `step_minutes` | carry long moves in slices with no LLM calls. `false` = one decision per tick |
| `activity.carry_interval_seconds` | real seconds between carry ticks |
| `activity.interrupt_on_events`, `interrupt_on_empty_need` | what breaks an activity |
| `habits.enabled`, `window_ticks`, `min_count`, `min_share` | how easily a recurring move becomes a habit (lower = more habits) |
| `habits.max_stored`, `max_shown` | how many are kept and how many reach the prompt |

Wording is in prompts.yaml: the `fallback` entry of its `habits` section, the two `habits` entries of the `sleep` section (one under its `jobs`), and the `habits` line of the brain's situation.

### The world director

| Parameter (`director.*`) | Effect |
|---|---|
| `enabled` | `false` = only the hand-written events |
| `every_ticks` | how often it runs in a normal stretch. Lower = a livelier world and more LLM calls; higher = a quieter, cheaper one |
| `stale_every_ticks` | how soon it runs when the entity is stuck in one place doing the same things. Must not exceed `every_ticks` |
| `max_mood` | the strongest mood nudge a happening may carry (events say −1..1). `0` makes happenings mood-neutral |
| `max_drain_points`, `max_drain_needs` | how hard a happening may hit needs. `max_drain_points: 0` removes drains entirely |
| `avoid_repeat` | how many recent happenings it sees and is rejected for repeating. Raise if it keeps saying similar things |
| `recent_actions_shown` | how much of what the entity did it can react to |
| `text_max_length` | how long a happening may be |

The wording is in the `director` section of `prompts.yaml`. To make the world gentler or harsher, edit "Most of the time nothing should [happen]" and the tone examples; to forbid a kind of happening, say so there (code will not catch it). Also `llm_temperature.director` (0.8) controls how adventurous it is. Keep it from reading needs or mood: that is what stops the world from steering the entity.

### Projects

| Parameter (`projects.*`) | Effect |
|---|---|
| `max_active` | how many projects it can work on at once. Raising it lets several run; each appears as its own work-on move |
| `progress_values` | points per `+` / `++` (of 100). Smaller = projects take many more ticks; larger = they finish fast. The buckets must exist in `gain_values` |
| `taper` | 0 = every step is the same size; 0.5 (default) = a step is half-size near the end; 1 = steps shrink to nothing at 100 (a project may then never quite finish, so keep it below 1) |
| `milestone_every` | spacing of notable moments (a memory each time) |
| `completion_gains` | need points paid by code on finishing; set `{}` to pay nothing |
| `pause_after_idle_nights` | how long a neglected project stays active (0 = never pauses) |
| `idle_hours_note` | how long before the brain is told it has been left alone |
| `stages` | the stage words and where they flip |
| `title/why/note/artifact_max_length`, `artifacts_shown` | text limits and how many pieces the site shows |

The wording that forms and advances projects is in `prompts.yaml`: the `projects` entry under `sleep` (when to start one: raise or lower the bar by editing "returning again and again"), `brain.create*` (the piece it makes), and `brain.situation.project*` (how the brain is reminded). Project work being exempt from repetition is intentional. To keep some pressure on a project that drags on, raise `taper` (for example 0.8) so its steps shrink faster toward the end.

### The LLMs
`brain_model`, `narrator_model`, `sleep_model`, `embed_model`, `llm_temperature` (brain 0.9 = adventurous, journal 0.7; `narrator_model` is the director's model; the default models are `qwen3:14b`, with `llm_timeout_seconds` 600), `llm_num_ctx` (4096; raise if prompts get truncated), `llm_retries`, `llm_timeout_seconds`. Changing `embed_model` requires re-embedding stored memories: `scripts/reembed.py`.

### The words (prompts)
All of the following are in `config/prompts.yaml`; each entry's comment lists its `{placeholders}`:
- **Brain system:** `intro`, `identity`, `core_traits`, `voice`, `output` (the JSON contract).
- **Brain situation:** headings and sentences, including `heading_pulling` and `none_pulling`.
- **Journal:** `system` (its rules; keep the doubled braces `{{ }}`), `heading_happened`, `did`, `why`, `impulse`, `result`, `events`, `need_eased`, `heading_now`, `heading_memories`, `closing`.
- **Sleep:** `system`, the five `jobs`, and the five step prompts.

The validator refuses an empty or missing entry or an unknown `{placeholder}` and reports every problem. Keep the JSON-format instructions intact (the models are also given a JSON schema, but the text helps). Never put need numbers or a list of options in a prompt: that breaks the design rule that needs shape what is *said*, not what is *chosen*.

For the character's voice and wording of needs, edit `character.yaml`: `meaning` (what a need is; shown on the website and in prompts), `expressions` (the four band texts the brain reads), `stale_here`, `voice`, `backstory`, `core_traits`.

### Recipes

| Goal | Change |
|---|---|
| A **hungrier**, more driven entity | raise `sustenance` `decay_per_hour` to 8; raise `need_bands.low_below` to 50 |
| An entity that **rarely feels anything pressing** | lower `need_bands.low_below` to 30 and `empty_below` to 10; set `max_pressing_needs: 1` |
| **Calmer, slower life** | `tick_interval_seconds: 180`; lower `joy` and `curiosity` decay |
| **More varied behavior** | `repetition_penalty.multiplier: 0.2`, `stale_after_ticks: 2`, raise `llm_temperature.brain` to 1.0 |
| **Sleeps more** | `effort_energy_cost: {low: 3, medium: 9, high: 18}`; `sleep.fall_asleep_energy_below: -10` |
| **Never sleeps by day** | `sleep.wants_sleep_energy_below: -50` |
| **Moodier** | raise `mood.bands` magnitudes (e.g. joy full 25, empty −25) and `event_points` to 30 |
| **Remembers more** | raise `recall.k` to 8 and `recency_half_life_sim_hours` to 120 |
| **Faster personality drift** | `sleep.tendency_max_step: 0.1` |
| **No dislike penalty** | `dislike_drain.points_per_hour: 0` |
| Make a **new need** | add it to `character.yaml` (start, decay_per_hour, meaning, all required expressions); it joins at its start value after a restart. Optionally list it in `mood.bands`, `repetition_penalty.needs`, `dislike_drain.needs` |
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
| Forget everything it has learned about how it tends to be | `UPDATE state SET tendencies = json_object();` | no tendency lines in the prompt until the next sleep notices one |
| Send it home | `UPDATE state SET area = 'living_room';` | |
| Forget its habits | `DELETE FROM habits;` | no "you tend to…" line until the next sleep finds one |
| Forget all projects and what they made | `DELETE FROM projects;` and `DELETE FROM artifacts;` | no projects or pieces; the sleeping mind may form new ones |
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
| Why is the mood *calm*? | tick detail → **Mood math** (every band term, event nudge, feeling) |
| Is it running well? | **Overview** (tick time, doze count, errors, service health); **LLM calls** (latency, invalid JSON) |
| How are needs and mood trending? | **Needs & mood** (charts over 24 h / 7 d / all, sleep and doze shaded, critical time per need) |
| What is it working on, and what has it made? | **Projects** (progress numbers, idle nights, every artifact); a tick page's **Project work** section |
| How many decisions vs carried-on ticks, tokens per call and per day? | **Overview** (model decisions vs carried on, tokens) |
| Which habits does it have and what would the next sleep see? | **Projects** page, Habits section |
| Are the needs balanced? | run `scripts/simulate_balance.py` (no LLM; random / first / cycle / needy policies, time in each band, anxiety). `tests/test_simulate_balance.py` fails if the `needy` yardstick leaves some need empty 15% of the time or more, or is above the impulse threshold 15% of the time or more |
| How did a real run actually go? | `scripts/measure_run.py --db data/cat.db` (read-only): brain errors and off-menu replies, latency, prompt size against `llm_num_ctx`, where on the menu its picks sit (list-order bias), how varied its choices are, impulses, time in each need band, anxiety, and what it has built up |
| Why did something just happen in the room? | tick detail → **World director** (why it ran, the proposal, what code kept or ignored) |
| What did last night do? | **Sleep** (each step's input, proposal and what code applied; tendency history) |
| What does it remember? | **Memory** (short/long-term, recall stats, embedding map, search) |
| Are my edits live? | **Config** (loaded vs on disk; *Reload settings*) |
| Try a different prompt or model | **Playground** (replay any stored call, edit and compare) |

Dev controls (pause, resume, step one tick, set interval, force sleep cycle, reload settings, inject an event, snapshot DB) are queued in `data/devdash.db` and run by the engine within about two seconds. **Step** is the best tool for tuning: pause, step once, read the tick page, adjust, reload, repeat.

From a terminal, `.venv/bin/python scripts/show_diary.py -n 50` prints the diary, and `.venv/bin/python -m cat.main --mock --ticks 100` runs a whole day-cycle with no Ollama (a mock brain) to check a config change without spending LLM time.

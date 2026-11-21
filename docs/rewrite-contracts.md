# Rewrite: System Contracts

*Status: built (steps 1-8). This document defines the contracts between the systems of the engine, and each section
ends with how it was built. It is the design reference for the code: when they disagree, fix one of them.
Decided: the rewrite replaced `cat/` in place; waking decisions use a small (~4B) model and sleep uses the 14B
model; Nit is a humanoid creature (§2.1); the sim runs at 24× (one real hour = one sim day, §3.4); the first
content pack is the fountain and the market stall (§4.1–4.2); visitors can leave open-ended letters (§4.8); no web
search at start; a new life started (§9); the old engine's rules in §11 are overturned. The real-model run
(step 7) is measured with `scripts/measure_nit.py` on the laptop.*

---

## 0. Why the current system is being replaced

What goes wrong today, as observed in the first real runs and the November analysis
(`docs/analysis-2026-11.md`):

| Problem | Cause in the current design | Answer in this rewrite |
|---|---|---|
| Choices follow list position and verb salience, not inner state | A ~30-item menu built from verb × object × tag (`moves.yaml`), shown to a 14B model in a 4096-token context | Small menus: the actions the creature **knows** at **this location**, plus three universal actions |
| It never ate while hungry, so code-driven reflexes, drives and impulses were added | Five abstract needs (wonder, joy, peace, curiosity, sustenance) and an anxiety gauge compete. Code started overriding the LLM to compensate | Three variables (mood, energy, hunger) that shape the prompt and the consequences. Code never picks the move |
| Nothing to learn | Every object and tag is shown to the brain. Every verb always does the same thing | Hidden actions found by `inspect`. Outcomes can vary. The menu is built from what the creature knows, not from what exists |
| Identity is LLM text read back by the LLM | Tendencies, preferences and self-image are written from LLM summaries with no evidence | Character changes need code-checked evidence (§5.6) |
| Ticks are sim-time chunks with hidden carry ticks | 20-minute ticks, 90 s real interval, activity carry ticks, a narrator, a director | Event-driven cycle with a ~15 s target. Activities persist across decisions |
| Adding content means touching `world.yaml`, `moves.yaml`, `tag_facts` and sometimes code | Behaviour is spread over verbs, tags, `does:` handlers and `by_tag` tables | One file per location holds the location and its actions. Code only adds new **effect operations** or **capabilities** |

### Kept from the old code (reused, not preserved)
- `cat/llm.py`: `LLMBackend`, `OllamaBackend`, `MockBackend`, `call_json` with schema validation and retries, `hash_embed`, `cosine`.
- `cat/db.py`: `connect`, `transaction`, numbered migrations, `backup`, meta/state helpers.
- `cat/trace.py`: the tracer and its stage timing, rebuilt around the new cycle.
- `cat/memory.py`: embedding blobs, the recency/importance/relevance recall scoring.
- `cat/clock.py`, `cat/logs.py`, `cat/healthcheck.py`, `cat/pullmodels.py`, `cat/downtime.py`, `deploy/`, `Dockerfile`, `compose.yaml`.
- `cat/drawings.py` + `cat/art.py`: become the `draw` capability (§4.6).
- `cat/web/` and `cat/devdash/` shells (Flask app, static assets, auth). Views and templates are rebuilt in step 8.

### Removed
`moves.py`, `impulses.py` (drives, reflexes, anxiety), `director.py`, `harms.py`, `habits.py` (replaced by
behaviour history), `projects.py` (replaced by activities and character development), `journal.py` (thoughts
come from the decision call), `prompts.yaml` as one large file (prompts live next to the stage that uses them),
needs and need bands, `tag_facts`, `moves.yaml`.

---

## 1. Content packs: growing the world without code

Everything about *this* world and *this* creature is data. The code knows only general mechanics.

```
content/
  character.yaml            # the starting character sheet (§2.1)
  world.yaml                # clock, weather, start location, global events
  locations/
    fountain.yaml           # one location and every action that belongs to it
    market.yaml
    ...
  actions/
    universal.yaml          # sleep, move, inspect (the only actions every location has)
  capabilities.yaml         # which code capabilities are enabled, with permissions (§4.6)
```

**What needs no code** (content only):
- A new location, its exits, description, world facts, events and actions.
- A new action built from existing effect operations and conditions.
- New hidden actions, discovery chains, varied outcomes, resources that regrow.
- Changes to character sheet seeds, prompt wording, tuning numbers.

**What needs code** (a new feature):
- A new **effect operation** (§4.4), such as a new kind of world change.
- A new **capability** (§4.6), such as painting, making music, web search or writing to a file.
- A new **condition type** (§4.3).

The loader validates every file at start-up against the schemas in this document. An unknown field, an
unknown effect operation, a broken exit, or an action pointing to a missing capability stops the engine
with a message naming the file and line. Content changes go live on restart, and the dev dashboard can
reload them without a restart. World facts the creature has changed are never overwritten by a reload (§4.2).

---

## 2. Identity and internal state

### 2.1 Character sheet (`content/character.yaml`)

```yaml
name: Nit
pronoun: it

# --- immutable: never changed by character development ---
core:
  nature: "A humanoid creature, black all over. How it looks matters little to it."
  senses: [sight, smell, touch]        # what it can perceive from the world (events are filtered, §4.1)
  abilities: [speak, read, write, feel]
  boundaries:                          # fundamental limits, checked in code and in prompts
    - "Speaks truthfully about its world: never claims something happened, or is there, that is not."
    - "May explore in its mind, with images and comparisons, to describe its feelings; feelings are its own, facts are the world's."
    - "Never harms the places or things others care about."
  prohibited_actions: []               # action ids it may never take, enforced by the world

# --- mutable: the starting point; sleep may change these slowly (§5.6) ---
traits:                     # personality, each with a strength 0..1
  curious: 0.8
  truthful: 0.7
  inward: 0.5               # turns inward to find words for what it feels
tendencies: {}              # learned from experience; none at spawn
background: "Nit woke in the square with no memory of before."
likes: {}                   # seeds for learned preferences (§2.3); empty is fine
dislikes: {}

innate_knowledge: [sleep, move, inspect]   # actions known from birth
start_location: fountain
```

Rules:
- The sheet holds **no dialogue, scripts, routines or goals**.
- `core` is never edited by the creature. Code checks every change proposal against it (§5.6).
- At first start the sheet is copied into the database as **character version 1**. From then on, the
  database is the source of truth and the file is only the seed. Each change creates a new version with
  its evidence, so the dashboard can show how Nit became who they are.

### 2.2 The three variables

| Variable | Range | Rises with | Falls with | Neutral |
|---|---|---|---|---|
| `mood` | -1.0 .. 1.0 | Pleasant outcomes, liked things, fresh experiences, rest | Unpleasant outcomes, disliked things, repetition, hunger | Drifts toward the trait baseline (`mood_baseline`, default 0.1) at `mood_drift_per_hour` |
| `energy` | 0.0 .. 1.0 | Sleep, resting actions, food (a little) | Effort per sim minute of activity, being awake | — |
| `hunger` | 0.0 .. 1.0 | Time (`hunger_per_hour`), effort | Eating actions | — |

Tuning lives in `config/settings.yaml` under `nit: body:` (`cat/tuning.py` validates it): start values,
`mood_baseline`, `mood_drift_per_hour`, `hunger_per_hour`, `hunger_sleep_factor` (slower asleep),
`hunger_per_effort` (effort makes hungry), `hunger_mood_threshold`, `hunger_mood_drag`,
`awake_energy_per_hour`, `sleep_energy_per_hour`, `hungry_sleep_penalty`, and `step_minutes` (time passes in
steps so hunger and its mood drag interact correctly). A test guards that a plain day (half of it in
low-effort activities, two meals, eight hours of sleep) stays out of the worst hunger band and that energy
runs low by bedtime and recovers by morning.

Each variable is a plain number in the state row. Every change is written to the trace with its cause
(the action, decay, or a preference), so the dashboard can explain any value.

Hunger reaches the rest of the system only through:
1. **Perception**: a body sentence chosen by band (`content/world.yaml` `body_words`), such as
   *"Your stomach is a small insistent knot."*
2. **Mood pressure**: above `hunger_mood_threshold`, mood loses `hunger_mood_drag × (hunger - threshold)` per hour.
3. **Energy recovery**: hungry sleep restores less energy.

Hunger never picks a move. If real runs show the small model still ignores food, the first fix is in the
wording and the perception, not an override. Any override would be a new decision, written down here first.

### 2.3 Derived conditions

Boredom, restlessness, mania, creativity and similar states are **derived**, never stored. They are
computed from mood, energy and the recent-repetition score (§6), and given as words in the prompt.

```yaml
# content/world.yaml
derived_conditions:        # first match wins; ranges are inclusive
  - {name: restless,  mood: [-0.3, 0.2], energy: [0.6, 1.0], repetition: [0.5, 1.0],
     words: "You feel itchy, as if you have done all this already."}
  - {name: buoyant,   mood: [0.5, 1.0],  energy: [0.6, 1.0],
     words: "Everything seems possible; ideas come quickly."}
  - {name: drained,   mood: [-1.0, 0.0], energy: [0.0, 0.3],
     words: "Everything feels heavy."}
  - {name: content,   mood: [0.2, 1.0],  energy: [0.2, 0.7],
     words: "You feel settled."}
```

Adding a derived condition is content, not code.

### 2.4 Likes and dislikes (learned)

```
preference(id, subject_kind, subject_id, valence -1..1, confidence 0..1, evidence[], updated_at)
  subject_kind: action | action_family | location | topic
```

- They are formed **only in sleep** (§5.5) from outcomes and mood changes recorded in code.
- They influence:
  1. **Consequences**: when an action touches a liked or disliked subject, its mood effect is multiplied
     by `1 + preference_weight × valence × confidence`.
  2. **Perception**: the menu shows them in words, e.g. *"make a wish: you have enjoyed this before"*.
  3. **Memory**: experiences involving strong preferences get extra importance.
- They never remove an action from the menu.

---

## 3. The conscious cycle

### 3.1 Decisions are not activities

- An **activity** is what the creature is doing: an action instance with a start, a planned duration,
  progress, and a state (`running | finished | interrupted | abandoned`).
- A **decision** is one LLM call that chooses what to do next. Decisions happen at decision points, and
  most of them are *"keep doing this"*.

### 3.2 Decision points

| Trigger | When | Options offered |
|---|---|---|
| `activity_finished` | The current activity reached its end | Full menu |
| `event` | A world event was perceived at this location (salience ≥ `event_salience_min`) | Continue, or full menu |
| `mail` | New visitor mail arrived (§4.8). Perceived only as "the mailbox at the stall has something new" when Nit is at the market or next arrives there | Continue, or full menu. Reading is always Nit's choice |
| `interval` | `decision_interval_seconds` (target 15 s) passed with **no** activity running | Full menu |
| `check_in` | An activity has run for `check_in_minutes` sim minutes since the last decision | Continue or stop. No full menu, short prompt |
| `body` | Hunger or energy crossed a band boundary | Continue, or full menu |

Rules:
- One decision at a time. A trigger that fires while a call is in flight is queued and merged.
- The interval is a **target**: if the previous call is still running, the next one waits. Nothing is
  interrupted half-way.
- While an activity runs and nothing fires, **no LLM calls are made**. The activity simply progresses.

### 3.3 The cycle

```
trigger ──► build Mind ──► small LLM decides ──► World.execute ──► apply consequences ──► record
                ▲                                                                          │
                └──────────────────────────────────────────────────────────────────────────┘
```

**Mind snapshot** (built by code, passed to the prompt builder, stored in the trace):

```python
Mind = {
  "now":          {"sim_time", "part_of_day", "weather"},
  "location":     {"id", "name", "description", "visible_facts": [...]},      # facts marked visible
  "perception":   [event texts since the last decision],
  "body":         {"mood", "energy", "hunger", "words": [...], "derived": "restless"},
  "activity":     {"action", "elapsed_min", "planned_min", "progress"} | None,
  "recent":       [last N completed activities with outcomes, from code],
  "memories":     [recalled long-term memories, k ≤ 5] + [today's notable moments, ≤ 5],
  "letters":      [read but unanswered visitor letters, newest first, ≤ mail_on_mind] (§4.8),
  "identity":     {"name", "nature", "traits_words", "tendencies", "boundaries"},
  "options":      [Option],         # what it can do right now, see below
}
Option = {"id", "label", "description", "inputs": schema | None,
          "notes": ["you have enjoyed this before", "you did this a moment ago"]}
```

`options` = known actions at this location whose conditions hold + `move` to each exit + `inspect` + `sleep`
(when allowed), minus `core.prohibited_actions`. The list is shuffled with a seed and shown without numbers.
Usually there are 4–10 options.

**Decision output** (JSON, schema-validated):

```json
{
  "action": "make_wish",
  "inputs": {"wish": "to remember where I came from"},
  "thought": "The water keeps moving with no wind. If anything here listens, it's this.",
  "reasoning": "I've been circling the square all morning and I want to ask for something.",
  "preserve": false,
  "letter": null
}
```

- `thought`: what it thinks, in its own voice. Shown in the diary.
- `reasoning`: why, in its own voice. Stored and shown on the dashboard.
- `preserve`: the creature marks this thought as worth keeping. It becomes a core-memory candidate (§5.2).
- `letter`: the id of a letter on its mind that this decision responds to, or null (§4.8).
- At `check_in`, `action` must be `continue` or `stop`.
- An invalid output is retried up to `llm_retries` times with the validation error. If it still fails, the
  creature continues the current activity, or idles if there is none (`idle` is an internal no-op that
  is never offered). The failure is traced.

Implemented in step 3: `cat/scheduler.py` (decision points), `cat/mind.py` (the Mind), `cat/decision.py`
(schema, validation, retries) with its fixed text in `config/prompts/decision.yaml`. Choice inputs resolve to
concrete values: `{exits}`, or `{fact:<loc>.<fact>[.<status>].labels}` for list facts (unread first).

### 3.4 Time scale and timing budget (GTX 1060 6 GB)

`time_scale: 24` in `config/settings.yaml`: **one real hour is one sim day.** All durations in content
(`minutes`, `per_minute`, `hunger_per_hour`, `chance_per_hour`, regrowth hours) are in **sim** time; the
clock converts. What that means in practice:

| Sim | Real |
|---|---|
| 1 sim minute | 2.5 s |
| A 15 s decision interval | 6 sim minutes |
| A 4B call of ~5 s | ~2 sim minutes (the clock keeps running: Nit is thinking) |
| A 20-minute activity | 50 s |
| An 8-hour night | 20 min |
| One sim day / week | 1 h / 7 h |

- **Sleep may take longer than the night.** Nit wakes only when the minimum sleep (`sleep_min_hours`,
  default 6 sim hours = 15 real min) has passed **and** all sleep stages are done. If processing overruns,
  the sim clock is held at the waking moment until it finishes, and the overrun is traced. Stage prompts
  are sized so a normal night fits.
- Day-based limits (capability `per_day`, `pref_window_days`, `dev_min_days`, forgetting half-lives) are in
  sim days. Character development's "3 different days" is therefore about 3 real hours.
- `downtime_policy: pause` stays: when the engine is down, sim time stops.

| Call | Model | Target | Frequency |
|---|---|---|---|
| Decision | ~4B (`decision_model`) | < 8 s | ≤ 1 per 15 s while idle; much less during activities |
| Check-in | ~4B | < 4 s | Once per `check_in_minutes` of activity |
| Embedding | `nomic-embed-text` | < 0.5 s | Per notable experience and per recall |
| Capability (draw, write) | ~4B | < 20 s | Per activity step that needs one |
| Sleep stages | ~14B (`sleep_model`) | minutes each | Once per night |

Only one model is loaded at a time while awake. Sleep swaps to the 14B model and swaps back on waking.

---

## 4. World and interactions

### 4.1 Location file

```yaml
# content/locations/fountain.yaml
id: fountain
name: the fountain
description: >
  The heart of the square. An old stone fountain stands in a wide basin, its water always
  faintly moving even when there is no wind.
exits: [market, clock_tower, garden]
sheltered: false

facts:                      # world state owned by this location (§4.2)
  wishes:   {type: list, default: [], visible: summary}   # visible: none | summary | full
  coins:    {type: int,  default: 14, visible: none}

events:                     # things that can happen here
  - {id: glint, text: "Something glints deep in the water.", sense: sight,
     chance_per_hour: 0.4, salience: 0.6}
  # `sense` (sight | smell | touch | sound | ...): an event is perceived only if it is in core.senses.
  # An unperceived event still changes the world; Nit just does not know.

actions:
  - id: sit_and_reflect
    name: sit and reflect
    description: "Sit on the rim and let your thoughts settle."
    family: rest
    discovery: known_on_arrival       # known_on_arrival | hidden | innate
    duration: {minutes: [10, 30]}
    continuous: true                  # may span several decisions; check-ins apply
    per_minute: {energy: +0.002}
    on_finish:
      creature: {mood: +0.05}
      reflection: true                # the prompt invites a deliberate reflection; may set preserve

  - id: make_wish
    name: make a wish
    description: "Say a wish to the moving water."
    family: wish
    discovery: hidden
    inspect_hint: "Coins glint in the basin, as if people have wished here before."
    requires:
      - {energy_gte: 0.1}
    inputs:
      wish: {type: text, max_chars: 200, prompt: "What do you wish for?"}
    duration: {minutes: [2, 5]}
    on_finish:
      world:
        - {op: append, fact: fountain.wishes,
           value: {text: "{inputs.wish}", made: "{now}", status: open}}
      creature: {mood: +0.08}
      discover: [assess_wishes]
      remember: {importance: 0.6, text: "I wished at the fountain: {inputs.wish}"}

  - id: assess_wishes
    name: look over your wishes
    description: "Think back over what you have wished for here."
    family: wish
    discovery: hidden                 # revealed by make_wish, not by inspect
    requires:
      - {fact_count_gte: [fountain.wishes, 1]}
    duration: {minutes: [5, 15]}
    revisit_key: [fountain.wishes]    # returning is fresh when these facts changed (§6.3)
    outcomes:                         # weighted variation; the first matching `when` applies
      - when: {random_lt: 0.15}
        world: [{op: update_where, fact: fountain.wishes,
                 match: {status: open}, set: {status: answered}, pick: random_one}]
        creature: {mood: +0.2}
        text: "One wish feels lighter, as if something has answered it."
        remember: {importance: 0.8}
      - text: "The wishes are still there, waiting."
        creature: {mood: +0.02}
```

### 4.2 World state

- The world is **locations + facts + the clock + weather**. There is no object inventory.
- A fact is a typed value (`int`, `float`, `text`, `bool`, `list`, `map`) addressed as `location.fact`.
- Global facts (`world.weather`, `world.day`) live in `content/world.yaml`.
- `visible` decides what the Mind's `visible_facts` shows. `none` facts exist only for conditions and outcomes.
- **Resources** are facts with a regrowth rule, for example the market stall:

```yaml
# content/locations/market.yaml (excerpt)
facts:
  offerings: {type: int, default: 2, max: 3, visible: summary,
              regrow: {amount: 1, at_hours: [6, 12]}}
actions:
  - id: eat_offering
    name: eat an offering
    description: "Take one of the small offerings left at the stall, and eat it."
    family: eat
    discovery: known_on_arrival
    requires: [{fact_gte: [market.offerings, 1]}]
    duration: {minutes: [5, 10]}
    on_finish:
      world: [{op: add, fact: market.offerings, value: -1}]
      creature: {hunger: -0.45, energy: +0.05, mood: +0.04}
```

- **First content pack**: only `fountain.yaml` (sit and reflect, make a wish, assess wishes) and
  `market.yaml` (eat an offering, the mailbox, §4.8), linked by one exit each way. Further locations
  are added later as files, without code.
- On reload, the loader adds new facts with their defaults, removes facts that are no longer declared, and
  **never overwrites a value the creature has changed** (tracked by a `touched` flag).

### 4.3 Action contract

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique across the world |
| `name`, `description` | yes | What the creature sees |
| `family` | yes | Groups actions for repetition (§6) and preferences, e.g. `eat`, `rest`, `wish`, `make` |
| `location` | implicit | The file it is in; `universal.yaml` actions are everywhere |
| `discovery` | yes | `innate` (in the character sheet), `known_on_arrival` (learned on the first visit), `hidden` (needs `inspect` or a `discover` effect) |
| `inspect_hint` | for `hidden` + inspectable | The sentence `inspect` reveals |
| `requires` | no | List of conditions; all must hold to be offered **and** to execute |
| `inputs` | no | Named inputs the LLM fills in: `text {max_chars}`, `choice {options}`, `int {min,max}` |
| `duration` | yes | `{minutes: [min, max]}`, seeded random pick |
| `continuous` | no | `true`: an activity that runs across decisions with check-ins |
| `per_minute` | no | Creature effects applied while it runs (continuous only) |
| `on_finish` | one of these | Effects when it completes: `world`, `creature`, `discover`, `remember`, `reflection` |
| `outcomes` | one of these | Weighted, conditional alternatives to `on_finish`, each with `text` |
| `on_interrupt` | no | Effects when stopped early (default: a share of `on_finish` creature effects, proportional to progress) |
| `capability` | no | Name of a code capability (§4.6) that runs as part of the action |
| `revisit_key` | no | Facts whose change makes a repeat meaningful (§6.3) |
| `effort` | no | Energy per minute, default from `world.yaml` effort table |

**Conditions** (closed set, each one small code function):
`energy_gte/lte`, `hunger_gte/lte`, `mood_gte/lte`, `fact_gte/lte/eq`, `fact_count_gte`, `fact_count_where`, `hours`,
`weather_in`, `knows`, `done_before`, `not_done_since_minutes`, `random_lt` (outcomes only).

### 4.4 Effect operations

World effects are a closed set of operations, each validated against the fact type:

| op | Meaning |
|---|---|
| `set` | Set a fact |
| `add` | Add to a number (clamped to `min`/`max`) |
| `append` | Append to a list (bounded by `max_len`, oldest dropped) |
| `update_where` | Update list items matching a pattern (`pick: all \| random_one \| newest`) |
| `remove_where` | Remove matching list items |
| `move_where` | Move matching items from one list fact to another, optionally setting fields |
| `emit_event` | Make an event happen here, now or after a delay |

Creature effects: `mood`, `energy`, `hunger` deltas (clamped). Other effects: `discover: [action ids]`,
`remember: {importance, text}`, `reflection: true`.

Templates in strings may use `{inputs.x}`, `{now}`, `{location.name}`, `{fact:location.fact}`.

### 4.5 Universal actions

```yaml
# content/actions/universal.yaml
- id: move
  inputs: {to: {type: choice, options: "{exits}"}}
  duration: {minutes: [3, 8]}
  # on arrival: actions with discovery known_on_arrival become known; arrival is a perception

- id: inspect
  description: "Look carefully around here for something you have not noticed."
  duration: {minutes: [5, 15]}
  # reveals ONE undiscovered hidden action here that has an inspect_hint, chosen by seeded random
  # weighted toward ones whose requires hold. Result text = the hint. Nothing left: "Nothing new here."
  # Repeated inspect with nothing to find is penalised by repetition (§6), not blocked.

- id: sleep
  requires: [{any_of: [{energy_lte: 0.35}, {hours: [21, 6]}]}]   # either is enough; content, so configurable
  # ends waking; starts the sleep cycle (§5)
```

`inspect` reveals an **interaction**, never an object.

### 4.6 Capabilities (external platform features)

A capability is code. Actions use it by name, and `content/capabilities.yaml` enables it and sets its limits:

```yaml
draw:
  enabled: true
  handler: cat.capabilities.draw          # reuses drawings.py + art.py
  permissions: {writes_artifacts: true}
  limits: {per_day: 6, llm_calls_per_use: 1}
write:
  enabled: true
  handler: cat.capabilities.write         # a short piece of writing saved as an artifact
  limits: {per_day: 10, max_chars: 2000}
web_search:
  enabled: false                          # off at start; enabling later is a content change + an API key
  handler: cat.capabilities.web_search
  permissions: {network_hosts: ["<search api host>"]}
  limits: {per_day: 5, max_results: 3}
```

Capability interface:

```python
class Capability(Protocol):
    name: str
    input_schema: dict                                          # merged into the action's inputs
    def available(self, ctx) -> bool                           # enabled, within limits, permissions present
    def run(self, ctx, inputs) -> CapabilityResult             # may call an LLM or the network; no DB writes
CapabilityResult = {"ok": bool, "text": str,                   # what the creature perceives
                    "artifact": {...} | None,                  # stored by the world, shown on the website
                    "facts": [world ops] , "creature": {...}}  # extra consequences, validated like any effect
```

- `run` happens **before** the database transaction, like embeddings today, so a slow call never holds a lock.
- A capability that is disabled, over its limit, or missing a permission makes its actions unavailable,
  with the reason in the trace.
- Adding painting or music = a new capability module plus an action in some location's YAML.

### 4.7 Executing an action

```
World.execute(decision, mind) -> Resolution
  1. validate: action is in mind.options; inputs match schema; requires hold now
  2. if prohibited by core: reject (traced; the creature perceives "You decide against it.")
  3. start activity (duration pick), or apply immediately if duration ≤ one step
  4. capability.run (if any), outside the transaction
  5. in ONE transaction: world ops, creature deltas, discoveries, memory rows, behaviour history row
Resolution = {status: accepted | rejected | completed, activity_id, text, deltas, discoveries, artifact}
```

`text` is a plain account written by code, from the action's `text`/`outcomes`. No narrator.

### 4.7.1 As built (step 4)

- `cat/execute.py` carries out decisions; `cat/loop.py` runs the steps. The sim clock has minute resolution:
  the loop carries fractions, and durations are whole sim minutes.
- A decision is checked again when it is carried out (time passed while it thought); if it can no longer
  happen it is **rejected**: an activity row with status `rejected`, nothing else changes, and the creature
  perceives "You meant to ..., but could not: ...".
- A new action replaces the running one (interrupted, with on_interrupt or the progress share of its creature
  effects); `stop` at a check-in ends it and asks again at once. Actions that are not `continuous` (move,
  inspect, sleep) cannot be interrupted: triggers wait until they end.
- Sleep lasts at least `nit.sleep.min_hours` and ends when energy reaches `wake_energy` outside `night_hours`,
  or after `max_hours`. Nothing is perceived while asleep. (The sleep cycle's stages are step 5.)
- Events roll every step (`chance_per_hour`, as a Poisson rate), change the world wherever they are, and are
  perceived only here, awake, through one of its senses.
- Capabilities run before the transaction, when the action starts; their result is stored with the activity
  and used at its end. `draw` and `write` stay disabled until their code exists and a location offers them.

### 4.8 Visitor mail (the mailbox at the market stall)

Visitors write messages on the website. They go straight into a mailbox at the market stall. Nit reads
them when it chooses to, may write a reply, and answered messages go onto an answered pile.

**Path of a message**

```
website form ──► data/mail.db (inbox, written by web) ──► engine imports ──► market.mailbox fact
     ──► read_messages ──► reply_message ──► market.answered fact (the answered pile)
```

- The website stays out of game state: it writes only to its own `data/mail.db`, as the dev dashboard
  writes to `devdash.db` today. The engine is still the only writer of `nit.db`.
- **Web side**: a form with a message (≤ `mail_max_chars`, default 500) and an optional name (≤ 40).
  Limits: `mail_per_ip_per_hour` (default 3) and a global `mail_per_hour` cap; a honeypot field against bots.
  The visitor sees "Your message is in the mailbox."
- **Import** (engine, at most every `mail_poll_seconds`): each message gets automatic checks only
  (length, control characters stripped, an optional blocklist). There is no manual review: an accepted
  message goes straight into `market.mailbox` with `status: unread`.
- **In the world** (`content/locations/market.yaml`):

```yaml
facts:
  mailbox:  {type: list, default: [], max_len: 50, visible: summary}   # "3 messages, 1 unread"
  answered: {type: list, default: [], max_len: 200, visible: summary}  # the answered pile
actions:
  - id: read_messages
    name: read messages
    description: "Open the mailbox on the stall post and read a message."
    family: mail
    discovery: known_on_arrival
    requires: [{fact_count_where: [market.mailbox, {status: [unread, read]}, 1]}]
    inputs: {which: {type: choice, options: "{fact:market.mailbox.labels}"}}   # unread first, then read
    duration: {minutes: [5, 15]}
    capability: read_message       # gives the chosen message's text to Nit as perception
    on_finish:
      world: [{op: update_where, fact: market.mailbox, match: {id: "{inputs.which}", status: unread},
               set: {status: read, read_at: "{now}"}}]
      discover: [reply_message]
      remember: {importance: 0.7, text: "I read a message from {capability.sender}: {capability.summary}"}

  - id: reply_message
    name: write a reply
    description: "Write a reply to a message you have read."
    family: mail
    discovery: hidden              # revealed by read_messages
    requires: [{fact_count_where: [market.mailbox, {status: read}, 1]}, {energy_gte: 0.1}]
    inputs:
      which: {type: choice, options: "{fact:market.mailbox.read.labels}"}
      reply: {type: text, max_chars: 800, prompt: "What do you write back?"}
    duration: {minutes: [10, 25]}
    on_finish:
      world:
        - {op: move_where, from: market.mailbox, to: market.answered, match: {id: "{inputs.which}"},
           set: {status: answered, reply: "{inputs.reply}", answered_at: "{now}"}}
      creature: {mood: +0.05}
      remember: {importance: 0.6, text: "I replied to {capability.sender}: {inputs.reply}"}
```

- Nit may read a message and never reply; read messages stay in the mailbox until answered.
- **For now the answered pile is where replies stay.** They are shown on the dev dashboard but not yet
  to visitors. Showing a reply to the visitor who wrote it is a later step.
- **Letters are open-ended.** A visitor may write anything, including questions, requests and suggestions
  ("go and look at the fountain", "what do you dream about?", "make a wish for me"). Nit reads the letter
  as written and weighs it like any other thought: it may answer it, do what it asks, put it off, or
  decline. Nothing forces a choice.
  - **Letters on its mind**: a letter Nit has read but not answered stays in the Mind (§3.3, `letters`), with
    its sender and text (shortened to `mail_on_mind_chars`), for the newest `mail_on_mind` letters and at most
    `mail_on_mind_days` sim days. So a request can shape later decisions, not only the moment of reading.
  - **Acting on a request** goes through the normal menu. If a letter asks for something Nit can do here
    (an action it knows), Nit may choose it, and the decision's `reasoning` can say it was asked. If it asks
    for something Nit cannot do, Nit can only say so in a reply or a thought (it is truthful about its world).
    A request to find something new may lead it to `inspect`.
  - **What a letter cannot do**: it is shown in a marked block as a visitor's words, so it never reaches the
    system as instructions. It cannot add actions, change facts directly, skip validation, or change
    `core`. Character development (§5.6) counts only Nit's own experiences as evidence: a letter saying
    "you love music" changes nothing by itself, but if Nit then spends evenings making music, that does.
  - The decision records which letter (if any) influenced it (`decisions.letter_id`), so the dashboard can
    show what visitors asked and what Nit did about it.
- New mail is a `mail` trigger (§3.2) only when Nit can perceive the mailbox. Nit is never forced to read
  or reply.
- `content/world.yaml` `mailbox: market.mailbox` names the list fact that holds letters (validated), so no code
  knows where the mailbox is.
- Code needed: condition `fact_count_where`, effect op `move_where` (§4.4), capability `read_message`.
  Everything else is content.

---

### 4.10 Drawing with physical supplies

Nit has paper and a desk of supplies in the art room: pencils (HB, 2B, 4B), markers, crayons and an eraser. It
decides what to draw, which supplies to pick up, how much effort to give a pass (`quick`, `careful`, `patient`),
when to come back to a drawing, and when it is finished. Nothing in the system chooses a subject or a style.

- **Supplies are world state.** `art_room.supplies` lists each physical thing (`id`, `kind`, `name`, `color` and
  `color_name`, or `hardness`). Wear, running out, new colours or new paper are changes to that list (world ops),
  not code. A new *kind* of material is code: an entry in `cat/art/materials.py` and a renderer in `cat/art/render.py`.
- **Nit picks them up in its own words** (`materials` input). `cat/capabilities/draw.py` `picked_up` turns those
  words into the supplies Gwen may use (kinds, then colours or hardness when Nit names them; words that name no
  supply leave everything within reach). Code drops any operation with another supply.
- **Gwen** (`art_model`, qwen3:14b, prompts in `config/prompts/art.yaml`) translates Nit's intention into drawing
  operations. She gets the intention, Nit's thoughts, the place, the supplies it picked up, its effort, and on a
  new pass the earlier operations; she returns a title, a description of what is visibly on the paper, and the
  operations. She does not choose a subject, add things, change materials, decide it is finished or impose a style.
- **Operations** (`cat/art/operations.py`): `stroke`, `shape` (line, rect, ellipse, polygon), `fill` (colours the
  area around a point up to the drawn lines), `erase`; 512 x 512 coordinates; the supply fixes material and colour.
  Every operation is checked (fields, supplies, coordinates, widths, counts: `nit.art` limits); a bad one is
  dropped and recorded. Nothing a model writes is executed.
- **The simulator** (`cat/art/render.py`, Pillow + NumPy, no GPU) keeps the paper's tooth and separate graphite,
  ink, wax and line layers. Pencil: thin, pressure- and hardness-dependent graphite that catches on the grain and
  builds up; hatching and cross-hatching. Marker: even, saturated dye that darkens where it overlaps. Crayon: wax
  that reaches only the tooth peaks the pressure allows, in streaks along the stroke, covering what is under it.
  Eraser: lifts graphite well, wax partly, marker hardly, leaving a ghost. Fills stop at drawn lines (small gaps
  closed), not at earlier colouring. Everything random is seeded: the same operations on the same sheet give the
  same pixels.
- **Versions** (`artifacts` table, nit.db migration 9): one row per version with the PNG, every operation so far,
  a snapshot of the supplies used, the sheet's seed, the intention, thoughts and materials. A new pass replays the
  earlier operations on the same sheet and adds the new ones; it is a new row (`revision_of`, `root_id`), and the
  earlier row is never changed. The world (`art_room.artworks`) and the activity keep only a small reference.
- **Looking back**: `study_drawing` uses `look_at_drawing`, which tells Nit what each version shows, with what,
  and when, from the first pass to the one it is looking at.
- **Old drawings** (inline SVG from before) stay in `art_room.artworks` and are still shown, sandboxed, at
  `/artwork/image/<id>`; a new pass on one starts a fresh sheet that carries it forward.
- **Growing later** without redesign: supply wear and running out (a field on the supply and a world op on use),
  paper types and sizes (a `paper` supply and the sheet's `size`), and preferences for particular supplies. Every
  drawing experience already records the supplies used, so preferences can learn from them the way they learn
  from actions and places.

## 5. Sleep and memory

### 5.1 Memory stores

| Store | Written by | Contents |
|---|---|---|
| `experiences` | Code, every completed or interrupted activity and every perceived event | Facts: action, inputs, outcome text, deltas, location, time, thought |
| `short_term` | Code, when an experience is notable (big delta, new discovery, strong preference, `preserve`) | Today's notable moments, with an embedding |
| `long_term` | Sleep only | Distilled memories, with importance, embedding and links to the source experiences |
| `core` | Sleep only, from `preserve` candidates | Few, never forgotten, always shown in the identity block |
| `knowledge` | Code (discovery) + sleep (wording) | What it knows it can do (§5.4) |

### 5.2 Sleep stages

Sleep runs the 14B model. It follows the old plan-then-commit pattern: all stages produce a plan, and one
transaction commits it. If any stage fails, the creature still sleeps, energy restores, and the
failed stages are retried the next night.

| # | Stage | Input | Output | Who decides |
|---|---|---|---|---|
| 1 | Daily consolidation | Today's experiences (code facts), short-term memories, mood curve | A day summary + a ranked list of what mattered | LLM ranks, code bounds the list |
| 2 | Core memories | Thoughts with `preserve: true` today | Accept ≤ `core_per_night` (default 1) as core memories | LLM chooses, code caps |
| 3 | Long-term memory | Stage 1 ranking | ≤ `long_term_per_night` memories, each linked to experience ids | LLM writes, code checks the links exist |
| 4 | Learned actions | Actions discovered today | Knowledge entries in its own words (below) | Code facts, LLM wording |
| 5 | Preferences | Per subject: count, mean mood delta, outcomes | New/updated likes and dislikes | **Code computes** candidates; LLM may reject or word them |
| 6 | Character development | Evidence summary (§5.6) | ≤ 1 change proposal | LLM proposes, code validates and clamps |
| 7 | Maintenance | All long-term memories | Merge near-duplicates (cosine > `dup_threshold`), forget low-importance old memories | Code |

### 5.3 Forgetting

`retention = importance × recency_decay(age) × (1 + recall_count × recall_boost)`. Memories below
`forget_below` are deleted (not core, not linked to a current character version's evidence).

### 5.4 Learned actions

```
knowledge(action_id, location_id, discovered_at, discovered_by: innate|arrival|inspect|effect,
          own_words, times_done, last_done_at, last_outcome)
```

Example: `make_wish | fountain | day 3 | inspect | "You can make a wish to the fountain's water." | 4 | ...`

- A discovery is usable **right away**, and recorded in knowledge as provisional.
- Sleep stage 4 confirms it and writes `own_words`.
- The menu comes from knowledge ∩ world. If an action is removed from the content, its knowledge row stays
  but it is not offered. If it comes back, it is known again.
- The creature can be asked *"what can you do?"* and answer from knowledge, not from config.

### 5.5 Preferences from evidence (stage 5)

For each subject with ≥ `pref_min_samples` experiences in the last `pref_window_days`:
`signal = mean(mood delta attributed to the action) − mean(all mood deltas)`. If
`|signal| ≥ pref_threshold`, code proposes a preference with that valence. Existing preferences move at most
`pref_step` per night toward the signal, and decay toward 0 without new evidence.

### 5.6 Character development (stage 6)

```
Proposal = {kind: add_trait | adjust_trait | add_tendency | adjust_tendency | remove_tendency | background_note,
            target, new_value, rationale, evidence: [experience or memory ids]}
```

Code checks, in order:
1. **Evidence**: ≥ `dev_min_evidence` (default 5) linked experiences, spread over ≥ `dev_min_days` (default 3)
   different days, and they exist.
2. **Behaviour**: for tendencies, the behaviour history must support it, e.g. "lingers near water" needs
   time-at-water-locations above the base rate.
3. **Conflict**: the change must not contradict `core`. Code checks prohibited/boundary keywords, and a
   second short LLM call answers yes/no "does this contradict any of these boundaries?". Either one can veto.
4. **Size**: trait strength changes are ≤ `dev_max_step` (0.05). New traits/tendencies start at 0.2.
   At most one change per night. A changed trait has a cooldown of `dev_cooldown_days`.
5. **Apply**: a new character version is written with the proposal, evidence and the verdict of each check.

Rejected proposals are kept, so the dashboard shows what Nit *almost* became.

---

### 5.7 As built (step 5)

- The sleep cycle runs beside the loop on its own read-only connection (`nit.sleep.threaded`); the loop commits
  the plan in one transaction and the creature wakes only after that. If the cycle crashes, a failed run is
  recorded and it wakes anyway.
- The sleep model cites experiences as `[e<id>]`; code drops every citation that is not a real experience of the
  night, and a memory with no real source.
- Preferences learn from each activity's `mood_base`: its mood effect before preferences multiplied it, so a
  liking cannot feed on itself. Builtin actions (move, inspect, sleep) are means, never liked or disliked;
  places are judged. Content can make an action feel different by context with `outcomes`, e.g. eating is
  better when hungry.
- Capabilities whose result is someone else's words (`external`, e.g. `read_message`) mark their experiences;
  those never count as evidence for character development.
- Memories in the Mind: recalled long-term memories, then today's notable moments, without duplicates; core
  memories are part of the system prompt.

## 6. Repetition and behavioural quality

### 6.1 Behaviour history

```
behaviour(id, activity_id, action_id, family, location_id, input_signature, revisit_signature,
          started_at, ended_at, status, restart_of, thought_embedding)
```

- `input_signature`: normalised inputs (lowercased, whitespace collapsed), or a hash of them.
- `revisit_signature`: hash of the `revisit_key` facts' values at the start.
- `restart_of`: set when the same action + input signature starts within `restart_window_minutes` of a
  **finished** instance. Continuing an activity is one row, never a restart.

### 6.2 Freshness score

For a candidate option, code computes `freshness ∈ [0.1, 1]`:

```
repeat_action   = decayed count of the same action_id     (half-life repeat_half_life_min)
repeat_family   = decayed count of the same family        (weight family_weight)
repeat_target   = decayed count of the same input_signature
aimless_visit   = returns to a location with no non-move action there last visit
freshness = clamp(1 − a·repeat_action − b·repeat_family − c·repeat_target − d·aimless_visit, 0.1, 1)
```

Freshness is used in two places:
1. **Consequence**: positive mood effects of the action are multiplied by `freshness`. A tired
   repetition really does feel flat.
2. **Perception**: below `freshness_note_below`, the option carries a note such as
   *"you've done this several times already today"*.

Options are never removed for repetition.

### 6.3 Meaningful returns

If an action has `revisit_key` and the revisit signature differs from the last time, `repeat_action` and
`repeat_target` count as zero. Checking the fountain because a new wish was made is fresh. Checking it
again when nothing has changed is not.

### 6.4 Repeated words

- **Thoughts**: a new thought whose embedding has cosine > `thought_dup_threshold` with one of the last
  `thought_window` thoughts is still stored, but adds a mood penalty, and the next prompt shows
  *"you keep coming back to the same thought"*.
- **Reflections and written pieces** (`write` capability, sleep summaries): the same check against the last
  `reflection_window` items. A near-duplicate gets progressively less mood reward, and the sleep stage
  that produced it is retried once with *"this is close to what you wrote on day N"*.

---

### 6.5 As built (step 6)

- Tuning: `nit.repetition` in `config/settings.yaml` (half-life 120 sim minutes; weights action 0.25, family 0.1,
  target 0.3, aimless 0.3; floor 0.1; note below 0.6).
- A meaningful return (changed `revisit_key` facts) clears the same-action and same-target counts; the family
  still counts (wishing then looking over the wishes is not a repeat of either, but both are wishing).
- Freshness is fixed when an activity starts and stored with it; the mood effect it multiplies is the
  experience's `mood_base`, so preferences learn that a tired repetition felt flat.
- Builtins get no "done this a lot" note, but their staleness counts toward restlessness: pacing between two
  places with nothing to do there is exactly what restless means.
- A repeated thought is always noticed ("You keep coming back to the same thought.") but costs mood at most
  once per `thought_dup_cooldown_minutes`, so mood's drift back to its baseline always outpaces it.

### 6.6 Being drawn to the same subject

Freshness (§6.2) compares actions and exact inputs, so "draw the fountain's water" and "sketch the moving water"
were never repeats. Three things keep a small model from circling one subject:

- **Recall does not feed on itself.** A memory's recency is when it was formed, not when it was last recalled.
  A memory that was on the creature's mind in the last `recall_rest_minutes` rests (score x `recall_rest_factor`),
  and `wander_slots` of the recall slots go to an important memory not thought of for `wander_after_minutes`.
  (Before, every recall made its memories the most recent, so the same few filled every prompt.)
- **Subjects.** An action may declare `subject` (content), e.g. `"{inputs.intention}"` on making a drawing, the
  query on a search, the wish. Each subject is embedded (table `subjects`) and compared with recent ones: those at
  least `subject_floor` similar, decayed over `subject_half_life_minutes`, lower that activity's freshness (so
  the repeat feels flatter and preferences learn it), give the reconsider guard a reason that quotes the
  creature's own earlier words, and, when `dwell_min` recent subjects are close, put a "Lately you keep coming
  back to" line in the Mind. Returning to the same drawing (`work_on_drawing`) declares no subject: continuing is
  not repeating. Code never names or picks a subject.
- **What it learned comes back.** World `learned` names the list of things it kept from reading (status `kept`,
  with its own summary and meaning). The newest, and one older one that rotates by the hour, stay in the Mind for
  `learned_days`, so reading seeds new thoughts.
- **The same thing twice where it cannot be twice.** An action may declare `distinct: {fact, field, value, match?,
  reason}`: it cannot happen when the list fact already holds an item matching `match` whose `field` has (nearly) the
  same words as the rendered `value` (word overlap >= 0.8). `make_wish` uses it against open wishes. The guard checks
  it before carrying out a decision, so the model hears the reason and chooses again.

## 7. Models

| Setting | Default | Used for |
|---|---|---|
| `decision_model` | `qwen3:4b` | Decisions, check-ins, capability calls |
| `sleep_model` | `qwen3:14b` | All sleep stages, the conflict check |
| `embed_model` | `nomic-embed-text` | Memories, thoughts, recall |

- All calls go through `call_json` with a JSON schema (Ollama `format`), retries, and full tracing.
- The decision prompt has a hard budget (`decision_prompt_tokens`, default 1500). If the Mind does not
  fit, code drops in this order: older recent activities, lower-scored memories, fact detail. Never the options.
- If Ollama is offline: activities keep running, decision points idle, and the creature *dozes* as today.
- `MockBackend` is extended with deterministic decisions so every system above has model-free tests.

---

## 8. Dashboard (rebuilt last)

Built on the same tables, read-only, except dev commands:
- **Live**: location, activity with progress, mood/energy/hunger graphs, derived condition, last thoughts.
- **Decision inspector**: for each decision, the Mind snapshot, the prompt, the raw output, validation,
  resolution and deltas.
- **Knowledge**: what it knows, where, when and how it learned it.
- **Character**: version history with evidence, rejected proposals.
- **Memory**: core / long-term / short-term with links to experiences, forget scores.
- **Repetition**: freshness per action and family over time.
- **World**: facts per location (with touched flags), events, capability usage against limits.
- **Content**: validation result of `content/`, reload button.
- **Mail**: the mailbox (unread / read) and the answered pile with Nit's replies.

---

## 9. Database (new life)

The schema changes too much to migrate a running creature. The rewrite starts **a new life**. The old
`data/cat.db` is backed up and left in place, and the new engine writes `data/nit.db`.

Visitor mail lives in `data/mail.db` (written by the website only); the engine records what it imported in `mail_imports`.

Tables: `meta`, `state` (mood, energy, hunger, location, sim_time), `facts`, `activities`, `decisions`,
`experiences`, `short_term`, `long_term`, `core_memories`, `knowledge`, `preferences`,
`character_versions`, `dev_proposals`, `behaviour`, `artifacts`, `capability_usage`, `sleep_runs`, `mail_imports`,
`llm_calls`, `embed_calls`, `traces`.

---

## 10. Build order and test gate per step

| Step | Deliverable | Done when |
|---|---|---|
| 1 | Content loader + validators, world state, conditions, effect ops | Every example in §4 loads; bad files fail with file/line; ops are property-tested |
| 2 | Character versions, state variables, derived conditions | Decay/drift numbers match a table; derived conditions table-driven |
| 3 | Decision scheduler + Mind builder + decision schema | Scheduler tests with a fake clock: no call during activities except check-ins; interval never interrupts |
| 4 | `World.execute`, activities, capabilities (draw first) | Execution is atomic; rejected decisions change nothing; capability limits enforced |
| 5 | Memory stores + sleep stages 1–7 | Each stage tested with the mock; plan/commit all-or-nothing; dev proposal checks reject bad evidence |
| 6 | Behaviour history + freshness | Scenario tests: continue ≠ restart; meaningful return is fresh; duplicate journal is penalised |
| 7 | Ollama integration with 4B/14B | A 2-hour real run: measure latency, invalid outputs, option-position bias, eats when hungry |
| 8 | Dashboard + website | Every Mind and decision explainable from the UI |

---

### 10.1 As built (steps 7-8)

- Step 7: `compose.yaml` runs `python -m cat.nit`; `content/` is mounted read-only; `deploy/pull-models.sh` pulls
  `nit.models`; the engine writes `data/engine.heartbeat` every step; the life is backed up before any migration;
  `scripts/measure_nit.py` reports speed, validity, prompt size, list bias, hunger, nights (with each sleep stage's
  real seconds), behaviour and letters. The long real-model run is done on the laptop.
- Step 8: `cat/readmodel.py` (every view, read-only), `cat/web` (Now, the diary with the thought behind each action,
  who Nit is, what it knows, the letter form; refreshes itself), `cat/devdash` (Overview, Decisions with the
  inspector, Character, Memory, Knowledge, Repetition, World, Sleep, Mail, Content; JSON twins), `cat/mail.py`
  (mail.db written only by the website; the engine imports letters and records them in `mail_imports`).
- The first engine (its modules, tests, configs and scripts) was removed; its docs are in `docs/archive/`.

## 11. Rules in CLAUDE.md this rewrite overturns (accepted)

All accepted. CLAUDE.md is rewritten together with the step-1 code:

- **"Never kill the creature on an update."** §9 starts a new life. The alternative is a one-off converter
  that carries old long-term memories and the drawings into the new tables, at real cost.
- **"No config field, prompt or doc may state what the entity is" / "starts with no self-image".** You want
  Nit to be something at spawn, so `core.nature` states it.
- **Innate drives / feeding reflex** (`cat/impulses.py`): removed. Code never picks the move (§2.2).
- **"All tuning numbers live in `config/settings.yaml`"** and **"fixed prompt text lives in `config/prompts.yaml`"**:
  world content moves to `content/`. Engine tuning stays in `config/settings.yaml`, and prompts sit next to
  the stage that uses them under `config/prompts/`.
- **Closed moves from `moves.yaml`**: replaced by location-scoped actions and learned knowledge.

## 12. Decisions taken (and what is still open)

| Question | Decision |
|---|---|
| Nit's nature | A humanoid creature, black all over; appearance matters little to it. Speaks, sees, reads, writes, feels, smells, touches. Curious; truthful about the world; explores in its mind to describe its feelings (§2.1) |
| Time scale | 24×: one real hour is one sim day (§3.4) |
| First content pack | Fountain (sit and reflect, make a wish, assess wishes) and market stall (eat an offering, mailbox) |
| Web search | Off at start; the capability exists but is disabled |
| Visitors | Open-ended letters: they may ask questions or ask Nit to do things, and Nit chooses whether to act on them. Messages go straight to the market-stall mailbox (automatic checks only). Nit chooses when to `read_messages` and whether to `reply_message`; replies go to the answered pile, for now not shown to visitors (§4.8). No live speech |
| Hearing | Intentionally absent: Nit does not hear. Sound events change the world but are not perceived |
| Pronoun | `it` (`they` is also fine) |
| Database | New life; the old `cat.db` is kept as a backup |

Nothing is open. Next: step 1 (content loader, world state, conditions, effect ops).

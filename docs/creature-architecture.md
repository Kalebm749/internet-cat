# Internet Cat: Creature Architecture (v0.4)

A framework for a conscious entity that lives in a simulated world. It thinks with an LLM, while plain code tracks time, needs and rules. Inspired by pinos.world. Pino's internals aren't public, so this is our own design.

**Nothing here assumes a form.** The framework makes no assumption about the entity's species, body or appearance. The current character, Gregorovitch, is one configuration of it. The entity starts with no self-image and may or may not come to think of itself as having any form. Whatever it comes to believe about itself is learned during sleep (§7) and stored as data, never written into the config or the code.

**Paused:** the original cat character and its 3D model work are paused. They are not part of the design. The old cat image assets and the script that generates them stay in the repository, unused.

**Scope for now:** a single creature living on its own. No visitor interaction yet.
**Hardware:** everything runs on one Ubuntu laptop (GTX 1060 6GB, 16GB RAM) under Docker Compose: Ollama (the brain), the world loop, the state store and the website (see §8).

Related docs: `server-architecture.md` (how it is built and deployed, including host setup), `web-gui.md` (the website), `config-reference.md` (config fields).

---

## 1. Design principles

1. **The character is data.** An entity is defined by its config (name, voice, traits, needs). The engine is the same for every entity.
2. **No assumed form.** Nothing in the code, config or docs assumes a species, body or appearance. A self-image, if one forms, is learned and revisable (§7).
3. **Actions aren't predefined.** The creature can do anything that makes sense in its situation. There's no fixed action menu.
4. **Open actions, closed effects.** Actions are freeform, but they can only change the world through a small, fixed set of state changes that code checks (see §5). This keeps consequences persistent and stops things appearing out of nowhere.
5. **The actor never grades itself.** The creature decides what to do. A separate, personality-free narrator decides what happened and how it affected needs.
6. **The LLM describes, code calculates.** LLMs return words and coarse buckets. Code turns those into numbers, clamps them and applies them.
7. **The LLM gets a situation, not instructions.** The brain receives an overview of its world and inner state, not "choose one of these options."
8. **Needs shape the prompt, not the choice.** Need levels become words in the brain's prompt, and only the most pressing ones are shown. Code never picks, suggests or ranks actions by need, and the brain may still do anything. Actions change needs only through the narrator's outcome buckets (which only add); drains come from the world (decay, events, repetition, dislikes). Energy is a separate resource: effort spends it, sleep restores it.
9. **Personality changes slowly.** Traits drift a little each night and never get rewritten in one go.

---

## 2. Components

| Component | Owns | Implemented by |
|---|---|---|
| **World** | Clock, areas, objects and their state, world events (which can drain needs), need decay | Code + world config |
| **Character** | Identity, core traits, tendencies, needs, energy, mood, self-image, inventory, location | Config + saved state |
| **Memory** | Short-term memories (today), long-term memories (consolidated), full diary log | Code (storage) + LLM (consolidation) |
| **Brain (actor)** | Thinking, feeling, choosing the next action | LLM call #1 |
| **Narrator (world/game master)** | Resolving an action into an outcome, need gains and world changes | LLM call #2 |
| **Sleep cycle** | Memory consolidation, trait drift, preference learning | LLM call(s), nightly |

### Character model
- **Identity:** name, voice, optional backstory. Never changes. There is **no species, body or appearance** in the config: the entity does not know what it is.
- **Self-image:** what it currently thinks it is, as a few short text lines (each with a confidence and the day it was formed). It starts empty, is learned and revised during sleep (§7), and may stay empty or never mention a form. An optional text hint can be set in `character.yaml` (`self_image.seed`, empty by default).
- **Core traits:** a few defining traits ("curious", "sentimental"). Never change. They keep the creature recognisably itself.
- **Tendencies:** traits that can drift, stored as numbers from 0 to 1 (e.g. boldness, sociability, love of rain). They change a little during sleep.
- **Preferences:** learned likes and dislikes ("the oak tree in the park"). Formed during sleep.
- **Needs:** five in the placeholder character: sustenance, wonder, joy, peace and curiosity. Each runs 0–100 and has a `decay_per_hour`, a **meaning** (a sentence saying what the need is) and text **expressions** for four bands: `full`, `okay` (may be null), `low` and `empty`. A level becomes a band by thresholds in `settings.yaml` (`need_bands`). Which needs exist is set per character. There are no per-need flags: the only special rule is that the sustenance need only rises when an edible object is consumed.
- **Energy:** a separate resource, one number from −50 to 50. Effort spends it; sleep (and dozing) restore it. It is not a need and is never derived from the needs.
- **Mood:** one number, a simple slider from −50 (negative) to 50 (positive), worked out from need **bands**, events and the brain's own feeling (see §5). Never set directly.

### World model
- **Areas:** description and which areas connect to which. v1 uses a fixed, hand-written map in config.
- **Objects:** each has a name, a location (an area, or the creature's inventory), **tags** (`edible`, `climbable`, `warm`, `movable`, `fragile`…) and simple **state** (`full/empty`, `open/closed`). Objects define what's possible. Actions don't. Objects can be added at runtime by events or by the creature (`create`).
- **Events:** the world changes on its own (weather, things dropped, deliveries, seasons), so the creature always has something new to react to.

---

## 3. The tick loop

```
┌─► 1. World tick        clock advances, needs decay, world events roll (and may drain needs)
│   2. Brain (LLM #1)    situation overview → reaction to last outcome + next action
│   3. Narrator (LLM #2) action + world facts → outcome, need gains, effort, duration, world changes
│   4. Apply (code)      validate changes, buckets → numbers (adds only), spend energy, clamp, diminishing returns, advance clock
│   5. Record (code)     write diary entry + short-term memory
└── 6. If tired / night → enter sleep cycle (§7)
```

Two LLM calls per tick. The brain records how it felt about the previous outcome at the start of its next think, so no third call is needed.

---

## 4. Brain (actor) prompt

The brain gets an overview of its situation, not a list of options. Contents:

1. **Identity and personality:** a plain statement that the entity has a name, a voice, core traits and tendencies, that it does not know what it is or whether it has any physical form, and that it may notice things about its own situation and limits but must not assert a form, species or body that is not in its self-image; then its current **self-image** (the lines learned in sleep, or a note that it is empty), core traits, current tendencies, preferences
2. **Time and world area:** time of day, where it is, what this place is like
3. **Energy and mood:** in words ("tired but content"), never raw numbers
4. **What's pulling at it:** the band expressions of only the most pressing needs (see below), in words. Never numbers, never a list of suggested actions.
5. **Objects in this area:** what's actually here (name, tags, state), plus the inventory
6. **What just happened:** the narrator's account of the last action
7. **Recent actions:** the last few ticks
8. **Memories:** relevant long-term memories + today's short-term memories

### What's pulling at you right now
Need levels shape the prompt, not the choice. Each need's level becomes a band (`full`, `okay`, `low`, `empty`). The prompt shows the expression text of at most `max_pressing_needs` (default 2) needs, taken from the lowest bands (`empty` before `low`), ties broken by config order. If nothing is in the `low` or `empty` band the prompt says nothing in particular is pulling, and may show one `full` expression instead. After `stale_after_ticks` ticks in one place the `stale_here` sentence from `character.yaml` is added; that flag changes no need value. The brain still does anything it likes: code never picks, suggests or ranks actions by need. The dev dashboard shows, per tick, which expressions were selected and why.

### Brain output (structured JSON)
- `reaction`: how it feels about what just happened, in its own voice
- `thought`: what's on its mind
- `action`: what it does next, in free text ("climb the old oak to watch the sunset")
- `intent`: why ("restless, want to see something beautiful")

The brain never says how an action affects needs or the world.

---

## 5. Narrator and need scoring

### Narrator input
The action, the creature's location, the area's objects (with tags and state), the inventory, the time and weather, and each need's **meaning** with its current **band as a word** (never a number). No personality: the narrator is neutral, and there is no table saying which actions feed which needs: it decides which needs an action helped from what actually happened.

### Narrator output
- `outcome`: 1–2 sentences of what actually happened (can include bad luck, like getting wet or a slip)
- `gains`: for each need the action helped, a bucket `0`, `+` or `++`. The narrator only ever adds.
- `effort`: `low`, `medium` or `high`
- `duration`: minutes
- `changes`: a list of world changes, using only these types (full validation rules in `server-architecture.md` §9):
  - `move_self(area)`
  - `move_object(object, area)`
  - `take(object)` / `drop(object)`
  - `consume(object)`
  - `set_state(object, key, value)`
  - `create(name, tags, area)`, for creations and things built in the world

### Rules (applied in code)
- **Validate every change.** The object must exist and be within reach. Moves must go to a connected area. Only `edible` objects can be consumed. Invalid changes are dropped and logged.
- **Buckets become numbers:** `+` ≈ +15, `++` ≈ +30 (to be tuned). Effort becomes an **energy** cost (energy is separate from the needs).
- **Drains come from the world, never the narrator.** Needs decay per sim hour; world events can carry `drains`; and while something the entity dislikes is present (the area, an object here, the weather or its own action) the world drains configured needs. Bad outcomes are story only.
- **Sustenance depends on the world.** It only goes up when an `edible` object is actually consumed this tick. Every other need can be helped by almost anything the narrator judges helped it.
- **Repetition pays less.** The gains of the needs listed in `repetition_penalty.needs` (wonder and curiosity) are reduced when the action is similar to a recent one taken in the same place (measured with embeddings). This stops loops and encourages variety.
- **Clamp everything:** needs to 0–100, energy and mood to −50–50.

### Mood calculation
Mood is **one number**, a slider from −50 (negative) to 50 (positive), computed from need **bands** using a mapping in `settings.yaml` (`mood.bands`): points per band for the needs listed (joy and peace raise it, sustenance `empty` lowers it), plus a decaying nudge from world events and the brain's own `feeling`, so mood isn't purely mechanical. Raw levels never enter the mood. Mood is turned into words before it goes into the brain prompt.

---

## 6. Memory

| Layer | Contents | Lifetime |
|---|---|---|
| **Diary log** | Every tick: action, outcome, reaction, need changes, world changes | Permanent, append-only |
| **Short-term** | Today's notable moments | Cleared at sleep |
| **Long-term** | Consolidated memories, learned preferences | Permanent, capped and pruned |

**Recall:** the brain prompt includes the most relevant long-term memories, scored by recency, importance and embedding similarity to the current situation. Embeddings come from a small embedding model running alongside the main model (e.g. `nomic-embed-text`). Memories are stored as text, and the embedding is only a search index, so the website shows the text.

---

## 7. Sleep cycle

Sleep is a real state: it restores energy, and the creature chooses when to sleep. It's also when the heavier batch LLM work runs.

1. **Consolidate memory.** Sum up the day's short-term memories into a few long-term ones and rate their importance. Drop the rest.
2. **Adjust tendencies.** Based on the day, nudge drifting traits. Each trait can move at most a small step per night (e.g. ±0.05). Core traits never change.
3. **Learn preferences.** Repeated good experiences become likes, repeated bad ones become dislikes.
4. **Update the self-image.** One call reads the day's short-term memories and proposes **at most one** new self-image line, or none. A line has text, a confidence (0..1) and the day it was formed. Stored like preferences, so a later line can revise or contradict an earlier one. The number of lines kept is capped in `settings.yaml` (least confident forgotten first) and every proposal and its outcome is logged in the sleep trace. Nothing in the config says what the entity is; the self-image is the only place a form can appear, and only if the entity's own days support it.
5. **Learn habits (optional).** Actions the creature keeps coming back to can be noted as habits and suggested in future prompts.

---

## 8. Hardware and pacing

One Ubuntu laptop (GTX 1060 6GB, 16GB RAM) runs everything as Docker Compose services:

| Service | Role |
|---|---|
| **ollama** | Uses the GPU. Brain, narrator, sleep and embedding calls all go here. |
| **engine** | Runs the world loop, stores state, memory and the diary. |
| **web** | Serves the read-only website on the local network. |

- **Models:** 4–8B, quantized to fit 6GB of VRAM (e.g. `qwen3:8b` or `llama3.1:8b`; `gemma3:4b` as a lighter option). Embeddings: `nomic-embed-text`.
- **Pacing:** a few seconds per call. Tick interval about 1–2 min (to be tuned for how lively vs. how calm it should feel).
- **Brain offline:** if Ollama can't be reached, the world keeps running (clock, decay, events) and the creature is "dozing". It resumes when the brain comes back. The loop must never crash because the brain is unavailable.
- **Upgrade path:** point `ollama_host` at any Ollama-compatible host or a hosted API. The rest of the design doesn't change.

---

## 9. Open questions

- **Growing world:** v1 uses a fixed map. Should the narrator or events eventually be able to add new areas?
- **Creations:** what they are (text, images, objects placed in the world) will be decided during development. Objects can already be placed in the world with `create`.
- **Trait set:** which core traits and tendencies to define for the first real character.
- **Narrator strictness:** how often should things go wrong?
- **Bucket values, decay rates, tick interval:** need tuning by running the loop and reading the diary.
- **Visitors:** out of scope for now. Later they'd come in as world events the creature perceives.
- **3D world:** the long-term goal. The text world and website come first.

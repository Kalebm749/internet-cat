# Internet Cat: Creature Architecture (v0.3)

An autonomous web creature that lives in a simulated world. It thinks with an LLM, while plain code tracks time, needs and rules. Inspired by pinos.world. Pino's internals aren't public, so this is our own design.

**Scope for now:** a single creature living on its own. No visitor interaction yet.
**Hardware:** everything runs on one Ubuntu laptop (GTX 1060 6GB, 16GB RAM) under Docker Compose: Ollama (the brain), the world loop, the state store and the website (see §8).

Related docs: `server-architecture.md` (how it is built and deployed, including host setup), `web-gui.md` (the website), `config-reference.md` (config fields).

---

## 1. Design principles

1. **The character is data.** A creature is defined by its config (identity, traits, needs). The engine is the same for every creature.
2. **Actions aren't predefined.** The creature can do anything that makes sense in its situation. There's no fixed action menu.
3. **Open actions, closed effects.** Actions are freeform, but they can only change the world through a small, fixed set of state changes that code checks (see §5). This keeps consequences persistent and stops things appearing out of nowhere.
4. **The actor never grades itself.** The creature decides what to do. A separate, personality-free narrator decides what happened and how it affected needs.
5. **The LLM describes, code calculates.** LLMs return words and coarse buckets. Code turns those into numbers, clamps them and applies them.
6. **The LLM gets a situation, not instructions.** The brain receives an overview of its world and inner state, not "choose one of these options."
7. **Personality changes slowly.** Traits drift a little each night and never get rewritten in one go.

---

## 2. Components

| Component | Owns | Implemented by |
|---|---|---|
| **World** | Clock, areas, objects and their state, world events, need decay | Code + world config |
| **Character** | Identity, core traits, tendencies, needs, energy, mood, inventory, location | Config + saved state |
| **Memory** | Short-term memories (today), long-term memories (consolidated), full diary log | Code (storage) + LLM (consolidation) |
| **Brain (actor)** | Thinking, feeling, choosing the next action | LLM call #1 |
| **Narrator (world/game master)** | Resolving an action into an outcome, need gains and world changes | LLM call #2 |
| **Sleep cycle** | Memory consolidation, trait drift, preference learning | LLM call(s), nightly |

### Character model
- **Identity:** name, species, appearance, backstory. Never changes.
- **Core traits:** a few defining traits ("curious", "sentimental"). Never change. They keep the creature recognisably itself.
- **Tendencies:** traits that can drift, stored as numbers from 0 to 1 (e.g. boldness, sociability, love of rain). They change a little during sleep.
- **Preferences:** learned likes and dislikes ("the oak tree in the park"). Formed during sleep.
- **Needs:** e.g. food, energy, fun, cleanliness, curiosity. Values run 0–100, each with its own decay rate. Which needs exist is set per character.
- **Mood and energy:** worked out from the needs (see §5). Never set directly.

### World model
- **Areas:** description and which areas connect to which. v1 uses a fixed, hand-written map in config.
- **Objects:** each has a name, a location (an area, or the creature's inventory), **tags** (`edible`, `climbable`, `warm`, `movable`, `fragile`…) and simple **state** (`full/empty`, `open/closed`). Objects define what's possible. Actions don't. Objects can be added at runtime by events or by the creature (`create`).
- **Events:** the world changes on its own (weather, things dropped, deliveries, seasons), so the creature always has something new to react to.

---

## 3. The tick loop

```
┌─► 1. World tick        clock advances, needs decay, world events roll
│   2. Brain (LLM #1)    situation overview → reaction to last outcome + next action
│   3. Narrator (LLM #2) action + world facts → outcome, need gains, effort, duration, world changes
│   4. Apply (code)      validate changes, buckets → numbers, clamp, diminishing returns, advance clock
│   5. Record (code)     write diary entry + short-term memory
└── 6. If tired / night → enter sleep cycle (§7)
```

Two LLM calls per tick. The brain records how it felt about the previous outcome at the start of its next think, so no third call is needed.

---

## 4. Brain (actor) prompt

The brain gets an overview of its situation, not a list of options. Contents:

1. **Identity and personality:** core traits, current tendencies, preferences
2. **Time and world area:** time of day, where it is, what this place is like
3. **Energy and mood:** in words ("tired but content"), never raw numbers
4. **Needs:** in words ("quite hungry", "bored")
5. **Objects in this area:** what's actually here (name, tags, state), plus the inventory
6. **What just happened:** the narrator's account of the last action
7. **Recent actions:** the last few ticks
8. **Memories:** relevant long-term memories + today's short-term memories

### Brain output (structured JSON)
- `reaction`: how it feels about what just happened, in its own voice
- `thought`: what's on its mind
- `action`: what it does next, in free text ("climb the old oak to watch the sunset")
- `intent`: why ("restless, want to see something beautiful")

The brain never says how an action affects needs or the world.

---

## 5. Narrator and need scoring

### Narrator input
The action, the creature's location, the area's objects (with tags and state), the inventory, the time and weather. No personality: the narrator is neutral.

### Narrator output
- `outcome`: 1–2 sentences of what actually happened (can include bad luck, like getting wet or a scraped paw)
- `gains`: one bucket per need, from `0`, `+` or `++`
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
- **Buckets become numbers:** `+` ≈ +15, `++` ≈ +30 (to be tuned). Effort becomes an energy cost.
- **Decay belongs to the world tick.** The narrator only gives positive changes. Bad outcomes are story only, and they affect mood through the brain's reaction.
- **Physical needs depend on the world.** Food only goes up when an `edible` object is actually consumed. Psychological needs (fun, curiosity) can come from almost anything.
- **Repetition pays less.** Gains are reduced when the action is similar to recent ones (measured with embeddings). This stops loops and encourages variety.
- **Clamp everything** to 0–100.

### Mood calculation
Mood comes from:
- **Need levels:** how full the needs are overall
- **Need trends:** filling up feels good, draining feels bad. Trends weigh more than levels.
- **Recent events:** small nudges from world events and outcomes
- **The brain's own reaction:** blended in lightly, so mood isn't purely mechanical

Mood is turned into words before it goes into the brain prompt.

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
4. **Learn habits (optional).** Actions the creature keeps coming back to can be noted as habits and suggested in future prompts.

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

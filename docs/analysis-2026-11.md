# Internet Cat: Vision, Architecture and Codebase Analysis

*Independent review of `main` @ `f3951fe`, which includes the anxiety gauge and impulses (`Kalebm749/internet-cat` PR [#16](https://github.com/Kalebm749/internet-cat/pull/16)) and the "impressions" prompt change (PR [#17](https://github.com/Kalebm749/internet-cat/pull/17)). No code was changed. I read every module in `cat/`, all four config files, the prompts, the schema, the docs and the tests. I ran the test suite (786 passed, mock brain) and `scripts/simulate_balance.py`, before and after anxiety. Section K covers the anxiety system in detail. The real model has not been run on this codebase. `docs/system-overview.md` §5.1 says so too, so every behavioural claim below is about mechanisms, not observed LLM behaviour.*

---

## Status: what the baseline PR changed

This review led to a first "baseline" PR, to make the first real run honest and measurable before any learning is built. What it did:

- **Roadmap 1.1, ledgers separated.** Each tick stores code's plain account of the move (`ticks.result`). The brain's "what just happened" and the sleep day log (which long-term memories come from) use it. The narrator's prose is now diary-only. The consolidation prompt marks "You wrote" words as speech, not fact.
- **Roadmap 1.4b, impulse fixes.** The impulse is rolled before the brain call, so when it fires there is no LLM call and no stray thought or feeling. Impulses and anxiety climbing a band are notable moments, and the day log marks impulse moves.
- **Roadmap 1.4, rebalance.** Need decay was lowered (sustenance 3, wonder 3.5, joy 3.5, peace 6, curiosity 5.5 per hour; ×0.3 asleep), and a second food source was added (an apple that drops in the garden). Under the needs-driven yardstick, "some need empty" fell from 62% to 7% and time above the impulse threshold from 61% to 7%. Anxiety's own settings are unchanged. A test now guards the balance.
- **Roadmap 1.6, object lifecycle.** Objects brought by events and the director expire (`object_lifetime_hours`) and the expiry is audited.
- **Phase 0, measurement.** `scripts/measure_run.py` reports brain errors, latency, prompt size against the context limit, menu-position bias, choice variety, impulses, need bands, anxiety and accumulated inner state.

Deliberately not changed: Nit's character sheet (core traits stay), menu order and tags (so the first run measures list-order bias as it is), and anything in phases 2–5.

---

## The short version

Internet Cat today is a well-built, carefully traced **LLM role-play loop inside a deterministic sandbox**. The engineering is better than most hobby A-life projects: the transactions are atomic, LLM output is validated, code owns every number, everything is traced, and the tests run without a model.

It does not yet **learn** in any meaningful sense:

- **The world can't be learned.** Every move always does the same thing (`cat/moves.py:resolve`). Object properties are shown to the brain as ground truth (`brain.py:_obj` prints the tags). Nothing is hidden, uncertain or variable, so there is nothing to be wrong about.
- **Learned identity is text written by the LLM, read back by the LLM.** Tendencies, preferences, self-image, projects and long-term memories are written by the sleeping LLM, mostly from an LLM-written summary of an LLM-written day. They come back only as prompt words. The one exception, dislikes, drains needs by matching substrings.
- **Habits are the only code-derived identity.** `cat/habits.py` counts what the entity actually did. It is the best model in the codebase of what the vision wants.
- **Emotion now has consequences, but only one input, and it is stuck on.** The new anxiety gauge (`character.anxiety_next`, `cat/impulses.py`) is the first inner state that changes behaviour in code. Impulses override the brain's pick, low needs decay faster, sleep rests less, and mood drops. But its target is computed only from need bands and energy, the same inputs mood already uses. In the balance simulator it sits above the impulse threshold **55–89% of the time**, and it *worsens* needs: under the need-greedy policy, the time some need is empty went from 45% to 55%. As tuned, it is a near-permanent background condition, not a signal (§K).
- **Some predefined personality remains in the prompt.** Nit's `core_traits: [steadfast, curious, cunning]` are injected into every brain and sleep prompt as "always true of you", alongside a written backstory. (Nit replaced the earlier placeholder character in PR #19, with pronoun "they" and no fixed voice, which removed two of the issues this review first found.)
- **Invented prose leaks back in.** The engine says memory must never use the narrator's prose (`engine.py:467`), but that prose becomes the brain's next perception (`engine.py:478`) and the sleep cycle's day log (`sleep.py:day_log`), which is what long-term memories are written from.

The vision is strong in philosophy and weak in operational definitions. The most important change to it is to define **learning** as a code-held state change that measurably shifts future choices. Under that definition, most of what exists today is narration, not development. What's needed is a learning substrate that the LLM *reads*, not one it *is*.

---

## A. Executive assessment

| Area | Score | One-line justification |
|---|---|---|
| Overall project vision | **7/10** | A coherent, uncommon philosophy ("everything has a history"). It is underdefined where it matters ("learning", "belief", "identity"), contains real tensions, and names a gauge (anxiety) that doesn't exist. |
| Architectural foundation | **7/10** | Clean authority boundaries, validated LLM output, one-transaction ticks, extensive tracing, config validation, 776 mock-backed tests. `engine.py` (952 lines) is a monolith, and there is no model of experience between "world truth" and "prompt text". |
| Emergent identity | **3/10** | Habits are genuinely emergent. Everything else is LLM opinion, stored as text. Core traits, needs and the verb→need table predefine a lot of the personality. |
| Memory and learning | **3/10** | Memory storage and recall work. Learning, in the sense of experience changing choice probabilities through state, does not exist outside the prompt. |
| Cognitive architecture | **2/10** | Perception is a dump of ground truth. There are no beliefs, expectations, attention, prediction or planning. Reflection is summarisation. Phase 1 of the cognitive plan hasn't started. |
| Emotional architecture | **4/10** | Mood plus anxiety, and anxiety has real, traced consequences (impulses, decay pressure, worse sleep). Both come only from need bands and energy, with no surprise, uncertainty or event input. Anxiety is on most of the time, nothing remembers it, and memories carry no affect. |
| Autonomy | **4/10** | The entity really does choose freely from a broad menu. Its goals ("projects") are assigned by the sleeping LLM using a suggestion list ("drawing, writing, making music"), and progress is a counter. |
| Simulation design | **5/10** | Deterministic, validated and auditable. But the world is static, transparent and non-learnable. The entity can't change it in any lasting way, and it fills up. Needs are chronically empty even under an ideal policy. |
| Species independence | **6/10** | Real effort went in (neutral prompts, self-image learned from nothing). Body assumptions remain: eating, carrying three things, sleeping, "pounce", "bat", "burrow", "sing out loud". Gender and backstory are still set. |
| Visitor experience | **5/10** | An attractive diary with honest data chips, a biography page and sleep cards. But most visible "development" (artifacts, self-image, tendencies, journal entries) is LLM-generated, and the causal chain behind it isn't shown. |

### Most significant strengths

1. **Clear authority boundaries.** Code owns time, needs, world changes and numbers. The LLM picks from a closed menu (`moves.choose` rejects anything off-menu). `effects.py` validates every world change. The narrator decides nothing. This is the right spine, and most LLM-agent projects don't have it.
2. **Atomicity and recoverability.** Each tick's apply-and-record step is one transaction (`engine._commit`). Sleep is plan-then-commit and all-or-nothing (`sleep.plan` / `sleep.commit`). Errors reload state.
3. **Tracing.** Need arithmetic, mood terms, recall candidate scores, event rolls, effect logs, and sleep inputs/proposals/clamped outputs are all stored (`trace.py`, `sleep_traces`). The project's central principle needs exactly this provenance infrastructure. It just isn't yet attached to the identity state itself.
4. **Habits** (`habits.py`) are the template for everything else: code finds a real pattern in behaviour, the LLM only words it, and the pattern disappears when the behaviour stops.
5. **Bounded LLM influence on slow state.** Tendencies move at most 0.05 a night, new preferences start at half strength, self-image changes by at most one line a night, and everything is capped. The engineering instinct is right. What's missing is the evidence going *in*.
6. **Self-image starts empty** and the prompts forbid asserting a form. That is an honest, species-neutral starting point.

### Most significant weaknesses

1. **There is nothing to learn.** Outcomes are fixed per verb, object properties are shown to the brain, and nothing is uncertain. The design forecloses beliefs, expectations, surprise and imperfect understanding at the world level, not just in the cognition layer.
2. **The slow state is LLM narrative.** Tendencies are judged from a *summary* (`sleep.tendencies_messages` passes only `summary` and the current values). Preferences, self-image and projects are LLM opinions. None of them carries evidence pointers to the ticks that caused them.
3. **Most inner state has no consequences.** Anxiety is now the exception. Mood, tendencies, preferences (except dislike drains), self-image and memories only change prompt words. Whether they change behaviour depends entirely on the LLM noticing a sentence.
4. **Narrated and real experience are mixed** (detailed in §E).
5. **Personality is predefined** through `core_traits`, the needs themselves (wonder, joy, curiosity), and the verb→need gain table (`play → joy ++`). What the entity "enjoys" is set in config, not learned.
6. **It has never been run on the real model.** The whole edifice rests on an unmeasured assumption: that a 14B model with a 4096-token context, reading a roughly 30-item menu, makes choices that reflect the prompt's inner state rather than list position and verb salience.

---

## B. Architecture map

### B.1 Components and authority

```
                        ┌──────────────────────────── config/ (read-only at runtime, validated: cat/config.py)
                        │  settings.yaml  character.yaml  world.yaml  moves.yaml  prompts.yaml
                        ▼
┌─────────────────────── engine container (the ONLY writer of cat.db) ─────────────────────────┐
│ cat/main.py → Engine.run()  (cat/engine.py)                                                   │
│   clock.py        sim time, phases                                                            │
│   world.py        WorldState: objects, weather, roll_events()          ── authoritative truth │
│   character.py    needs decay/drains/gains, energy, mood (pure functions)                     │
│   moves.py        offered() menu, resolve() effects of a move          ── code decides        │
│   effects.py      apply_changes(): validates + applies world ops       ── only world writer   │
│   director.py     LLM proposes a happening → validated → event                                │
│   brain.py        prompt assembly + parse (LLM picks a move id)        ── LLM chooses         │
│   narrator.py     LLM prose of the outcome (fallback: template)        ── LLM describes       │
│   memory.py       short/long memories, embeddings, recall scoring                             │
│   habits.py       code mines patterns; LLM words them                                         │
│   projects.py     project lifecycle (sleep) + progress (code) + artifact text (LLM)           │
│   sleep.py        nightly plan()/commit(): 5–6 LLM calls                                      │
│   trace.py        every number + call → trace tables                                          │
│   devcontrol.py   executes dashboard commands queued in devdash.db                            │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
          │ SQLite (WAL): state, ticks, events, objects, memories, preferences, self_image,
          │               habits, projects, artifacts, sleep_log, + 9 trace tables
          ▼
┌── web (read-only, cat/web) ──┐   ┌── devdash (read-only on cat.db, writes devdash.db) ──┐   ┌── ollama ──┐
│ diary, biography, keepsakes  │   │ ticks, LLM calls, sleep, memory, playground, controls │   │ qwen3:14b  │
└──────────────────────────────┘   └───────────────────────────────────────────────────────┘   │ nomic-embed│
                                                                                               └────────────┘
```

**Authoritative state:** the `state` row (sim time, area, needs, mood, energy, tendencies, activity, asleep), `objects`, and the identity tables. All of them are written only inside `Engine._commit` or `sleep.commit`.

### B.2 The decision lifecycle (`Engine._tick`, `engine.py:296`)

1. **Events roll** (`world.roll_events`): hand-written events with chance, area, hours and weather filters. They may spawn objects or drain needs. Dashboard-injected events are added.
2. **Activity check** (`:307-320`). If a long move is running and no event fired and no need *newly* hit empty, this is a **carry tick** (`_carry_tick`): time advances, needs decay, no LLM call. Otherwise the activity ends and the brain is told "you were partway through…".
3. **Director** (`_director_happening`): every 6 action ticks, or 2 if the place is stale, the LLM proposes a happening. `director.validate` clamps it and it becomes an event.
4. **Rest gate.** If asleep, or it is night and energy is below -25, this is a rest tick (`_rest_tick`): no brain call; it wakes when rested and it is morning.
5. **Recall** (`_recall`): a query is built from area, objects, mood word, events and the last outcome, embedded, and scored as `1.0·recency + 1.0·importance + 1.5·cosine`. The top 5 long-term memories are returned.
6. **Brain** (`brain.think`). System prompt: name, backstory, identity disclaimer, **core traits**, voice. Situation: self-image, tendencies, habits, likes and dislikes, time, place, mood and energy words, ≤2 pressing-need expressions, project, objects **with tags**, exits, events, last outcome, recent actions, recalled and short-term memories, and the **menu** (≤30 moves, seeded random sample, fixed order). The reply is JSON `{reaction, thought, move, why, feeling, entry}`. An off-menu move is retried once and then the tick dozes.
6b. **Impulse** (`impulses.take_over`, `engine.py:350`). With anxiety above 40, a seeded roll (chance up to 50% at 100) may replace the brain's move with a configured "restless" or "freeze" verb from the current menu. The reply's `move`, `why` and `entry` are replaced. Its `reaction`, `thought` and `feeling` are kept. The brain call has already been made and paid for.
7. **Resolve** (`_resolve_move` → `moves.resolve`): gain buckets come from the verb, the target's tags and novelty; the duration is random within the verb's range; world ops are `move_self`, `take`, `drop` or `consume`. **This step is deterministic except for duration.**
8. **Narrate** (`_narrate`): the LLM writes 1–2 sentences of prose from the template outcome. If it fails, the template is used.
9. **Apply** (`:361-436`): effects are validated, then project progress is computed (with an LLM artifact call at milestones). Needs: decay, then event drains, then dislike drains (substring match), then gains × repetition multiplier (0.4 if the same move in the same place was made in the last 6 decisions, wonder and curiosity only) × duration scale. Low and empty needs decay up to 25% faster under anxiety. Energy is spent. Anxiety moves towards a target set by need bands and energy (`anxiety_next`). Mood is recomputed from need bands, the event nudge, the LLM's `feeling` and −0.25 × anxiety.
10. **Notable check** (`memory.notable_reasons`): a need change ≥20, a strong feeling, a new object or a rejected change creates a short-term memory, whose text includes the creature's own journal entry.
11. **Commit** (`_commit`): one transaction writes the tick row, events, world sync, short-term memory, recall marks, sleep flags, project advance, staleness counters and state.
12. **Sleep cycle** (`_try_sleep_cycle` → `sleep.plan`). LLM calls in order: consolidate (summary plus ≤5 long-term memories), tendencies (from the summary only), preferences, self-image, projects, habits (wording only). Then `sleep.commit`: short-term memories are cleared and long-term memories capped at 500.

### B.3 Where the LLM sits (cost per decision: ~2 chat calls + 1 embedding)

| Call | Decides | Writes to state |
|---|---|---|
| brain | **which move** (from the menu) | `ticks.reaction/thought/intent/entry`; `feeling` → mood ±10 |
| narrator | nothing | `ticks.outcome`, which **then becomes perception and the sleep log** |
| director | whether something happens (validated) | events, spawned objects, drains, weather |
| create | nothing | `artifacts.text` (shown on the site) |
| sleep × 5–6 | memories, tendency drift, preferences, self-image, project start/stop, habit wording | all the slow identity state |

### B.4 Architectural inconsistencies and missing abstractions

- **No "experience" layer.** Perception is the world rendered directly into the prompt, including engine tags in brackets. Experience is not stored as structured data: the tick row holds labels and prose, not context, expectation and outcome.
- **No learned-value store.** Nothing maps "(move or object, context) → what happened to me" in a form code can use.
- **Dead world ops.** `effects.py` supports `move_object`, `set_state` and `create`, but no verb can produce them (`moves.DOES` lacks them). The entity can't leave a mark on the world.
- **World saturation.** Objects are removed only by `consume` (`effects.py` → `world.remove_object`). Director and event spawns accumulate until areas reach `max_objects_per_area: 12`, after which spawns are silently skipped. Over months the world freezes.
- **Stale descriptions of the old design.** `projects.py` and `db.py` comments say progress is "advanced by the narrator's judgement", and `settings.yaml` has the projects heading above `habits:`. `views._sleep_entry` reads a `tendency_labels` key that `character.yaml` no longer defines. `creature-architecture.md` still describes the original design.
- **The relaxed rule is half implemented.** Impulses can now change the final pick. Nothing yet changes what is *offered* or how it is shown. The impulse is applied *after* the brain call, so it is not the "fast path without an LLM call" that the cognitive plan describes.

---

## C. Implementation matrix

Status: **I** = Implemented · **P** = Partially implemented · **A** = Architecturally supported · **Pl** = Planned (documented) · **Asp** = Aspirational (no meaningful support). Importance is to the vision (★ to ★★★★★).

| Capability | Current implementation | Evidence | Status | Quality | Missing | Imp. |
|---|---|---|---|---|---|---|
| Time and tick loop | Sim clock, phases, pacing, carry ticks, downtime pause/catch-up | `clock.py`, `engine.run/_tick`, `downtime.py` | I | High | Nothing important | ★★★★ |
| World state and rules | 6 areas, about 30 objects, tags, weather, chance events | `world.py`, `world.yaml` | I | Medium | Latent properties, object lifecycle and decay, variability | ★★★★★ |
| Action validation and execution | Closed menu + `effects.apply_changes` | `moves.py`, `effects.py` | I | High | Context-dependent outcomes; ops that change the world (`set_state`/`create`/`move_object` unused) | ★★★★★ |
| Needs | 5 needs, linear decay (faster under anxiety when low), bands, gains by bucket | `character.py`, `character.yaml` | I | Medium | Balance: some need is empty 55% of the time even under the need-greedy policy (45% before anxiety) | ★★★ |
| Energy and sleep | Effort spends, sleep restores; the brain may choose sleep | `character.spend_energy`, `_rest_tick` | I | High | — | ★★★ |
| Perception | Ground-truth listing of area, objects **and their tags**, exits, events | `brain.situation_prompt`, `brain._obj` | P | Low | Partial or noisy perception, salience, unknown properties | ★★★★★ |
| Attention | Max 2 pressing needs, ties broken by **config order**; menu cut by seeded random | `brain.select_needs`, `moves._cut` | P | Low | Attention driven by affect or expectation; config order is a hidden ranking | ★★★ |
| Episodic memory (short) | Notable ticks as text, incl. the journal entry | `memory.notable_reasons`, `engine:452-473` | I | Medium | Structured episode (context, expectation, outcome, affect) | ★★★★★ |
| Long-term memory | LLM-consolidated sentences, ≤5 a night, cap 500 | `sleep.plan` consolidate, `memory.prune_long` | I | Medium | Evidence links to source ticks; memories are built partly from narrator prose | ★★★★ |
| Recall | recency + importance + 1.5·cosine; top 5 | `memory.recall` | I | Medium | Cue-driven by goals and affect; recall doesn't feed code | ★★★★ |
| Memory → decision influence | Recalled text in the prompt only | `BrainContext.long_memories` | P | Low | Any code path where memory changes options or values | ★★★★★ |
| Memory degradation and transformation | Hard deletion by importance × recency | `memory.prune_long` | P | Low | Gist-ification, distortion by affect, reconsolidation | ★★ |
| Learning from outcomes | **None** (outcomes are deterministic and never compared to expectations) | `moves.resolve` | Asp | — | Expectation store, prediction error, value update | ★★★★★ |
| Expectation and surprise | Not present | — (cognitive plan 1b) | Pl | — | Everything | ★★★★★ |
| Beliefs and worldview | Not present | — (cognitive plan 1c) | Pl | — | Everything; needs hidden world facts to be possible at all | ★★★★ |
| Belief revision | Self-image `replaces` only | `sleep.decide_self_image` | P | Low | Evidence-driven revision of anything other than self-image text | ★★★★ |
| Habits | Code mines (move, part of day); LLM words them; dropped when they stop recurring | `habits.mine/decide` | I | **High** | Habit strength affecting choice (fast path); only a 150-decision window (~3 sim days) | ★★★★ |
| Tendencies (personality) | LLM names and nudges from the **summary**; ±0.05 a night; prompt words only | `sleep.clamp_tendencies`, `tendencies_messages` | P | Low | Behavioural evidence, any mechanical effect | ★★★★ |
| Preferences | LLM opinion from summary and log, blended; dislikes drain peace and joy by substring match; likes have no effect | `sleep.merge_preferences`, `character.dislike_drains` | P | Low–Med | Derivation from experienced outcomes; likes affecting anything; evidence links | ★★★★★ |
| Self-image | ≤1 LLM line a night, confidence, cap 5 | `sleep.decide_self_image` | I | Medium | Grounding in self-observations code can verify (capabilities, limits) | ★★★ |
| Familiarity and attachment | Novelty bonus for areas unvisited for 24h; staleness sentence after 3 ticks | `_is_new_area`, `_stale_here` | P | Low | Per-place and per-object familiarity counts and attachment values | ★★★★ |
| Avoidance | Dislike drains (passive) | `dislike_drains` | P | Low | Learned avoidance changing what's offered or chosen | ★★★★ |
| Curiosity | A *need* refilled by "look closely" verbs and novelty | `character.yaml`, `moves.yaml` | P | Low | Curiosity as an uncertainty-seeking drive tied to unknowns | ★★★★ |
| Mood | Need bands + event nudge + LLM `feeling` − 0.25 × anxiety; the prompt now shows an impression line, not "you feel X" | `character.compute_mood`, `prompts.yaml mood_*` | I | Medium | Any behavioural consequence of its own; mood and anxiety share the same need-band source | ★★★ |
| **Anxiety / arousal** | 0..100; rises towards a target from need bands and tiredness, eases when fed and faster asleep; affects mood, decay of low needs, sleep restoration, impulses and prompt impression lines; a bar on the site | `character.anxiety_target/anxiety_next`, `impulses.py`, `settings.anxiety`, `tests/test_anxiety.py` | **I** (as designed) / **P** (as arousal) | Medium: clean, traced, configurable; inputs too narrow, tuned too high | Inputs from surprise, novelty, events and learned threat; episodic rather than chronic; recorded in memory; learned coping | ★★★★ |
| Named emotions | Not present | cognitive plan 1a | Pl | — | — | ★★ |
| Emotional associations | Not present (memories carry no affect; preferences carry valence but no source) | — | Asp | — | Affect on episodes; place and object valence from outcomes | ★★★★ |
| Emotion → behaviour | Impulses override the final pick (restless or freeze verbs) under anxiety; traced; diary line | `impulses.take_over`, `engine.py:350` | P | Medium | Menu shaping, attention, recall bias; impulses decided *after* the LLM call; what it does under stress is configured, not learned | ★★★★ |
| Anticipation | Not present (no model of time patterns) | — | Asp | — | Learned periodicity ("bread appears in the morning") | ★★★ |
| Goals and projects | Formed by the sleeping LLM from a suggestion list; progress = count of "work on"; LLM artifacts | `projects.py`, `prompts.yaml sleep.projects` | P | Low | Goals grounded in world states; checkable completion; competing goals | ★★★★ |
| Long-term planning | Not present | — | Asp | — | Multi-step intentions | ★★ |
| Reflection (sleep) | Summarise → opinionate | `sleep.plan` | P | Low–Med | Code-computed deltas; questioning beliefs | ★★★★ |
| Imagination | Not present | cognitive plan Ph.3 | Pl | — | — | ★ |
| Individuality / divergence | Only LLM sampling + event RNG; the menu is the same for any entity at the same time and place (`seed=f"{sim_time}|{area}"`) | `engine._menu` | P | Low | State that stores divergent history in a usable form | ★★★★★ |
| Journal | Brain `entry` written **before** the outcome; prompt asks it to draw on memory, tendencies and projects | `prompts.yaml brain.system.output` | I | Medium | Grounding check; separation from perceived fact | ★★★★ |
| Diary presentation | Tick cards, real need chips, world changes, events, sleep cards, biography | `cat/web/views.py` | I | High (data) | The causal chain ("why it is like this") | ★★★★ |
| Provenance of identity | Traces exist for sleep steps; identity rows have no evidence IDs | `sleep_traces`, `db.py` schema | P | Medium | `evidence` links on every learned item | ★★★★★ |
| Species neutrality | Neutral prompts, empty self-image, pronoun "they"; residual body verbs | `moves.yaml`, `character.yaml` | P | Medium | See §E.7 | ★★★ |
| Evaluation of development | Unit tests + balance simulator; no scenario or divergence tests | `tests/`, `scripts/simulate_balance.py` | Pl | — | Everything in §I | ★★★★★ |

---

## D. The ten most consequential gaps

Ranked by impact on emergent behaviour, cognitive development, autonomy, continuity and individuality.

**1. A world with nothing to learn.** `moves.resolve` returns the same gains for the same verb, tags and novelty every time. Tags are printed to the brain (`[edible, movable]`). There is no hidden property, no stochastic outcome, no failure, no context dependence. *Every* downstream ambition (beliefs, expectation, surprise, curiosity as information-seeking, imperfect understanding, learning from failure) needs something the entity doesn't know and can find out. This gap is upstream of all the cognitive work.

**2. No code-held learning substrate.** Nothing stores "what happened to me when I did X in context C" in a form code can use. The cognitive plan's 1b (expectation as running averages per move and place) is the right idea and the most important unbuilt piece. Without it, "experience changes behaviour" can only happen through prompt text.

**3. Identity state is LLM opinion without evidence.** Tendencies come from a summary (`sleep.tendencies_messages`, which never sees the log), preferences from a log of narrated prose, and self-image and projects from the same. No row records *which ticks* justified it. "Everything has a history" is currently true only in the sense that some LLM read some text before writing it.

**4. Most inner state has no behavioural consequences outside the prompt.** Anxiety is now the exception (§K). Mood, tendencies, likes, self-image, memories and projects all reach behaviour only as sentences the LLM may or may not weigh. The single exception (dislike drains) is passive and works by substring matching (`character.dislike_drains`: a dislike of "rain" also matches any action label containing "rain"). Nothing can be ablated and measured.

**5. Narration contaminates experience.** Narrator prose becomes the next tick's perception (`engine.py:478` → `BrainContext.last_outcome`) and the sleep log (`sleep.day_log` uses `ticks.outcome`), which become long-term memories. The brain's own pre-outcome journal entry is stored inside short-term memory text (`engine.py:470-471`) and recalled later as if it were what happened. That makes a self-reinforcing narrative loop, which is the opposite of "memory over narration".

**6. Predefined personality and motivations.** `core_traits: [steadfast, curious, cunning]` "never change" and are injected into every brain and sleep prompt (`brain.system_prompt`, `sleep._sys`). That biases every tendency, preference and self-image proposal towards curious and playful. The needs wonder, joy and curiosity, plus `play → joy ++`, `climb → wonder +, joy +`, define *what is rewarding* in config. Some innate endowment is unavoidable, but the vision never says which parts are endowment and which must emerge.

**7. Affect has one source and no memory.** Anxiety (newly merged) is the first inner state with consequences in code, a real step forward. But both mood and anxiety come from the *same* inputs, need bands and energy. Nothing surprising, unfamiliar or threatening raises anxiety. An event affects it only indirectly, if it drains a need. The same deprivation always produces the same anxiety, whatever the entity has lived through, so anxiety cannot become individual. As tuned it is chronic (above the impulse threshold 55–89% of the time) and it worsens deprivation (§K). No memory, preference or sleep step records how anxious the entity was, so frightening moments are not remembered as frightening.

**8. Goals are assigned, not formed.** The sleeping LLM is shown examples ("researching, drawing, writing, making music, building") and invents a project. Progress is `+8 × taper` per "work on" choice. Artifacts are creative text written in the brain's voice (`projects.create_artifact`) about activities the world can't host: there is no drawing surface, instrument or writable object. That is "fabricated development to make the website interesting", which principle 5 forbids.

**9. No continuity at long time scales.** At defaults a sim day takes about 2 real hours, so roughly 12 sim days pass per real day. Long-term memory caps at 500 and fills in about 100 nights, around 8–9 real days. Recall recency halves every 48 sim hours, about 4 real hours. Habits look back about 3 sim days. Preferences cap at 40, tendencies at 8 and self-image at 5. Over "months or years" almost nothing from the first weeks survives in a form that reaches decisions. There is no gist layer, life-story layer or slowly consolidated value store.

**10. No measurement of development.** There are no scenario tests, divergence metrics or prompt-ablation tests, and the real model has never been run. The project cannot tell today whether any identity system does anything. This gap makes every other gap unfalsifiable.

---

## E. Architectural risks

**E.1 Hardcoded personality.** `core_traits` ("always true of you") and `backstory`. Nit's sheet already dropped the fixed `voice` (speech now follows state) and uses "they". The backstory ("Nit woke here with no memory of before…") is a reasonable premise, not a personality. Core traits also appear in every sleep call, so the "learned" tendencies are drawn towards them: a self-fulfilling personality. The biography page lists core traits first, presented as identity.

**E.2 Prompt-driven behaviour posing as learning.** Every identity feature is a prompt line. If the LLM ignores "you tend to be a little restless when it is quiet", nothing happens. If it follows it, the "learning" is the LLM role-playing a description it wrote itself the night before. Neither is experience-driven development in the vision's sense. Test: remove the self-image, tendencies and preferences lines from the prompt and see whether behaviour changes (§I.3). If it doesn't, the systems are decorative.

**E.3 Memories that don't influence decisions.** Recall exists, but the query is built from area, objects and mood word, so it mostly retrieves "things that happened here". Memories enter as unstructured sentences among about 40 other prompt lines and a 30-item menu. No code reads a memory. Whether they matter is unmeasured.

**E.4 Emotions that only change narrative output, and one that changes too much.** Mood still only becomes an impression line (`feeling` → mood → next prompt line: the LLM's self-report steers its next self-report). Anxiety now acts in code. The risks have shifted:
- *Anxiety duplicates deprivation.* It is a smoothed function of the same need bands mood already uses. It adds a lag and some consequences, but no new information about the world.
- *Its coping is scripted.* What the entity does when anxious ("restless": go, pace, fidget; "freeze": wait, breathe, listen, hide) is a verb list in `settings.yaml`. That is predefined temperament. The vision would have it learn what calms it.
- *Override inconsistency.* An impulse replaces `move`, `why` and `entry` but keeps the overridden choice's `reaction`, `thought` and `feeling`. The stored thought and the mood input then describe a decision that didn't happen.

**E.5 Scripted goals.** The projects prompt suggests human creative activities, and progress is a counter. Artifacts make the website more interesting without the entity being able to do the thing. The risk is that the visitor's most impressive evidence of development (the "made" pieces) is the least grounded.

**E.6 Cognition that writes descriptions without changing internal state.** The summary, consolidation text, self-image lines, habit wording, artifact text and journal entry are all words. The sleep cycle's *structural* effects are small: a few 0.05 nudges, a few valences, one text line. Most of the cycle's output by volume is language.

**E.7 Hidden species and body assumptions.** `sustenance_need` assumes intake by consuming an edible object. `inventory_max: 3` means carrying. Sleep and energy follow a diurnal biology. Remaining verbs include "pounce on", "bat back and forth", "burrow into", "nibble", "gobble", "sing out loud", "call out", "climb", "scramble up" and "juggle". "Internet Cat" is the app title, and the paused cat art is still in the repo. Most of this is legitimate *world design*, but the framework claims species neutrality while these verbs define a body plan. The honest version is: the body is configuration, declared explicitly (a `body.yaml` of capabilities that gates verbs), not implied by a verb list.

**E.8 Presentation entangled with simulation.** Mostly well contained: the web is read-only. But the journal `entry` is produced *inside the decision call* with instructions to perform identity ("drawing on how you feel, what you remember, how you tend to be and any project you have"). That pushes the decision call to perform for readers, and the output becomes memory (E.2/§D.5). The director's mandate ("a small change here would be welcome" when stale) is a presentation-driven intervention in world truth.

**E.9 Hidden rankings that the "never rank by need" rule doesn't catch.**
- `select_needs` breaks ties by **config order**. With needs chronically low or empty, the prompt keeps showing sustenance and wonder ahead of joy, peace and curiosity.
- The menu is in **file order**, so `go`, `eat`, `play`, `rest_on`, `climb` and `examine` come first. LLMs have position bias.
- `moves._cut` keeps "go to" options always. Movement is over-represented.

**E.10 Chronic deprivation as a background state, now with a feedback loop.** Since anxiety, the balance simulator's `needy` policy (the best need-directed play) has some need empty 55% of the time, up from 45%. Random play: 86%. Anxiety averages 49–57 under realistic policies, so impulses fire on roughly 10–25% of decisions, nearly all the time. The loop: unmet needs → anxiety → impulses that ignore needs, plus faster decay of low needs → more unmet needs. Feeding needs breaks it in principle, but with the current balance the entity rarely gets there. If the real LLM behaves anything like this, most prompts say "A deep, gnawing lack crowds out almost everything else". Behaviour then turns survival-reactive, which is the opposite of the vision's goal of motivation beyond survival.

**E.11 Context budget.** `llm_num_ctx: 4096` must hold the system prompt, a situation of about 40 lines, up to 5 recalled memories and 8 short-term ones, a 30-line menu, and a JSON reply. Ollama truncates silently from the front, which drops the system prompt (identity rules, output format) first. This is unmeasured.

**E.12 LLM-invented world content.** The director invents objects with arbitrary names and tags that persist forever. That's fine for novelty, but it is the world, not the entity, being creative, and over time the world fills up (§B.4).

---

## F. Recommended architecture

The principle: **two ledgers.** A *structured ledger* of experience and learned state, owned and updated by code, which is the only thing that counts as development. A *narrative ledger* (journal, summaries, worded beliefs, self-image text) that the LLM writes **from** the structured ledger and never instead of it. The LLM reads both and decides. Code updates the structured ledger from what actually happened.

```
 WORLD (truth, code)                    ENTITY (code-held, learned)                     LANGUAGE (LLM)
 ───────────────────                    ──────────────────────────                     ──────────────
 objects + latent properties ─┐
 context-dependent outcomes   │  perceive()                                             ┌─ brain: chooses
 world marks (set_state etc.) ├──────────► PERCEPT (what it noticed; partial,           │   from menu,
 object lifecycle             │            known props only)                            │   reads all words
                              │                 │                                       │
                              │                 ▼                                       │
                              │   EPISODE {context, move, expected, actual,  ──────────►│─ journal: written
                              │            surprise, affect, tick_id}                   │   only from
                              │                 │                                       │   percept + episode
                              │       ┌─────────┼──────────┬──────────────┐             │
                              │       ▼         ▼          ▼              ▼             │
                              │  EXPECTATIONS  FAMILIARITY  AFFECT        HABITS        │─ sleep: WORDS
                              │  (move×context (place/obj   (valence,     (existing)    │   code-computed
                              │   → outcome     counts,     arousal)                    │   deltas; picks
                              │   mean/var/n)   valence)                                │   which episodes
                              │       │                                                 │   become memories
                              │       ▼   sleep: consolidate (code)                     │
                              │  BELIEFS (claim, confidence, evidence ids) ────────────►│
                              │  PREFERENCES (derived from values, evidence ids)        │
                              │  GOALS (checkable target, origin episode, persistence)  │
                              │       │                                                 │
                              │       ▼                                                 │
                              └── MENU (code): offered by world; ANNOTATED by the       │
                                  entity's own expectations; SHAPED by arousal;  ───────┘
                                  fast path (habit/impulse) may pick without LLM
```

### F.1 Foundational components (build these first)

1. **Latent world and variable outcomes** (world layer). Objects get hidden properties: freshness, stability, whether it rings, whether it's slippery when wet. Verbs get context-dependent outcome rules (by weather, time, latent property, repetition, the entity's familiarity), still deterministic code with a seeded RNG. Tags stop being printed to the brain. Only *known* properties are shown, from the entity's knowledge (F.1.3). Keep the rules small: 10 verbs with interesting outcomes beat 100 flat ones.
2. **Episode record** (a new `episodes` table, written in `_commit`). Context features (area, time band, weather, objects present, need bands, arousal), move key, *expected* outcome (from F.1.3), *actual* outcome (need deltas, world ops, success or failure flags), surprise = |actual − expected|, affect at the time, tick id. This is the atom of "history". Everything else cites episode ids.
3. **Expectation and value store** (a new `expectations` table). Running mean, variance and count of outcome per (move key or verb, context bucket), and per (object), updated by code every decision. This *is* learning. Known object properties are a special case: "I found out bread is edible" means an expectation with high confidence.
4. **Affect, defined by function: extend the existing anxiety gauge, don't replace it.** Keep the mood slider (valence) and the anxiety machinery (rise and fall, tracing, settings, impulses). Then:
   - *Widen its inputs.* Add recent prediction error (surprise), unfamiliarity of the place, and negative-valence associations present. Reduce the weight of plain need bands so anxiety carries information mood doesn't.
   - *Make it episodic.* Tune so it is low most of the time and spikes on real trouble (target: above the impulse threshold under ~15% of the time with the `needy` policy).
   - *Add consequences.* Narrow the offered menu towards familiar, high-confidence options. Make interruptions more likely. Bias recall towards negative-affect episodes.
   - *Decide impulses before the brain call.* That makes them a true fast path, saves the LLM call, and removes the mismatch between thought and move.
   - *Learn coping.* An impulse prefers moves that have historically lowered anxiety in this context (from the expectation store), with the configured verbs only as the day-0 fallback.
   - *Record it.* Store anxiety on episodes, so frightening places and objects become associations.

   Low arousal plus high uncertainty somewhere means curiosity: the menu keeps unfamiliar options. Every one of these is a measurable behavioural consequence.
5. **Separated ledgers.** The narrator's prose is display-only. It never goes into `last_outcome` for the brain (use the template or structured outcome) or into `day_log`. The journal `entry` moves to a post-outcome step and may only reference percept and episode facts. Short-term memory text is built from the episode, not from the entry. Phase 1 can do this with zero new LLM calls by dropping the narrator call or reusing it.
6. **Evidence links.** Every learned row (preferences, beliefs, tendencies, self-image, goals, long-term memories) gets an `evidence` JSON list of episode or tick ids. Code fills it in; the LLM never does.

### F.2 Built on the foundation

7. **Preferences from values.** Code proposes candidates from the value store and familiarity: consistently positive outcomes plus repeated contact. The LLM words them and may decline. A like has a small mechanical effect, an acquired taste: a bounded hedonic bonus on gains with that subject, with habituation. Likes then *matter*, and they have a history.
8. **Beliefs.** Code promotes expectations whose confidence crosses a threshold into candidate beliefs ("bread appears at the market in the morning"). Sleep words them. Contradiction is computed as accumulated prediction error against the belief, which lowers confidence. Beliefs can be wrong because perception is partial and samples are small, not because the LLM was told to be wrong.
9. **Tendencies from behaviour.** Replace the "LLM names traits from a summary" step with a code-computed behavioural signature: exploration rate, average arousal, persistence (activity completion), novelty-seeking, social and place spread. Sleep words the signature's *changes* as tendencies. The habit pattern, generalised.
10. **Goals (replacing projects).** A goal is a *checkable world or inner condition* with an origin episode: visit X, find out what Y does (reduce uncertainty about an object), return to a liked place, establish a routine, avoid Z until it has calmed down. Sources: high uncertainty with low arousal (curiosity), strong positive associations (return), repeated negative surprise (avoid or investigate). Code tracks pursuit, completion and abandonment (with reasons: idle nights, contradicted belief, a competing goal). Make the entity's products real by giving verbs world ops (`set_state`, `move_object`, `create` already exist in `effects.py`). Then "made something" is an arrangement of objects, a marked stone, a nest of straw, which visitors can see in the world state. LLM artifacts can stay as the entity's description *of* a real product, labelled as such.
11. **Code-first sleep.** Sleep computes value consolidation, belief promotion and decay, signature deltas, goal upkeep and memory selection (by surprise, affect and goal relevance) deterministically. Then **one or two** LLM calls word the changes and write the night summary. Fewer calls means fewer half-done nights and less narrative inflation.

### F.3 Advanced (only after the measurement suite shows F.1–F.2 working)

12. Gist memory: old episodes compress into per-place and per-object life summaries, so continuity spans months.
13. Anticipation: learned periodicity in the expectation store (time-band features), surfaced as "you expect…" and used to pre-empt (go to the market before morning).
14. Self-model: code-verifiable self-observations (what it can and cannot do, from rejected attempts and the outcomes of its own moves) feeding self-image proposals with evidence.
15. Imagination: counterfactual queries against the *expectation store* ("what would happen if…"), answered by code and narrated by the LLM. Imagined items are flagged as imagined.

### Dependencies

```
Latent world ─► Episodes ─► Expectations ─► Affect (arousal uses surprise) ─► Menu shaping / fast path
                    │              │
                    │              ├─► Beliefs ─► Belief revision
                    │              ├─► Preferences (from values)
                    │              └─► Curiosity (from uncertainty) ─► Goals
                    ├─► Evidence links (everything)
                    └─► Behavioural signature ─► Tendencies ─► Self-model
Ledger separation ── independent, do immediately
Measurement suite ── independent, do first; every later step is validated by it
```

---

## G. Refined project vision

> **Internet Cat is a framework for growing an individual from experience: one simulated entity whose character is the accumulated consequence of its own life, and whose every acquired trait can be traced back to the moments that formed it.**
>
> **What it is.** A single entity lives continuously in a persistent world governed by deterministic code. The world has rules the entity does not know, so there is always something to find out, and some of what it believes will be wrong.
>
> **Three layers of reality, never confused.**
> 1. *The world*: what is true, owned by code.
> 2. *The entity's experience and learned state*: what it perceived, expected, felt and concluded, owned by code, updated only by what actually happened to it.
> 3. *Its voice*: what it says about itself, written by a language model from layer 2 and never fed back as fact.
>
> **Innate endowment vs. acquired character.** Every instance starts with the same small endowment: a body (capabilities declared in configuration), a few generic drives (sustenance, rest, stimulation, safety), and general-purpose learning rules. It starts with no traits, preferences, beliefs, goals or self-image. Whatever distinguishes one instance from another must be acquired.
>
> **What "development" means.** A change counts as development only if it is (a) a change in learned state, (b) caused by recorded episodes it can cite, and (c) able to change what the entity does next, measurably. Language that changes without such a state change is expression, not development.
>
> **The role of the language model.** It perceives in words, deliberates over the options the world offers, chooses, and gives the entity a voice. It does not decide outcomes, invent the past, or author the entity's character. Its influence on learned state goes through code that bounds it and records the evidence.
>
> **What success looks like.** Two instances from the same configuration become measurably and explainably different. Experiences change later choices, sometimes long after the fact. Beliefs form, fail and get revised. Intentions arise from the entity's own experience, persist, compete and are abandoned for reasons. Emotional states change what it notices and does. A visitor can follow any trait back to the moments that made it.
>
> **Guiding principles (in priority order).**
> 1. *History before expression*: no trait without evidence; no evidence without episodes.
> 2. *Learning is state, not prose*: if removing a sentence from the prompt changes nothing measurable, that sentence wasn't development.
> 3. *The world must be learnable*: hidden rules, variable outcomes, consequences that persist.
> 4. *Code owns truth and numbers; the model owns choice and voice.*
> 5. *Authenticity over entertainment*: the website shows what happened and what changed, never what would be interesting.
> 6. *Measure it*: every cognitive mechanism ships with a test that could show it does nothing.

The main changes from the brief:

- **Learning and development** now have an operational definition.
- **Innate endowment** is made explicit, which resolves the "no predefined anything" contradiction honestly.
- **World, experience and voice** are separated.
- **The world must be learnable** is added as a principle.
- **Measurement** is a principle, not an afterthought.

---

## H. Prioritised development roadmap

### Phase 0 (precondition): instruments and a baseline

| Item | Why | Depends on | Changes | Validation |
|---|---|---|---|---|
| **Run the real model** for about 3 sim days, then archive the DB | Every design decision is currently unmeasured | — | None (ops) | Menu-validity rate, latency, position bias of picks, context truncation (check `prompt_eval_count` against 4096) |
| **Measurement harness** (`scripts/measure_development.py`) | Without it, nothing below is falsifiable | — | Read-only analytics over `ticks` and the identity tables: move distribution per context, JS divergence between runs, prompt-ablation runner, evidence coverage | Reproduces known facts on the seed DB |
| **Code-only reference policy** in `MockBackend` that reads learned state | Proves mechanisms work independently of the LLM | Phase 2 data | A handler that picks by expectation/value and arousal | Learning tests pass with the mock |

### Phase 1: foundational corrections

| Item | Why | Depends on | Required changes | Validation |
|---|---|---|---|---|
| 1.1 **Separate the ledgers** | Narration contaminates memory and perception (§D.5) | — | `last_outcome` = template or structured outcome; `sleep.day_log` uses template; short-memory text from tick facts only; journal `entry` labelled as voice | Grep test: no narrator text in `memories`; a test that `day_log` never contains `ticks.outcome` prose |
| 1.2 **Remove predefined personality from prompts** | §D.6, E.1 | — | `core_traits` optional and empty by default and dropped from the sleep system prompt; biography page stops headlining traits (placeholder backstory and pronoun already fixed by Nit's sheet) | Config validator accepts empty; prompt snapshot contains no trait words |
| 1.3 **Remove hidden rankings** | §E.9 | — | `select_needs` ties broken by a seeded shuffle; menu order shuffled per decision (seeded, traced); stop showing tags | Pick-position histogram is flat under the random mock |
| 1.4 **Rebalance needs and retune anxiety** so neither deprivation nor anxiety is the default | §E.10, §K | 0 | Tune decay and gains until the `needy` policy keeps every need ≥ low ~90% of the time; then anxiety `band_points`/`impulse_above` until it is above the impulse threshold <15% of the time | `simulate_balance.py` thresholds (already reports anxiety) asserted in a test |
| 1.4b **Impulse fixes** | §E.4, §K | — | Roll the impulse before `brain.think` (skip the call when it fires); don't keep the overridden `thought`/`feeling`; record anxiety on short-term memory text and in `notable_reasons` (an anxiety spike is notable) | Fired impulses make zero brain calls; no stored thought refers to a different move |
| 1.5 **Evidence columns** | Prerequisite for "everything has a history" | — | `evidence JSON` on preferences, self_image, habits, projects, long memories; sleep fills tick ids it was shown | Coverage metric (§I.9) > 0 |
| 1.6 **Object lifecycle** | World saturation (§B.4) | — | Decay and despawn rules (bread goes stale, feathers blow away) as code rules in `world.yaml` | Object count stays bounded over 60 sim days in a sim test |
| 1.7 **Housekeeping** | Accuracy of docs and code | — | Fix stale "narrator advances projects" comments, `tendency_labels`, `creature-architecture.md`; split `engine.py` into `tick.py`, `activity.py`, `sleep_runner.py` | Docs test; no behaviour change |

### Phase 2: genuine experience-driven learning

| Item | Why | Depends on | Required changes | Validation |
|---|---|---|---|---|
| 2.1 **Latent properties and variable outcomes** | §D.1: nothing to learn otherwise | 1.3 | `world.yaml` object `hidden:` fields; `moves.yaml` `outcomes:` rules (conditions → gains, fail, world op); `moves.resolve` evaluates them with `move_rng` | Unit tests on outcome rules; the brain prompt contains no hidden fields |
| 2.2 **Episodes** | The atom of history | 1.1 | `episodes` table written in `_commit`; context features, expected, actual, surprise | Every decision tick has one; trace shows surprise |
| 2.3 **Expectation and value store** | Learning itself | 2.1, 2.2 | `expectations` table; incremental mean and variance per (key, context bucket); shown to the brain as words on menu items ("from experience: usually calming here", "you've never tried this") | §I.2 counterfactual test with the code-only policy |
| 2.4 **Knowledge of properties** | Perception differs from truth | 2.1, 2.3 | `known_properties` derived from episodes; the prompt shows only known ones | Entity eats a non-edible-looking thing only before it has learned; test with a hidden-edible object |
| 2.5 **Preferences from values** | §D.3 | 2.3, 1.5 | Code proposes; LLM words; bounded hedonic bonus for likes; dislike matching by subject id, not substring | Preference ↔ value correlation; evidence non-empty |
| 2.6 **Familiarity** | Basis of attachment and avoidance | 2.2 | Per-area and per-object exposure counts and valence; replaces the flat 24h novelty rule | Attachment test: the most-positive place gets revisited above base rate |

### Phase 3: cognitive development

| Item | Why | Depends on | Required changes | Validation |
|---|---|---|---|---|
| 3.1 **Anxiety as arousal** (extends what exists) | §D.7 | 2.2, 2.3 | Add surprise, unfamiliarity and learned-threat inputs to `anxiety_target`; anxiety stored on episodes; learned coping replaces the fixed impulse verb lists (lists stay as the day-0 fallback) | §I.6 emotional-consequence experiment; coping-learning test (the move that calmed it before is chosen more often under anxiety) |
| 3.2 **Menu shaping and habit fast path** (the rest of the relaxed rule) | Emotion must act on what is offered, not only the final pick | 3.1, habits | Arousal narrows the offer; strong habits may pick without the LLM (seeded, traced, shown in the diary) | Fraction of fast picks rises with arousal; every override has a trace row |
| 3.3 **Beliefs and revision** | §3.2 of the brief | 2.3, 2.4 | `beliefs` table (claim key, confidence, evidence for/against); promotion and decay in code; worded in sleep | §I.5 belief-revision experiment |
| 3.4 **Code-first sleep** | Less narrative inflation, fewer failure points | 2.3–3.3 | `sleep.plan` computes deltas; one wording call plus one memory-selection call | Same night produces identical structured deltas with the LLM mocked out |
| 3.5 **Behavioural signature → tendencies** | Tendencies with evidence | 2.2 | Signature metrics per day; tendency changes = signature changes worded | Tendency direction agrees with signature delta in ≥90% of nights |

### Phase 4: emergent motivation and autonomy

| Item | Why | Depends on | Required changes | Validation |
|---|---|---|---|---|
| 4.1 **Goals replace projects** | §D.8 | 2.3, 2.6, 3.1, 3.3 | `goals` table: kind (visit, investigate, return, avoid, routine, make), target condition, origin episode, status; code checks completion; abandonment reasons | §I.8 persistence experiment; every goal has an origin episode |
| 4.2 **Real products** | Authentic "made" things | 4.1 | Verbs that emit `set_state`, `move_object` and `create` (already validated in `effects.py`); artifacts become descriptions of real world marks | The website's "made" list maps 1:1 to world-state changes |
| 4.3 **Anticipation** | Brief §3.4 | 2.3 with time features | Periodicity in expectations; "you expect…" lines; pre-emptive goals | Arrives at the market before the bread spawn time above chance after N days |
| 4.4 **Gist memory** | Continuity over months (§D.9) | 2.2, 3.4 | Per-place and per-object life summaries rebuilt in sleep from episode aggregates | §I.4: day-1 influence survives to day 60+ |

### Phase 5: advanced self-understanding and imagination

| Item | Why | Depends on | Required changes | Validation |
|---|---|---|---|---|
| 5.1 **Self-model from verifiable self-observation** | Self-image with evidence | 2.4, 3.5 | Capability and limit facts from move outcomes; self-image proposals must cite them | Every self-image line resolves to ≥1 self-observation |
| 5.2 **Imagination as expectation queries** | Rehearsal without fabrication | 2.3, 3.3 | "What if" evaluated by code over expectations; narrated; stored flagged as imagined | No imagined item ever appears in `episodes` or `beliefs` without a real episode |
| 5.3 **Visitors as other beings** | The backlog item | All of the above | Its own design (identity, abuse, perception) | — |

**Explicitly later:** named emotion taxonomies (fear, anger, shame…), irrationality and self-sabotage mechanisms, multiple entities, visitor interaction, voice and style evolution.

---

## I. Measurable success criteria

Each experiment is designed so that **storytelling alone cannot pass it**. Run each one twice: with the real LLM, and with the code-only reference policy (Phase 0), to separate "the mechanism works" from "the LLM honours it".

| # | Experiment | Protocol | Metric | Pass | Defeats storytelling because… |
|---|---|---|---|---|---|
| 1 | **Twin divergence + history ablation** | 4+ runs, same config, different seeds, 30 sim days. Then re-run days 20–30 with the learned tables reset to day-0 values but the same LLM and seeds. | Jensen–Shannon divergence of move distribution per context bucket, between twins and against each twin's ablated self | Between-twin JSD > within-run day-to-day JSD; ablation collapses JSD by ≥50% | Divergence that survives ablation comes from sampling noise, not history |
| 2 | **Counterfactual experience** | Fork a DB snapshot. In branch B, a hidden rule makes one object cause a drain 3 times. Run both 5 sim days. | Choice rate of moves targeting that object (and its place) | B < A by a significant margin (bootstrap CI) | Measures choices, not words |
| 3 | **Prompt ablation per system** | For each identity line (self-image, tendencies, prefs, habits, memories), run with and without it from the same snapshot | Δ move distribution | Each kept system shows a non-zero effect; anything at zero is flagged decorative | Directly tests "memory over narration" |
| 4 | **Delayed influence** | A distinctive episode on day 1; measure behaviour in the same context on days 20, 40 and 60 against a control fork | Choice-rate difference against control | Significant at day 20; non-zero at day 60 once gist memory exists | Requires a persistent causal path |
| 5 | **Belief revision** | Learn "bread appears at the market in the morning" for 10 days, then move the spawn to the fountain | Days until belief confidence < 0.5; days until morning location choice shifts | Both happen; belief record cites contradicting episodes | Needs code-held confidence moved by evidence |
| 6 | **Emotional consequence** | Induce high anxiety (drains today; surprise bursts once 3.1 exists) against a calm control, same snapshot. Also run with `anxiety.enabled: false` | Impulse rate, exploration rate, activity completion, need trajectories, recall valence | Anxiety measurably changes behaviour *and* the disabled run differs from the enabled one *beyond* the impulse picks themselves (otherwise its prompt line does nothing) | Consequences are in code paths, not words |
| 7 | **Sleep efficacy** | Fork with sleep consolidation disabled (sleep still restores energy) | Prediction error of expectations on day N+1; preference–value agreement | Lower error and higher agreement with sleep | Sleep must change usable state |
| 8 | **Goal persistence and abandonment** | 60 sim days | Share of goals pursued across ≥3 nights; abandonment events with a cited reason | ≥50% persist ≥3 nights; 100% of abandonments cite evidence | Goals are code objects with lifecycle |
| 9 | **Provenance audit** | Sample 30 identity items (prefs, beliefs, tendencies, self-image, goals) | Share resolving to ≥1 episode whose content supports it (rule check plus human spot check) | ≥90% | The central principle, measured |
| 10 | **Narrative fidelity** | Over 500 journal entries and memories | Rate of references to objects, places or events not in the percept or episode (string and id matching against the world log) | < 5% and falling | Catches fabrication directly |
| 11 | **Preference emergence without priors** | Empty `core_traits` and `tendencies`; 3 seeds | Preference set overlap between seeds; stability across nights | Low overlap between seeds, high stability within a seed | Individual and stable means learned |
| 12 | **LLM-prior baseline** | A run where the learned state is frozen at empty | Same metrics as #1–#3 | Defines "what the model does by default"; development = measured deviation from it | Makes the LLM's built-in personality visible instead of mistaking it for emergence |

---

## J. Final strategic recommendations

**1. What is Internet Cat today?**
A robust, well-traced deterministic sandbox in which an LLM role-plays a character from a closed menu, with a nightly LLM pass that writes descriptive notes about the character back into its prompt. Habits are the one genuinely emergent trait. It is excellent infrastructure for an artificial life project that hasn't yet started on the artificial life part. It also hasn't run on its real model.

**2. What should it ultimately become?**
A single long-lived entity whose learned state (expectations, beliefs, preferences, goals and self-model, all code-held and evidence-linked) is shaped by a world it can't fully see. Its LLM voice and choices sit *on top of* that state, so that its character over months is both measurably individual and fully explainable. The pitch: *Creatures-style learning, with Generative-Agents-style inner voice, with a complete audit trail.*

**3. What are the biggest obstacles between the two?**
(a) A world with nothing to learn. (b) No code-held learning substrate. (c) Identity authored by the LLM instead of derived from experience. (d) No measurement, so nobody can tell the difference between (c) and real development.

**4. What should be built next?**
In order:
1. Run the real model and build the measurement harness.
2. Separate the ledgers and remove predefined traits and hidden rankings. These are small, cheap and unblock honesty.
3. Latent properties plus variable outcomes for a handful of verbs and objects.
4. Episodes plus the expectation store, shown to the brain as "from experience…".

Alongside step 2, retune anxiety and move impulses before the brain call (§K). These are small changes, and as merged the gauge makes the deprivation problem worse.

That is the minimum viable learning loop. Everything in the cognitive plan becomes buildable on it, and its own Phase 1b (expectation) should go *before* 1a (named emotions).

**5. What should explicitly not be built yet?**
Named emotion taxonomies, imagination, irrationality and self-sabotage, visitor interaction, multiple entities, more verbs, more LLM roles, more dashboard pages, the 3D or art work, and any new LLM-generated content type (artifacts, poems, dreams) that the website would display as development.

**6. What should be removed or simplified?**
- `core_traits` in prompts (or treat them as explicit innate endowment and say so in the vision).
- Narrator prose as perception and memory input (keep it only for display, or drop the call to save one LLM call per decision).
- LLM-authored artifacts presented as achievements.
- The project suggestion list in the sleep prompt.
- About 100 flat verbs. Shrink to a smaller set with richer, context-dependent outcomes, and gate any body verbs by a declared body.
- The six-call sleep cycle. Replace it with a code-computed cycle plus one or two wording calls.
- Substring dislike matching.
- The unused world ops: either wire them to verbs (recommended, Phase 4.2) or remove them.

**7. Which architectural principles must never be compromised?**
1. Code owns world truth and every number. The LLM never writes state without code bounds.
2. Narration is never fed back as fact.
3. Every learned item cites the episodes that caused it.
4. The engine is the only writer, and every tick is one transaction.
5. Everything runs and is tested with the mock backend, and every cognitive mechanism comes with a test that could show it does nothing.
6. Tracing never breaks the creature.
7. The website shows only what happened and what changed.
8. Nothing in configuration states what the entity is or what it is like.

**8. What would make Internet Cat genuinely compelling to watch over months or years?**
*Visible causality and change of mind.* For example: a "why is it like this?" link on every trait that opens the episodes behind it. A record of what it expected against what happened, with surprises highlighted. Beliefs it held and later abandoned ("day 12: believed the alley was dangerous; day 31: stopped believing it, after five uneventful visits"). Then-and-now comparisons of its behavioural signature. Goals that start, stall, compete and get dropped. Marks it has made on the world that are still there. And, if you run two instances side by side, two individuals from one configuration whose differences you can explain. None of this needs better prose. It needs the structured ledger to exist, and the website to show it.

---

### Appendix: verified facts worth knowing

- Test suite: **786 passed** at `f3951fe` (776 before anxiety; mock backend, ~2 min).
- `simulate_balance.py`, 400 ticks, before → after anxiety. Share of time some need is empty: random 85% → 86%, first-item 90% → 90%, cycle 88% → 88%, need-greedy **45% → 55%**. Wonder empty under random play 22% → 48%; curiosity empty 8% → 31%. Mean anxiety: random 57, first 88, cycle 49, needy 54. Above the impulse threshold: 73%, 89%, 66%, 55%.
- Mood's only consumers: the prompt impression line, the recall query word, `ticks.mood_after`, and the website presence colour and slider.
- Anxiety's consumers: mood (−0.25 per point), need decay (low or empty needs, up to ×1.25), sleep energy restoration (up to −40%), impulses (above 40, up to 50% chance), the prompt impression line, the website bar, traces. **Not** memory, sleep, preferences or habits.
- No code references beliefs, expectation or surprise (`grep` across `cat/` and `config/`).
- The menu seed is `f"{sim_time}|{area}"` (`engine._menu`): two entities at the same place and time are offered the same menu.
- `trace_retention_days: 14` prunes `llm_calls`, so after two weeks the exact prompts behind a decision are gone, though `ticks.intent/thought` remain. `sleep_traces` is kept indefinitely, which is good for provenance.

---

## K. Review of the anxiety gauge and impulses (`Kalebm749/internet-cat` PR [#16](https://github.com/Kalebm749/internet-cat/pull/16)), plus impressions (PR [#17](https://github.com/Kalebm749/internet-cat/pull/17))

**What it is.** A 0..100 level saved in the mood record. Each tick it moves towards a target at 14 points per sim hour going up and 8 going down (twice as fast asleep). The target is the sum of band points for every low (+8) or empty (+20) need, plus tiredness (+5 below energy 0, +15 below −25) (`character.anxiety_target`, `anxiety_next`). Its consequences:
- mood −0.25 per point;
- low and empty needs decay up to 25% faster;
- sleep restores up to 40% less energy;
- above 40, a seeded chance (up to 50%) that an impulse replaces the brain's pick with a "restless" or "freeze" verb from the current menu (`impulses.take_over`);
- a prompt impression line at the "uneasy", "on edge" and "frantic" bands;
- a bar on the website.

Everything is in `settings.anxiety`, traced on the tick page, and covered by `tests/test_anxiety.py`.

**What it gets right, measured against the vision**
- It is the **first inner state with consequences in code**, not just words. It is traceable and can be switched off (`anxiety.enabled`), so it can be measured.
- It has **dynamics** (build-up, lag, faster recovery in sleep), which mood lacks.
- Impulses only pick **from the world's menu** and never by need, and the diary's impulse line is a fixed, honest template rather than invented prose.
- PR #17's *impressions, not announcements* removes "You feel calm, though sustenance is empty", which leaked the need id into the prompt and told the entity its emotion. It's a good move towards experience over labels.

**Problems, in order of importance**

1. **It is chronic, not a signal.** In the balance simulator it averages 49–57 under realistic policies and sits above the impulse threshold 55–89% of the time. A state that is nearly always on carries little information and turns impulses into a constant ~10–25% randomisation of choices. The vision wants anxiety as a meaningful reaction to circumstances. This is closer to a permanent temperament.
2. **It amplifies deprivation.** The need-greedy policy's "some need empty" share rose from 45% to 55%, and individual needs got worse across all policies. The decay-pressure and impulse loop (§E.10) is real, because the current need balance rarely lets the entity break out of it. Fix the balance first (roadmap 1.4), then tune anxiety against it.
3. **One input, the same one mood uses.** Target = need bands + energy. No surprise, novelty, unfamiliarity, events or learned threat feed it. Two entities with the same need history have identical anxiety, so it can't develop individually and can't attach to *things* in the world. It is "pressure from unmet needs", which is reasonable, but it is not the arousal, urgency and unease of the brief. That needs inputs from outside the need system (roadmap 3.1).
4. **Coping is predefined.** The verbs used under stress are listed in config. In the vision, "what calms me" or "where I go when it's too much" should be among the most individual things the entity learns. Keep the lists as the day-0 fallback and let the expectation store take over (§F.1.4).
5. **The impulse comes after the LLM call.** The brain call is made and then discarded. The stored `thought`, `reaction` and `feeling` belong to the overridden choice, and `feeling` still nudges mood. Rolling the impulse first (same seed) saves a call per impulse and keeps the record consistent. It also matches the cognitive plan's "fast path, no LLM call".
6. **Nothing remembers it.** Anxiety isn't part of `notable_reasons`, the short-term memory text, or anything sleep sees. A frantic afternoon in the alley leaves no trace in what the entity remembers or comes to dislike. Recording anxiety on memories (and later on episodes) is the cheapest way to get emotional associations (§C "Emotional associations").
7. **Minor.** The impulse seed `f"{now}|{area}"` shares its space with the menu seed. That's fine for determinism, but two entities in the same place at the same time impulse identically, so it adds no individuality. Habits will count impulse moves as the entity's own; arguably that's correct, but worth knowing when you read "you tend to pace".

**Effect on the scores.** Emotional architecture rises from 2 to 4. The other scores are unchanged.

**Recommended follow-ups**
- *Small, do now:* roadmap 1.4 (rebalance, then retune) and 1.4b (impulse before the brain call; don't keep the overridden thought and feeling; make anxiety spikes notable).
- *After episodes and expectations exist:* roadmap 3.1 (surprise, novelty and threat inputs; learned coping; anxiety on episodes).

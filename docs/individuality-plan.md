# From one chatty creature to distinct individuals: map, design, and staged plan

Status: **proposal, not yet accepted.** `docs/rewrite-contracts.md` still wins until a stage below is accepted, built, and
folded into it ("as built" notes). This file is the working plan; it is checked against the code as of PR #43.

Pino (pinos.world) is used as inspiration only: the principles, not his numbers. His pages `/facts`, `/brain`, `/memories`
and the roadmap article were read on 21 Nov 2026 (section 2 summarises what they show). Memory sizes, thresholds and decay are
his; ours are tuned by measurement on this hardware and this world.

## 1. How it works today (traced, not assumed)

**One creature, one life.** `python -m cat.nit` opens one `data/nit.db` (`life.open_life`: migrate, spawn or load, sync world
facts and the author's part of the sheet). The world (facts, weather, mailbox, artworks) lives *inside* that database, so
there is no shared environment and nobody else in it. The character comes from `content/character.yaml` only; `--db` picks
another database but not another character.

**Identity.** `character_versions` holds each version of the sheet: `core` (nature, senses, boundaries; the creature never
changes it) and a mutable part (`traits` as name -> strength, free-text `tendencies`, `background`). Sleep stage 6 may make
one small checked change a night. **The sheet reaches behaviour only as words in the system prompt**
(`mind.identity_block` -> `decision.system_prompt`). No code reads a trait's strength: `grep` finds traits used for wording
and nowhere else. Personality has no effect on anything except what the model reads.

**Inner state.** Three variables (`body.py`): mood -1..1, energy, hunger. They update in code, not by model calls: time,
effort, hunger drift, action effects (numbers fixed in content, identical for any creature), a mood drag when hungry, a
mood nudge for repeated thoughts. Derived conditions (`restless`, `buoyant`...) become words in the prompt. Mood drifts
toward the same baseline (0.1) for everyone. Hunger alone can take over the move (spec §2.5).

**One decision (`loop.step`).**
1. `scheduler.due`: triggers are `activity_finished`, `event`, `mail`, `body`, `interval` (15 real seconds when idle) and
   `check_in` (30 sim minutes into an activity). Most activities are 5-30 sim minutes, so in practice **every activity ends in
   a full decision**: the benchmark shows 254 full, 2 interrupt, 0 check-ins in 256 decisions.
2. `mind.build`: where he is, what he perceived, body words, the last 5 activity texts, up to 5 recalled long-term memories
   (embedding query; scored recency + importance + relevance; recently recalled rest; one "wander" slot) plus today's notable
   moments, kept reading, open loops, a "mulling" flag, letters, and **every available action** (`mind.available`: known
   AND at this place or universal AND `requires` hold AND capability enabled), shuffled by a seed, with notes (liked,
   disliked, "done a lot lately").
3. `decision.decide`: a 1500-token prompt (sections dropped when over budget: 72 drops in the benchmark), JSON out
   (thought, reasoning, action, inputs), validated, with retries and one reconsider attempt (`_stale_action_problems`).
4. `execute.start` re-checks, writes an `activities` row, applies outcomes on completion (world ops, `body.apply` with the
   preference mood factor and freshness scaling, `remember` -> an `experiences` row).
5. Per decision there are also 2-3 embedding calls (recall query, thought, subject). Median time is 24.8 s on the GTX 1060.

**The menu is not "three random plus continue".** It is *all* available actions (about 6-9 per place), shuffled. "Continue"
exists only in check-ins and interrupts. The stated intended structure (three drawn actions and a fourth: continue) is not
what is built. See decision D1.

**Memory.** `experiences` is the permanent raw log (never forgotten, never in the prompt directly). `memories` holds
`long` and `core` rows written only at sleep (`long_term_per_night`), with the model's importance 0..1, an embedding and
`recall_count`. Retention = importance x recency decay x (1 + recalls x boost); below `forget_below` it is deleted;
`long_cap` (80) pushes the weakest out; near-duplicates merge. There is no short-term store (a "notable today" query over
`experiences`), no fading/liminal layer (forgotten means deleted), and emotional impact does not affect strength.

**Learning.** `knowledge` (what actions it can do, confirmed at sleep). `preferences`: per action/family/location valence
and confidence **computed by code from mood outcomes**, which the model may reject. There are no beliefs with
contradicting evidence, no wrong beliefs, and failures (`rejected` activities) are only logged.

**Concerns.** `loops` (want / question / promise): opened and closed at sleep from cited own experiences, at most 5 open,
fade after 10 days, shown in the Mind. No people, no worries, no ideas, no projects. Drawings are artworks with a status in a
world fact; they keep the PNG and operations but no intention history, evaluation or abandonment.

**Persistence.** In `nit.db` and surviving restarts: state row, body history, world facts, character versions,
knowledge, decisions, activities (a running one resumes), experiences, memories, preferences, loops, artifacts, subjects,
thoughts, sleep runs. **Lost on restart:** the perception buffer, the scheduler's pending triggers, the in-memory `mood_base`.

**Observability.** The dev dashboard shows decisions (the mind, the exact messages, every attempt), sleep runs, memory,
character, repetition, mail, content, and now each LLM call's input and output. There is no view of *why* an option was
shown or chosen except the model's own reasoning text.

## 2. What Pino's pages actually show

Read live from his own pages (his figures, 24 h to 21 Nov: 2,765 moves).

- **Most moves involve no model.** Of 2,765 moves: big model decided 493, small model 354, queued from an earlier decision
  182, **autopilot (no model call) 1,446**, "nothing new, so he carried on" 289. A model call picks **one to three moves**;
  the later ones are queued. "When nothing new has happened he carries on with what he was doing, and on autopilot he chooses
  with no model call." (Correction to an earlier answer of mine: I said his docs did not say whether code picks his moves. His
  facts page says it does, for about half of them, on autopilot.) Cost: 3,770 decisions in 67 hours was $3.65, and "sleeping
  is free".
- **Needs and feelings are worked out every second with no model**; the model chooses "which moves he makes, from the ones on
  offer" and "why, in his own words". The world decides which moves are on offer and how they turn out.
- **Decide, then speak** (roadmap article): first a small typed decision checked against what is possible; then the words for
  it, so "what he says is what he did".
- **Sheet: written versus added by himself.** "22 lines a person wrote, 9 he has added." Locked parts stay. What he adds is
  **habits and found-so lines with the evidence that produced them**: "done on 5 days at the same time of day" (habit),
  "found so on 6 nights" (belief), "a worry that came true 3 times" (a fear), and "let go: no sign of it for 30 nights".
  Everything is listed with its cause.
- **Beliefs ("what he has decided")** are about what doing something does to him ("sitting down at the studio lifts me"),
  worked out at sleep "from how it left him each time", each with how many times it was borne out (28, 24, 20 ... 3).
  "A belief that nothing bears out for long enough gives its place to a new one." Some are about himself ("when I leave a
  thing alone on purpose, I'm usually glad I did").
- **Memory in three parts, each with room for only so many things** (long term 45/45, short term 38/42, liminal 14/200; the
  room grows with the world). "When a part is full, a new memory has to be worth more than the weakest thing in it, or it
  never gets in." Short term "fades in days unless it keeps coming back"; long term is "things that kept coming back on
  different days, and the odd moment big enough to go straight in. It fades over months". Worth = how strong it went in, how
  often it has come back, how long ago. Nothing is deleted on a timer; the weakest is "first to go" and is safe if it comes
  back. **Liminal**: what is left of a long memory is **one worn sentence, written once**, which comes back "only when
  something in front of him touches it, and he knows it is from further back". Kinds: belief, fact, moment, person, promise,
  read, work. A revisited piece adds a line to its memory ("Looking at it again later: half worked, half not").
- **On his mind**: counts of people (32), promises (20), questions (5), wants (2), a piece of work (1); "a thing stays open
  until he finishes it, lets it go, or it fades"; the ones pulling hardest are shown; a want that really matters "stays in
  front of him every thought until he gets it"; a one-line "why he is going where he is going"; what he is making (a list of
  works with kinds); what people asked of him; a confidence number for his own work (-0.19); **three "holds to" sentences with
  a strength 0..1 that "shape what reaches him from the town"**.
- **Practice**: each thing he works at has a count of goes and a 0..1 "how practised" (saturating): 1,695 goes and 1.00 for
  the synth; 6 goes and 0.17 for something new.
- **His own days**: a one-line summary per day ("found a ticket, sprayed a wall, and the synth finally stuck").
- **Roadmap**: neighbours (many minds in one world, "their own needs, their own days, and memories of each other") come
  *after* the builder, where a personality is chosen **from structured lists** (traits, values, fears, habits, how it talks).
- **Everything is checkable**: each claim on the page is backed by raw API data. That is the "check" in `/facts#check`.

What this changes in the plan (section 8 has the decisions):
1. The biggest efficiency lever is **not asking the model when nothing is new** and letting one decision queue several moves.
2. Habits and beliefs are **count-based with explicit thresholds and an expiry**: simple, transparent, and testable without a
   model.
3. Memory **competes for room per layer**, and the forgotten leave a vague trace that only resurfaces on a cue.
4. "Decide, then speak" and an action-first reply may remove most of the thought contamination we saw.
5. Values that shape what reaches him (perception filter) and per-thing practice are cheap additions with real consequences.

## 3. What blocks individuality (findings)

1. **The LLM is the creature.** Personality is prose; the structured state that exists (traits, preferences) does not shape
   what is encountered, felt, remembered or believed.
2. **Same outcomes for everyone.** Content gives fixed numbers; there is no appraisal that depends on who is experiencing.
3. **Everything available is always offered**, so the same few "safe" actions (inspect, study) stay in every menu and a
   small model keeps picking them. Variation by chance is absent.
4. **A decision after every activity** keeps the loop locked to the model's pace, and a decision on unchanged circumstances
   is the repetition we measure.
5. **Memory is too clean and too permanent in the wrong places.** Raw experiences never fade; long-term memory is written
   by one model once a night; no reinforcement of beliefs, no vague survivors.
6. **No creature but Nit**, so "different individuals" cannot even be observed.

## 4. Keep / extend / replace

| Keep as is | Extend | Replace |
|---|---|---|
| content packs, world ops, validators, templating | `body.py` -> a wider inner state with temperament | "all available actions" menu -> drawn menu (D1) |
| `experiences` log, `memories` table (add layers) | `preferences` -> beliefs with evidence for/against | `loops` -> a fuller concerns model (migrate rows) |
| sleep stages and checked citations | `identity` versions with evidence -> confidence + decay | `check_in` LLM calls -> local continuation unless changed |
| scheduler triggers, `starving` | `scheduler` with need / concern / project triggers | |
| artifacts and Gwen | drawings -> projects with intention/evaluation | |
| devdash/readmodel | an "inner life" page per stage | |

## 5. Target design

A creature is **a persistent state vector plus histories**; the LLM is asked only to interpret, choose among a drawn menu,
reflect, and word things.

- **Temperament** (structured, per creature, in the character file): sensitivities and tendencies as numbers, e.g.
  novelty-seeking, sociability, solitude-need, caution, impulsiveness, persistence, volatility, self-criticism, frustration
  tolerance. They are *inputs to code*: they scale how needs grow and how outcomes land, set how often it acts on a first
  pull, and select prompt framing. They are not injected as adjectives.
- **Affect and needs** (`inner` state, deterministic updates): affect = mood, stress, confidence, curiosity, loneliness,
  frustration, satisfaction. Needs = rest, novelty, social, solitude, expression, accomplishment, belonging, security.
  Levels rise with time and fall by declared effects (`needs:` on actions/outcomes/events in content), all scaled by
  temperament. Contradictions are allowed (curious and frightened; lonely and avoiding company). They are shown as plain
  words and as **notes on the options they bear on**. They never remove, add or rank an option. The one override that stays
  is starvation.
- **Appraisal**: the same outcome lands differently: the mood/need effects are multiplied by the creature's sensitivities and
  relevant beliefs/memories. At memory formation the creature words its own interpretation, so memories differ in text too.
- **Menu (D1):** a uniform draw of three from the genuinely available actions, plus "continue" when something is running
  (and a "stay as I am" when idle). Uniform on purpose: code does not choose what the creature is offered *for* it. Movement
  and sleep/eat safety need a rule (see D1).
- **Memory, three layers in one table:** `short` (capacity, e.g. 12; written during the day from experiences by significance
  = emotional impact x novelty x relevance x flagged-to-remember; new ones displace the weakest), `long` (written at sleep
  from short; strength = initial significance + reinforcement by recall - decay; cap ~80), `faded` (a long memory that
  weakens below a threshold keeps only a gist and a feeling, may resurface vaguely, then goes). Repeated events reinforce one
  memory (`occurrences`) instead of duplicating. Retrieval only injects memories above a relevance threshold and attaches
  them to the options they bear on.
- **Habits and beliefs with thresholds and expiry (Pino-style, tuned by us):** a habit row appears when an action/place is
  done on N distinct days in the same time-of-day band; a belief about "what this does to me" when the same outcome has
  followed on N distinct nights; a fear when a worry came true N times; each is dropped after M nights with no sign. All of
  it counted by code from experiences, with the evidence ids kept.
- **Beliefs:** `beliefs(statement, subject, valence, for_count, against_count, strength, last_reinforced, sources, status)`.
  Formed by code from repeated co-occurrence (deliberately naive, so wrong beliefs happen), worded by the model, reinforced and
  weakened by later outcomes, slow to change when strong, decaying when unreinforced. Failures are classified by code
  (circumstance / unmet requirement / capability / unexpected result) and the creature attributes a cause at sleep, with an
  attribution bias from temperament, so the wrong lesson is possible.
- **Personality evolution:** acquired traits and habits are structured rows with evidence ids, confidence, `last_reinforced`
  and decay; habits are code-detected patterns (action x place x time of day) shown as "you tend to". The foundational sheet
  is never rewritten by the model.
- **Concerns:** kinds person, question, want, worry, promise, project, problem, idea; each with salience that decays and can
  resurface, a lifecycle, and a trigger when it becomes loud. Unresolved ones are allowed to stay.
- **People:** `people(name, familiarity, affinity, last_seen, notes)` from letters now; other creatures later.
- **Projects:** an artwork/idea record with intention, attempts, the creature's own subjective evaluation (including
  disliking it), future possibilities, and a status including abandoned; the evaluation feeds later wants.
- **Event-driven and tiered inference:** local continuation of an activity; no check-in model call unless something changed;
  need/concern/project triggers; tiers: *gut* (short prompt, no reasoning, for impulsive or routine moments), *full*, and
  *reflect* (the sleep model). A hash of the situation prevents re-deciding unchanged circumstances.
- **Decide, then speak; queue 1-3 moves:** a decision is first a small typed choice (action and inputs, written *before*
  any prose, so a "quiet" thought cannot steer the action), checked against what is possible; the words for the diary are
  written after (or alongside, in that order). One decision may queue up to three moves; they run without a model unless
  something new happens, in which case the queue is dropped and the creature decides again.
- **Holds-to values and practice:** a few structured values per creature with a strength 0..1 that bias *what is noticed*
  (a visible fact matching a held value is more likely to appear in "what you can see"); a practice level per action/object
  (goes count, saturating) that affects outcome quality and ease.
- **Observability:** every decision stores a structured factor record (what was drawn, the needs snapshot, which notes and
  memories and beliefs bore on which option, the gut/full tier). The dashboard reads that, not the model's reasoning.

## 6. Staged plan (smallest coherent change first; each stage migrates, tests, documents, and adds its dashboard panel)

| Stage | What | Why first | Cost |
|---|---|---|---|
| 0 | Instances: `--character FILE` and a second, very different character; a compare script over N databases | without it, distinctness cannot be observed | small |
| 1 | Temperament, affect and needs with appraisal (deterministic, persistent, notes on options) | personality becomes code-level; outcomes differ per creature | medium |
| 2 | The drawn menu (D1), action-first replies (D6), gut/full tiers | most direct cure for repetition; adds chance-driven divergence | medium |
| 3 | Event-driven continuation, 1-3 queued moves, skipping unchanged circumstances (D5a) | cuts LLM calls (speed), ends decision-per-activity | medium |
| 4 | Memory layers (short, long, faded), reinforcement, deduplicated occurrences | memories diverge by what mattered to each | medium-large |
| 5 | Beliefs, naive learning, failure attribution | wrong, hard-to-change beliefs | medium |
| 6 | Concerns, people, projects with evaluation and abandonment | unfinished business and creative history | large |
| 7 | Acquired habits/traits with counts, thresholds, expiry and an evidence trail; holds-to values; practice | identity evolves without rewriting the sheet | medium |
| 8 | Divergence evaluation on real runs | the success test | script + time |

## 7. Constraints that shape this

- **Hardware:** one GTX 1060 (6 GB). A second creature running at the same time halves throughput at today's 25 s per
  decision. Stage 3 (fewer calls) matters before several creatures run together. Independent instances time-share Ollama.
- **A 4B model reads only so much.** More state in the prompt costs tokens (budget 1500) and a small model may ignore subtle
  state. That is why the plan puts effects in *code* (draw, appraisal, consequences, notes on specific options) and keeps the
  prompt short. Anything that must influence behaviour has to be testable without the model.
- **Quoting back what a model produced makes it copy it** (measured: thought repeats rose from 42% to ~85% when a guard quoted
  thoughts). No stage may show a creature its own generated words as a prompt to avoid.
- **Tests run on the mock backend.** Divergence is tested at the level the architecture controls (menus, appraisal, needs,
  memory formation, beliefs) with scripted choosers; real divergence is judged from real runs (stage 8).

## 8. Decisions needed before stage 1

- **D1. The drawn menu.** Today everything available is offered. A uniform draw of 3 + continue changes "the menu is
  knowledge ∩ world, never removed". Recommended: adopt it, but always draw from the full available set, and treat `move`
  as one of the actions (so leaving a place becomes a matter of chance and desire, which changes how often it moves; if
  that is too little, add one guaranteed "go somewhere" slot). Eating and sleeping are protected by the starving takeover
  and the low-energy note, not by the menu.
- **D2. Needs and the rule "code never picks the move".** The rewrite removed needs because code overrode the model. The
  plan reintroduces needs but only as state, words and notes, never as overrides (starvation stays the one exception).
  Confirm that reading.
- **D3. Several creatures: independent instances or one shared world?** Independent instances (same content pack, own
  database, different character) are enough to judge individuality and are cheap. A shared world with creatures meeting each
  other is a much larger change (shared fact store, concurrent writers, relationships between creatures).
- **D4. Migrating Nit's life.** Nit keeps everything. Backfill: temperament derived from current trait strengths and
  tendencies, beliefs seeded from preferences, memories into `long` with strength from importance and recalls, loops into
  concerns. Confirm that Nit should be migrated rather than starting a new life.
- **D5. Autopilot.** Pino's code chooses about half of his moves with no model (routine continuation and habits). For us
  there are two readings. (a) *Continuation only*: when nothing new has happened the current activity simply continues or the
  queued moves run (no model, no code choice). (b) *Habit autopilot*: code may also pick a habitual next action (for
  example the place and activity he usually does around midday). (b) is a second exception to "code never picks the move"
  and is what would make acquired habits real behaviour. Recommended: (a) now, (b) only if habits stay too weak.
- **D6. Decide-then-speak.** Action-first output is a cheap experiment (reorder the JSON; fewer output tokens). A full two-step
  (a typed decision, then a separate "words" call) doubles calls but guarantees that the diary says what was done. Recommended:
  action-first in one call; revisit after measuring.

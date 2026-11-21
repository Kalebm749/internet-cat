# From one chatty creature to distinct individuals: map, design, and staged plan

Status: **proposal, not yet accepted.** `docs/rewrite-contracts.md` still wins until a stage below is accepted, built, and
folded into it ("as built" notes). This file is the working plan; it is checked against the code as of PR #43.

Pino (pinos.world) is used as inspiration only. This session could not reach `pinos.world/facts` (the network policy
denies the host), so what is borrowed here comes from Pino's public API docs: a small memory where something new gets in only
by pushing something weaker out; beliefs that carry an evidence count; wants, worries, questions and promises that stay open
between decisions; per-part update clocks; and a character that grows by what it did, with its own additions listed
separately. Where the facts page would give numbers or rules, this plan says "tune by measurement" instead of guessing.

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

## 2. What blocks individuality (findings)

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

## 3. Keep / extend / replace

| Keep as is | Extend | Replace |
|---|---|---|
| content packs, world ops, validators, templating | `body.py` -> a wider inner state with temperament | "all available actions" menu -> drawn menu (D1) |
| `experiences` log, `memories` table (add layers) | `preferences` -> beliefs with evidence for/against | `loops` -> a fuller concerns model (migrate rows) |
| sleep stages and checked citations | `identity` versions with evidence -> confidence + decay | `check_in` LLM calls -> local continuation unless changed |
| scheduler triggers, `starving` | `scheduler` with need / concern / project triggers | |
| artifacts and Gwen | drawings -> projects with intention/evaluation | |
| devdash/readmodel | an "inner life" page per stage | |

## 4. Target design

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
- **Observability:** every decision stores a structured factor record (what was drawn, the needs snapshot, which notes and
  memories and beliefs bore on which option, the gut/full tier). The dashboard reads that, not the model's reasoning.

## 5. Staged plan (smallest coherent change first; each stage migrates, tests, documents, and adds its dashboard panel)

| Stage | What | Why first | Cost |
|---|---|---|---|
| 0 | Instances: `--character FILE` and a second, very different character; a compare script over N databases | without it, distinctness cannot be observed | small |
| 1 | Temperament, affect and needs with appraisal (deterministic, persistent, notes on options) | personality becomes code-level; outcomes differ per creature | medium |
| 2 | The drawn menu (D1) plus gut/full tiers | most direct cure for repetition; adds chance-driven divergence | medium |
| 3 | Event-driven continuation and unchanged-circumstance skipping | cuts LLM calls (speed), ends decision-per-activity | medium |
| 4 | Memory layers (short, long, faded), reinforcement, deduplicated occurrences | memories diverge by what mattered to each | medium-large |
| 5 | Beliefs, naive learning, failure attribution | wrong, hard-to-change beliefs | medium |
| 6 | Concerns, people, projects with evaluation and abandonment | unfinished business and creative history | large |
| 7 | Acquired traits and habits with decay; evidence trail | identity evolves without rewriting the sheet | medium |
| 8 | Divergence evaluation on real runs | the success test | script + time |

## 6. Constraints that shape this

- **Hardware:** one GTX 1060 (6 GB). A second creature running at the same time halves throughput at today's 25 s per
  decision. Stage 3 (fewer calls) matters before several creatures run together. Independent instances time-share Ollama.
- **A 4B model reads only so much.** More state in the prompt costs tokens (budget 1500) and a small model may ignore subtle
  state. That is why the plan puts effects in *code* (draw, appraisal, consequences, notes on specific options) and keeps the
  prompt short. Anything that must influence behaviour has to be testable without the model.
- **Quoting back what a model produced makes it copy it** (measured: thought repeats rose from 42% to ~85% when a guard quoted
  thoughts). No stage may show a creature its own generated words as a prompt to avoid.
- **Tests run on the mock backend.** Divergence is tested at the level the architecture controls (menus, appraisal, needs,
  memory formation, beliefs) with scripted choosers; real divergence is judged from real runs (stage 8).

## 7. Decisions needed before stage 1

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
- **D5. The Pino facts page.** Please paste its text (the network policy blocks `pinos.world` here), so memory/belief
  parameters can start from his shapes and be re-tuned by measurement.

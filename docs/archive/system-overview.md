# System overview (as built, for review)

A reading guide to every system, written to be judged against how the running system *feels*.
Facts here come from the code and config; the last section is opinion and unverified guesses, marked as such.
The real model has never been run by the author of this document: everything was tested with the mock brain.

## 1. The loop in one paragraph

Three containers: **ollama** (models), **engine** (the only writer; tick loop, SQLite) and **web** (read-only
site), plus an optional **devdash** (read-only dashboard, writes only to its own db). Each tick the engine
rolls world events, maybe asks the **director** for a happening, builds a **menu of moves** from the world
(`config/moves.yaml`), asks the **brain** to pick one (with a reason, a reaction and a feeling), lets **code**
decide what the move does (need gains, effort, duration, world change), asks the **journal** (in its own voice, after the fact) for one or two
sentences of prose, then applies everything in one DB transaction. A move longer than 20 sim minutes becomes an
**activity**: the next ticks only advance the clock (no LLM calls) until it ends or something interrupts it.
When it is tired at night it sleeps, and the **sleep cycle** (up to six LLM calls) consolidates memories and
changes tendencies, preferences, self-image, projects and habits.

## 2. Systems, one by one

| System | Files | What it owns | What it does *not* do |
|---|---|---|---|
| Clock and pacing | `clock.py`, settings `tick_interval_seconds`, `activity.*` | sim time, day/night phases, real-time pacing, carry ticks wait less | sim time is not tied to real time; a day passes in about two real hours at defaults |
| Needs | `character.py`, `character.yaml` | five levels 0..100, decay per hour, bands, band words, what is "pressing" | needs never rank the menu; they only shape the prompt (max 2 pressing) and mood |
| Energy | `engine.py` | a separate −50..50 resource: effort spends it, sleep restores it | not a need; never shown as a number |
| Mood | `character.py`, `mood.*` | one −50..50 number from need bands, event nudges, the brain's 3-value `feeling` | mood does not change what is offered or picked |
| Moves | `moves.py`, `moves.yaml` | the menu (seeded shuffled order, cut at `moves.max_offered`, "(never tried)" marks) and each move's effect | no ranking, no marking by need (the feeding reflex is the one need-driven pick) |
| Brain | `brain.py`, `prompts.yaml` | prompt assembly; JSON schema `reaction, thought, move, why, feeling` | an invalid or off-menu move fails the call (retry once, then doze) |
| Journal | `journal.py` | its own entry, written after code decided what happened, from facts only | cannot add details, change numbers or the world |
| Effects | `effects.py` | the only code that changes the world: seven validated ops | rejected changes are logged, not applied |
| Activities | `engine.py` | long moves, carry ticks, interruption by events or a newly empty need | carry ticks make no decisions |
| World director | `director.py` | one small happening every few ticks (LLM), validated as an event | never sees needs or mood |
| Memory | `memory.py` | short-term (notable ticks), long-term (consolidated), recall by recency, importance and embedding relevance | no beliefs, no distortion, no real/imagined distinction |
| Sleep | `sleep.py` | the nightly cycle; nothing is written unless every call succeeds | not run when the brain is offline (stays pending) |
| Tendencies, preferences, self-image | `sleep.py`, `character.py` | slow drift (≤ 0.05 a night), likes/dislikes (dislikes drain needs when present), at most one self-image line a night | self-image starts empty and is learned |
| Projects | `projects.py` | formed in sleep, advanced by "work on" moves, milestones, a saved piece made by a separate call in the brain's voice | a project never picks a move |
| Habits | `habits.py` | code counts recurring (move, part of day) pairs; sleep words them; shown as "you tend to…" | description only; never ranks a move |
| Repetition | `engine.py` | same move key in the same place within 6 decisions shrinks wonder/curiosity gains ×0.4 | flat penalty; project work exempt |
| Downtime / doze | `downtime.py`, `engine.py` | brain offline → doze ticks; engine off → pause or catch-up | |
| Tracing | `trace.py` | every number and call is stored for the dashboard; a trace failure is only a warning | |
| Website | `cat/web/` | the diary (with "because: …", "about N min", carried-on steps), mood slider, needs, habits, projects, keepsakes, self-image | read-only, never calls an LLM |
| Dev dashboard | `cat/devdash/` | tick detail, LLM calls, sleep nights, memory, config, playground, controls, stats | never writes game state |
| Tools | `scripts/` | seed db, add ticks, check Ollama, show diary, reembed, **balance simulator** | |

## 3. Where each LLM call happens and what it costs

| Call | When | Per |
|---|---|---|
| brain | every decision (not carry ticks) | decision |
| journal | after every decision (`journal.enabled: false` removes it) | decision |
| director | every ~6 decisions (2 when stale) | few decisions |
| create (a project piece) | at a milestone or finishing | rare |
| sleep (consolidate, tendencies, preferences, self-image, projects, habits) | each night | night |
| embeddings | recall each decision; saving memories | decision |

So a decision costs about two chat calls plus one embedding. Default models are all `qwen3:14b`, split between the 6GB GPU and RAM.

## 4. Data flow that matters for "feel"

- What the brain *sees*: self-image, tendencies, habits, likes/dislikes, time/place, mood and energy words, up to two pressing-need expressions, objects, events, last outcome, recent actions, recalled memories, active projects, and the menu.
- What the brain's choice *changes*: needs (via move gains), energy, location/objects, project progress, memories (if notable), and — only through sleep — personality.
- What the brain's text *changes*: its `reaction` and `why` show on the site; its `thought` is stored; its `feeling` nudges mood. None of those alter behavior directly.

## 5. Rough edges and open questions (opinion; verify by running it)

1. **Never run on the real model.** Unknown: whether the 14B model follows the menu-id JSON reliably, latency per decision, whether `why` reads as thought or filler, whether the narrator adds anything the template does not.
2. **Little feedback from inner life to behavior.** Mood, feeling, thought, self-image and tendencies only change prompt words. Whether that is enough variety is exactly what a run will show.
3. **Menu composition shapes behavior.** Verbs with many targets (look closely at X for every object) crowd the menu; the simulator shows random play is lopsided because of it. The model may be biased by list order and length.
4. **Needs may still be unbalanced.** One balance pass was done; some need is empty about half the time under random play. Real-model behavior will differ from the scripted policies.
5. **Pacing feels:** carry ticks make the diary sparse (one entry per move) and the world quiet between decisions; the director and events only break into an activity at tick boundaries.
6. **Sleep is a single big step** (up to six calls): one bad call stalls the whole night's change until retried.
7. **`engine.py` is large (about 950 lines)** and does orchestration, activities, moves, sleep triggering and recording. Candidate for splitting if it hurts.
8. **Placeholder content:** the one character, the small world (4 areas, about 10 objects) and the hand-written events are placeholders. A small world limits variety more than any model does.
9. **Docs describe the closed-moves design**, but the older concept doc (`creature-architecture.md`) still carries the original design text under a revision note.
10. **Unmeasured:** the dashboard shows tokens and latency, but nothing yet scores "interestingness", repetition in the diary text, or divergence between runs.

## 6. How to review it on your machine

- Pull `main`; run `deploy/pull-models.sh` for `qwen3:14b`; start Compose per the README.
- Watch the website and the dev dashboard's Ticks, LLM calls and Overview (decisions vs carried-on, tokens).
- Read the first few ticks' prompts and raw responses (Tick detail) before judging the diary.
- Let it sleep once and read the Sleep page (what each step proposed vs what code applied).
- Notes that help most: moments that felt dead or repetitive, replies that ignored the menu, prose that did not match the move, anything the creature did that surprised you.

# Cognitive architecture: assessment, plan and backlog

Goal: an entity with an evolving inner world (perception, memory, emotion, belief, expectation,
imagination, goals) that shapes what it does, including badly. Physical truth, state transitions and
action execution stay deterministic code; the LLM interprets and words. Every system below must fix a
named limitation, share one inner state, be traced, and be measured by a scenario test.

## 1. What is limiting today (as built)

| Area | Today | Limitation |
|---|---|---|
| Inner state | five needs, energy, one mood number from need bands, a 3-value `feeling` tag from the brain | No named emotions, no memory of feeling, nothing that decays, builds or spills over. Mood is a function of needs only, so events and outcomes barely register. |
| Perception | the prompt lists what is here, events, last outcome | No notion of expectation, so no surprise; nothing is "noticed" because it differs from what was predicted. |
| Memory | short-term notable ticks, long-term consolidated text, recall by recency/importance/relevance | Facts only: no beliefs with evidence, no confidence, no contradiction, no distortion, no imagined vs. real. |
| Decision | one LLM call per decision picks from a code menu; code never overrides | One speed only (carry ticks skip the call but do not decide anything). No conflict between wants, no reflex, no impulse. |
| Slow change | sleep: memories, tendencies, preferences, self-image line, one project, habits | Reflection only summarises; it never questions an earlier belief, imagines alternatives, or invents a goal beyond "project". |
| Individuality | tendencies drift 0.05 a night, preferences, self-image | Two creatures would differ little: no stable biases or sensitivities that change how the same event lands. |
| Evaluation | unit tests with a mock brain; balance simulator | No scenario test of cognition ("does a betrayal change later behavior?", "do two creatures diverge?"). |

## 2. Design principles (decided)

- One shared **inner state** (emotions, beliefs, expectations, goals) in SQLite, read by every system and shown to the brain as words, never numbers.
- **Fast vs slow.** A fast path is code: reflexes and impulses from emotion, habit and expectation, seeded and traced, no LLM call. A slow path is the brain (and sleep) for novel, surprising or conflicted situations. Most ticks should be fast or carried.
- **Rule relaxed (decided):** needs still never rank or mark the menu. Emotion and impulse may change what is offered, how options are shown, and sometimes the final pick. Every override is traced and shown in the diary ("it lashed out before thinking").
- No built-in moral lean: no field, prompt or default pushes toward good outcomes. Harm, selfishness and restraint all come from experience, traits and circumstances.
- Distortion is code, not hope: biased recall, overconfidence and flawed memory are computed deterministically from emotion and traits, then fed to the prompt.
- Cost: nothing new may add an LLM call per tick. New LLM work is at sleep or on rare "slow" ticks.

## 3. Phases (each is shippable, tested and measured)

**Phase 1: inner state (first).**
1a. *Emotions*: a small set of named affects (e.g. fear, anger, sadness, delight, shame; names and triggers in config, not code) as levels 0..100 that rise from events, surprise, outcomes and unmet needs, decay over time, and linger across ticks and sleep. Mood becomes a function of them plus needs. The prompt shows the strongest in words.
1b. *Expectation and surprise*: code keeps what each move usually yields in each place (running averages per `move_key`, place). Surprise = actual minus expected. Surprise feeds emotion (fear on a bad surprise, delight on a good one), makes a tick notable, and is told to the brain ("that was not what you expected").
1c. *Beliefs*: a `beliefs` table (claim, confidence, evidence for/against, source, formed/updated). Code proposes candidate beliefs from repeated outcomes (e.g. "the kitchen is where food comes"); sleep words them, may drop or lower them on contradiction, and a belief can be wrong. Confidence changes by evidence, with bias terms.
1d. *Measurement*: scenario tests (a betrayal-like event, a repeated reward, a loss) that assert emotion, belief and later behavior shift, plus a two-seed divergence metric.

**Phase 2: fast and slow.** Reflexes and impulses from emotion and habit (flee, freeze, lash out, comfort-seek) chosen by seeded code on high-emotion ticks, with the brain called only when the situation is novel, surprising or conflicted. Internal conflict: two strong wants in tension are named in the prompt, and an impulse can win.

**Phase 3: reflection and imagination.** Sleep gains: question an existing belief against the day's evidence; "what if" rehearsal (imagined items stored as imagined, never as fact); goals the entity invents (generalising projects), with code tracking progress. Daytime slow ticks may rehearse a risky option before choosing.

**Phase 4: individuality.** Stable sensitivities and biases per creature (what frightens it, what it over-trusts), memory distortion by emotion, trait feedback from behavior. Divergence metric across seeds in the same world.

## 4. Backlog (parked on purpose, written down)

- **Irrationality mechanism for acting against its own interests or values.** Undecided: code-side impulse override, prompt-side distortion only, or both. Revisit after Phase 1 shows how far emotion alone moves behavior. (The rule is already relaxed for emotion and impulse; this is about *values*-contradicting choices, deception, self-sabotage.)
- **Other beings = site visitors.** Relationships, attachment, deception and aggression will come from how visitors interact through the website (a visitor channel, rate-limited and moderated, and their effect as events with a source). Needs its own design: identity of visitors, abuse handling, what the entity can perceive. Not started. Overlaps the deferred "external input channel".
- Pino-style open "mind" threads (want / worry / question / promise / person / piece of work) as one general structure that beliefs, goals and projects could share.
- Multiple creatures run by the engine (not planned; visitors replace this).

## 5. Working method

One phase at a time: assess, add the smallest system that fixes the named limitation, trace it for the dashboard, add a scenario test, run the balance simulator, update the behavior guide, merge.

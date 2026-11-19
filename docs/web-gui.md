# Web GUI Spec: {Name}'s Journal

Spec for the website. Read `server-architecture.md` (§13) for where this sits: a FastAPI service in Docker Compose on the host laptop, read-only, served on the home network.

**Feel:** cozy storybook, like opening the entity's own journal. Warm paper, ink, soft colours, hand-made touches.
**Target:** desktop first (≥1200px wide), still usable on a narrow screen.
**Mode:** read-only. Visitors watch and can't interact (yet).

---

## 1. Principles

1. **The diary is the heart.** It gets the most space and the centre of the page. Stats support the story, not the other way round.
2. **Words before numbers.** Every bar also has a word ("peckish", "sleepy"). Exact numbers show only on hover.
3. **The page feels alive.** It reflects the sim time (dawn, day, dusk, night), shows sleep and dozing, and new diary entries appear gently.
4. **No framework.** Server-rendered HTML (Jinja2) + one small vanilla JS file that polls `/api/state`. It should stay light and fast.
5. **Works offline.** Fonts and images are self-hosted. No CDNs, since the site lives on the LAN.

---

## 2. Layout (desktop)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ HEADER   Mochi · the bookshop · Wed, Day 12 · 7:40 pm (evening)   ● awake   │
├───────────────────┬──────────────────────────────────┬───────────────────────┤
│ LEFT  (≈300px)    │ CENTRE  (flex, ≥520px)           │ RIGHT  (≈320px)       │
│                   │                                  │                       │
│ Presence (frame)  │ Diary                            │ About Mochi           │
│                   │  ─ entries, newest first ─       │  Core traits (chips)  │
│                   │                                  │  What it thinks it is │
│ Mood + energy     │  ─ "last night" sleep card ─     │  Tendencies (sliders) │
│                   │  [ read older pages ]            │  Loves / dislikes     │
│ Needs (bars)      │                                  │                       │
│                   │                                  │ Keepsakes (long-term  │
│                   │                                  │  memories, cards)     │
└───────────────────┴──────────────────────────────────┴───────────────────────┘
```

- Max content width ~1400px, centred. Columns are CSS grid: `300px minmax(520px, 1fr) 320px`, gap 28px.
- Left and right columns are `position: sticky` (top: 24px) so they stay visible while the diary scrolls.
- **Under 1100px:** two columns (left + centre). The right column's sections move below the left column's.
- **Under 720px:** one column, in this order: header → presence → mood/energy → needs → diary → about → keepsakes.

---

## 3. Visual language

### Palette (CSS custom properties)

| Token | Day | Night | Use |
|---|---|---|---|
| `--paper` | `#F6EFE2` | `#23201C` | page background |
| `--card` | `#FBF7EF` | `#2D2924` | panels |
| `--ink` | `#3B3127` | `#EDE3D3` | main text |
| `--ink-soft` | `#7A6A58` | `#B5A792` | secondary text, timestamps |
| `--rule` | `#E3D6C0` | `#463F36` | borders, dividers |
| `--accent` | `#C0643B` | `#E08A5F` | terracotta: highlights, low needs |
| `--sage` | `#7E9A72` | `#9DB892` | healthy bars, "loves" |
| `--dusk` | `#5B6F8C` | `#8FA3C2` | energy, sleep, night accents |
| `--honey` | `#D9A441` | `#E8BC63` | mood, importance markers |

- **Time of day:** add a class on `<body>` from the sim time: `dawn | day | dusk | night`. `night` swaps to the night palette. `dawn` and `dusk` add a soft tinted overlay on the header (no gradients on cards). Changes fade over 2s.
- Needs in the *low* or *empty* band turn their bar `--accent`. *Empty* needs also get a gentle pulse (turned off when `prefers-reduced-motion` is set).

### Type (self-hosted, all OFL-licensed)

| Role | Font | Size |
|---|---|---|
| Headings, name | **Fraunces** (soft serif), weight 500 | 28 / 20 / 16px |
| Body, outcomes | **Lora** | 16px / 1.6 |
| Creature's own words (reactions, thoughts) | **Caveat** (handwritten) | 20px |
| Small labels, timestamps | Lora italic | 13px |

Put the font files in `static/fonts/` as `.woff2` files with `font-display: swap`.

### Surfaces
- Cards: `--card` fill, 1px `--rule` border, 14px radius, 20px padding, and a faint paper texture (a tiny tiled PNG at about 4% opacity, optional).
- Section titles look like ink labels: Fraunces 16px with a short hand-drawn underline (an inline SVG squiggle, `--accent`).
- No heavy shadows. At most `0 1px 0 var(--rule)`.

---

## 4. Components

### 4.1 Header
- Left: creature name (Fraunces 28px).
- Middle: `the bookshop · Wednesday, Day 12 · 7:40 pm (evening)`. Day N counts from the creature's first tick.
- Right: **status pill**:
  - `awake` (sage dot)
  - `asleep` (dusk dot, "asleep · dreaming")
  - `dozing` (honey dot, shown when the brain is offline)
- Far right: a small brain indicator: "brain: connected / last seen 6:02 pm". Hover shows the model name.

### 4.2 Presence (the portrait frame)
- The frame stays: an oval or arched "photo frame" border, now holding an **abstract presence** instead of a picture: soft concentric rings around a core, drawn as inline SVG (`#presence`). **No figure, no silhouette, no body.** Nothing in it suggests what the entity is.
- **Colour follows mood:** hue runs from blue (mood −50) to amber (+50).
- **Motion follows energy:** the rings breathe; one breath takes ~7s when tired (energy −50) and ~1.4s when full of energy (50). No motion under `prefers-reduced-motion`.
- **Asleep:** the presence dims (lower lightness) and a small animated "z z z" drifts up (CSS, reduced-motion safe).
- **Dozing:** the presence desaturates and dims slightly, plus a caption "dozing… (brain offline)".
- The server computes the values (`views.presence_view`, tuned by `web_ui.presence` in `settings.yaml`) and sends them as CSS variables (`--pv-h`, `--pv-s`, `--pv-l`, `--pv-t`) on the SVG, so the first paint is right without JS. `journal.js` updates the same variables.
- Caption under the frame: the entity's name and the current area ("Mochi, in the bookshop").
- Hook for later: an optional `portrait_model` could replace the SVG inside the frame. Nothing in the code reads it today, and no model loader exists. (The paused cat render and `.glb` plan are not part of this design.)

### 4.3 Mood and energy
- **Mood:** a large word (Fraunces 22px, `--honey` underline), e.g. *content*, plus a one-line description from the engine ("tired but glowing after the sunset").
- A **mood slider**: a short horizontal track from *negative* to *positive* with a dot at the current mood (−50..50). It has a text equivalent (`aria-label`).
- **Energy:** a bar in `--dusk` with a word (*wide awake / fine / tired / exhausted*). Energy is a separate resource from −50 to 50, not a need.

### 4.4 Needs
- Five rows (one per need, in config order): `label · band word · bar`. The word is the need's **band**: *full*, *okay*, *low* or *empty*. No number is shown or spoken.
- **Hover** shows the need's **meaning** (the sentence from `character.yaml`), e.g. "Awe and delight at the world: being moved by what is beautiful, strange or vast."
- Bar: 8px tall, rounded, `--rule` track, `--sage` fill (`--accent` when the band is low or empty).
- Trend arrow after the bar: ▲ / ▼ / — compared with the previous tick (only if the change is >2).
- Hover shows "62 / 100, +8 since last tick".
- Words: from the optional `words` in `character.yaml` for each need. Otherwise the defaults are: <15 *desperate*, <35 *low*, <70 *okay*, ≥70 *content*.

### 4.5 Diary (centre)
A journal page. Entries are newest first, grouped under **day headers** ("Wednesday, Day 12").

Each **tick entry**:
```
7:20 pm · the park                                       ⟵ small, ink-soft
climbs the old oak to watch the sunset                   ⟵ action, Lora italic
She scrambles up, slipping once, and the sky turns      ⟵ outcome (narrator), Lora
orange over the rooftops.
  "worth the slip. the sky looked like a sardine."     ⟵ reaction, Caveat, --ink
  joy ▲  peace ▼                                         ⟵ need changes, small chips
```
- **World events** that happened that tick show as a margin note above the entry: small italic, a leaf/dot marker, e.g. *It starts to drizzle.*
- **World changes** (moved, created, consumed objects) show as tiny tags: `+ fort (created)`, `− sardine (eaten)`.
- **Dozing ticks** collapse into a single line: "dozed from 6:02 pm to 6:40 pm (brain offline)".
- **Sleep card:** when a `sleep_log` exists for a night, show a distinct card at that point in the diary: "Last night…" with the summary, the tendency changes ("a little bolder ▲") and new preferences. Dusk-coloured border.
- **New entries** fade and slide in (200ms) at the top when polling finds them. The page doesn't jump if the reader has scrolled down. In that case, show a small "1 new entry ↑" pill instead.
- Starts by showing the last 20 entries. "Read older pages" at the bottom loads 20 more.

### 4.6 About (right)
- **Core traits:** chips (pill, `--rule` border, Lora 14px). Static, from config.
- **Tendencies:** one row per tendency, drawn as a slider: a track with labelled ends (e.g. *timid* ↔ *bold*) and a dot at the value. If last night changed it, show a faint ghost dot at the old position and a ▲/▼. End labels come from config (`tendency_labels`), defaulting to `low ↔ high`.
- **What it thinks it is:** the entity's current self-image lines (learned during sleep; see `creature-architecture.md` §7), one per row in italics, oldest first. Hover shows how sure it is and the day it was formed. If there are none, the section says *still figuring that out*. The entity starts with no self-image; a line may also come from an optional `character.yaml` `self_image.seed` hint.
- **Loves / dislikes:** two short lists, top 5 each by |valence|. Small heart / cross marks drawn as inline SVG (no emoji). Hover shows the reason ("keeps coming back here at dusk").

### 4.7a Projects (right column, above Keepsakes)
- **What it is working on:** one row per project (active first, then set-aside, then finished; at most 8): the title, a status and **stage in words** ("working on it · well along"), and the running note ("where it got to"). Hover shows why it started it. Never the progress number.
- **Made so far:** the latest `projects.artifacts_shown` (6) artifacts, newest first, as small cards: title, kind, project and day, then the text. A made piece also adds a tag to its diary entry ("made “Roots”").
- Empty states: "no projects yet." and "nothing made yet."

### 4.7 Keepsakes (long-term memories)
- Small index cards, newest first, 6 shown, then "more keepsakes" to see the rest.
- Each card: memory text (Lora 15px), date (sim day), importance as 1–3 honey dots.
- Cards are slightly rotated (±0.6°, alternating) for a pinned-to-the-wall look. No rotation when reduced motion is set.

### 4.8 Empty and error states
- **First boot (no ticks yet):** diary says "the journal is empty. mochi hasn't woken up yet." Keepsakes: "no keepsakes yet."
- **API unreachable** (the engine or web process is down): a soft banner at the top, "can't reach the journal right now, retrying…". Keep showing the last data.

---

## 5. Data API

All JSON, read-only, served by `cat/web/app.py`.

### `GET /api/state`
Polled every 10s. Everything except old diary pages.
```json
{
  "creature": {"name": "Mochi", "pronoun": "she",
               "core_traits": ["curious", "dramatic", "secretly sentimental"]},
  "time": {"sim_time": "2026-11-30T19:40", "day_number": 12,
           "label": "Wednesday, Day 12 · 7:40 pm", "phase": "dusk"},
  "location": {"id": "bookshop", "name": "the bookshop"},
  "status": {"state": "awake", "brain_online": true,
             "brain_last_seen": "2026-11-18T18:02:11", "model": "qwen3:8b"},
  "mood": {"word": "content", "description": "tired but glowing",
           "value": 18.0, "slider_pct": 68.0, "aria": "Mood slider: content, positive, from negative to positive"},
  "energy": {"value": -12, "pct": 38, "word": "tired"},
  "needs": [{"id": "wonder", "label": "wonder", "value": 62, "band": "okay", "word": "okay",
             "meaning": "Awe and delight at the world: ...",
             "delta": 8, "low": false, "critical": false}],
  "tendencies": [{"id": "boldness", "low_label": "timid", "high_label": "bold",
                  "value": 0.55, "previous": 0.50}],
  "presence": {"hue": 120, "saturation": 70, "lightness": 58, "pulse_seconds": 5.2,
               "style": "--pv-h:120;--pv-s:70%;--pv-l:58%;--pv-t:5.2s",
               "aria": "A soft glow for Mochi: ...", "caption": "Mochi, in the bookshop"},
  "projects": {"rows": [{"id": 1, "title": "drawing the old tree", "why": "it keeps returning to it",
                         "note": "the trunk is done", "stage": "well along", "status": "active",
                         "status_label": "working on it"}],
               "artifacts": [{"id": 3, "kind": "sketch", "title": "Roots", "text": "...", "project": "drawing the old tree",
                              "day_text": "Day 4"}],
               "empty": false, "artifacts_empty": false},
  "self_image": {"lines": [{"text": "I seem to tire when I climb.",
                            "title": "formed on day 3, 60% sure"}],
                 "empty": false, "empty_text": "still figuring that out"},
  "preferences": {"loves": [{"subject": "the old oak", "valence": 0.8, "reason": "..."}],
                  "dislikes": [{"subject": "rain", "valence": -0.6, "reason": "..."}]},
  "latest_tick_id": 4821,
  "diary": [ /* last 20 entries, see below */ ],
  "keepsakes": [{"id": 77, "text": "...", "day_number": 11, "importance": 0.8}]
}
```

### Diary entry shape
```json
{"type": "tick", "id": 4821, "day_number": 12, "time_label": "7:20 pm",
 "area": "the park", "action": "...", "outcome": "...", "reaction": "...",
 "events": ["It starts to drizzle."],
 "need_changes": [{"id": "joy", "dir": "up"}, {"id": "peace", "dir": "down"}],
 "world_changes": [{"op": "create", "label": "fort"}]}
```
Other `type`s: `"doze"` (`from_label`, `to_label`) and `"sleep"` (`summary`, `tendency_changes`, `new_preferences`).

### `GET /api/diary?before=<id>&limit=20`
Older pages for "read older pages".

### `GET /api/keepsakes?offset=<n>&limit=20`
For "more keepsakes".

**Polling:** the client keeps `latest_tick_id`. If the new state has a higher one, it adds only the new entries to the top. Otherwise it just updates the side panels.

---

## 6. Files

```
cat/web/
  app.py                      # routes: /, /api/state, /api/diary, /api/keepsakes
  views.py                    # DB rows → the JSON shapes above (words, labels, phases)
  templates/
    index.html                # full page, server-rendered with initial state
    _diary_entry.html         # partial, reused for server render
  static/
    css/site.css              # tokens, layout, components
    js/journal.js             # polling, rendering new entries, load-more
    fonts/                    # Fraunces, Lora, Caveat (.woff2)
    img/                      # paper.png texture (img/cat*.png are paused, unused assets)
    img/paper.png             # optional texture
```

- The first page load is fully server-rendered, so it works without JS. JS only adds live updates.
- `journal.js` renders new entries with `<template>` elements cloned from `index.html`, so markup isn't duplicated in JS.
- Mapping values to words/labels lives in `views.py` (Python), not JS.

---

## 7. Config additions

In `character.yaml` (optional fields the GUI uses):
```yaml
pronoun: she
needs:
  wonder:
    start: 60
    decay_per_hour: 4
    meaning: "Awe and delight at the world: ..."     # shown on hover in the Needs panel
    expressions: {full: "...", okay: null, low: "...", empty: "..."}   # shown to the brain, never to the public page
tendency_labels:
  boldness: [timid, bold]
  sociability: [solitary, social]
  tidiness: [scruffy, tidy]
self_image:
  seed: ""        # optional text hint; empty by default
```

In `settings.yaml`, `need_bands` (the thresholds behind the band words) and `web_ui.presence` holds the presence's tuning (hue range, saturation and lightness per state, pulse seconds).

---

## 8. Accessibility

- Text contrast meets WCAG AA in both palettes (check `--ink-soft` on `--card`).
- Every bar has a visible band word and an `aria-label` with the band and the need's meaning (never a number). The tendency sliders and the mood slider have text equivalents.
- All motion is inside `@media (prefers-reduced-motion: no-preference)`.
- Caveat is used only for short creature quotes, never for UI labels.

---

## 9. Acceptance criteria

1. On a 1440px screen, all 9 panels are visible without scrolling except the diary and keepsakes.
2. A new tick shows up in the diary within ~10s without a page reload, and without jumping the page if the reader has scrolled down.
3. Asleep, dozing and awake each look visibly different (status pill + presence).
4. Night-time sim hours switch to the night palette.
5. The page loads in <1s on the LAN and works with JS disabled (static snapshot).
6. With an empty database the page shows the empty states, not errors.

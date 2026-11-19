# Web GUI Spec: The Creature's Journal

Spec for the Pi-hosted website. Read `pi-architecture.md` (§13) for where this sits: FastAPI on the Pi, read-only, served on the home network.

**Feel:** cozy storybook, like opening the creature's own journal. Warm paper, ink, soft colours, hand-made touches.
**Target:** desktop first (≥1200px wide), still usable on a narrow screen.
**Mode:** read-only. Visitors watch and can't interact (yet).

---

## 1. Principles

1. **The diary is the heart.** It gets the most space and the centre of the page. Stats support the story, not the other way round.
2. **Words before numbers.** Every bar also has a word ("peckish", "sleepy"). Exact numbers show only on hover.
3. **The page feels alive.** It reflects the sim time (dawn, day, dusk, night), shows sleep and dozing, and new diary entries appear gently.
4. **No framework.** Server-rendered HTML (Jinja2) + one small vanilla JS file that polls `/api/state`. It has to run happily on a Pi 3B+.
5. **Works offline.** Fonts and images are self-hosted. No CDNs, since the site lives on the LAN.

---

## 2. Layout (desktop)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ HEADER   Mochi · the bookshop · Wed, Day 12 · 7:40 pm (evening)   ● awake   │
├───────────────────┬──────────────────────────────────┬───────────────────────┤
│ LEFT  (≈300px)    │ CENTRE  (flex, ≥520px)           │ RIGHT  (≈320px)       │
│                   │                                  │                       │
│ Portrait (cat)    │ Diary                            │ About Mochi           │
│                   │  ─ entries, newest first ─       │  Core traits (chips)  │
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
- **Under 720px:** one column, in this order: header → portrait → mood/energy → needs → diary → about → keepsakes.

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
- Low needs (<35) turn their bar `--accent`. Critical needs (<15) also get a gentle pulse (turned off when `prefers-reduced-motion` is set).

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

### 4.2 Portrait
- Framed image of the cat (`static/img/cat.png`, a render of the 3D model), inside an oval or arched "photo frame" border.
- **Asleep:** image at 70% brightness + a small animated "z z z" drifting up (CSS, reduced-motion safe). Optional `cat-asleep.png` swaps in if it exists.
- **Dozing:** the same dimming, plus a caption "dozing… (brain offline)".
- Caption under the frame: species + current area ("a small grey cat, in the bookshop").
- Later: replace the `<img>` with `<model-viewer>` + `.glb`. Keep the frame element, so only the inner element changes.

### 4.3 Mood and energy
- **Mood:** a large word (Fraunces 22px, `--honey` underline), e.g. *content*, plus a one-line description from the engine ("tired but glowing after the sunset").
- A small **mood dot on a 2D pad** (60×60px): x = valence (−1..1), y = arousal (0..1). The quadrants are faintly labelled on hover (calm/happy, excited, tense, low). This is optional for v1, so it can be cut if fiddly.
- **Energy:** a bar in `--dusk` with a word (*wide awake / fine / tired / exhausted*).

### 4.4 Needs
- One row per need, in config order: `label · word · bar`.
- Bar: 8px tall, rounded, `--rule` track, `--sage` fill (`--accent` when low).
- Trend arrow after the bar: ▲ / ▼ / — compared with the previous tick (only if the change is >2).
- Hover shows "62 / 100, +8 since last tick".
- Words: from the optional `words` in `character.yaml` for each need. Otherwise the defaults are: <15 *desperate*, <35 *low*, <70 *okay*, ≥70 *content*.

### 4.5 Diary (centre)
A journal page. Entries are newest first, grouped under **day headers** ("Wednesday, Day 12").

Each **tick entry**:
```
7:20 pm · the park                                       ⟵ small, ink-soft
climbs the old oak to watch the sunset                   ⟵ action, Lora italic
She scrambles up, scraping a paw, and the sky turns      ⟵ outcome (narrator), Lora
orange over the rooftops.
  "worth the scrape. the sky looked like a sardine."     ⟵ reaction, Caveat, --ink
  fun ▲  energy ▼                                        ⟵ need changes, small chips
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
- **Loves / dislikes:** two short lists, top 5 each by |valence|. Small heart / cross marks drawn as inline SVG (no emoji). Hover shows the reason ("keeps coming back here at dusk").

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
  "creature": {"name": "Mochi", "species": "small grey cat",
               "core_traits": ["curious", "dramatic", "secretly sentimental"]},
  "time": {"sim_time": "2026-11-30T19:40", "day_number": 12,
           "label": "Wednesday, Day 12 · 7:40 pm", "phase": "dusk"},
  "location": {"id": "bookshop", "name": "the bookshop"},
  "status": {"state": "awake", "brain_online": true,
             "brain_last_seen": "2026-11-18T18:02:11", "model": "qwen3:8b"},
  "mood": {"word": "content", "description": "tired but glowing",
           "valence": 0.6, "arousal": 0.3},
  "energy": {"value": 41, "word": "tired"},
  "needs": [{"id": "food", "label": "food", "value": 62, "word": "okay",
             "delta": 8, "low": false, "critical": false}],
  "tendencies": [{"id": "boldness", "low_label": "timid", "high_label": "bold",
                  "value": 0.55, "previous": 0.50}],
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
 "need_changes": [{"id": "fun", "dir": "up"}, {"id": "energy", "dir": "down"}],
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
    img/cat.png               # portrait (+ optional cat-asleep.png)
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
  food: {start: 60, decay: 6, label: "food",
         words: {15: "starving", 35: "peckish", 70: "fed", 101: "full"}}
tendency_labels:
  boldness: [timid, bold]
  sociability: [solitary, social]
  tidiness: [scruffy, tidy]
portrait: img/cat.png
```

---

## 8. Accessibility

- Text contrast meets WCAG AA in both palettes (check `--ink-soft` on `--card`).
- Every bar has a visible word and an `aria-label` with the number. The tendency sliders and mood pad have text equivalents.
- All motion is inside `@media (prefers-reduced-motion: no-preference)`.
- Caveat is used only for short creature quotes, never for UI labels.

---

## 9. Acceptance criteria

1. On a 1440px screen, all 9 panels are visible without scrolling except the diary and keepsakes.
2. A new tick shows up in the diary within ~10s without a page reload, and without jumping the page if the reader has scrolled down.
3. Asleep, dozing and awake each look visibly different (status pill + portrait).
4. Night-time sim hours switch to the night palette.
5. The page loads in <1s on the LAN from the Pi 3B+ and works with JS disabled (static snapshot).
6. With an empty database the page shows the empty states, not errors.

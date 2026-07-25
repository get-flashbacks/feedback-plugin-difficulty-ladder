# Dynamic Difficulty

A feedBack plugin that keeps a song's difficulty matched to how well you're
actually playing it, and shows upcoming sections as a row of glass-filling
difficulty indicators.

## What it does

**Generate missing difficulty ladders**
- Most charts (GP imports, plain single-level sloppaks) have no phrase-level
  Easy/Medium/Hard data at all — `highway.hasPhraseData()` is `false` and the
  mastery slider has nothing to filter. The "⚙️ Generate Difficulties" button
  (shown automatically whenever the current song lacks phrase data) analyzes
  the arrangement's note/chord density, fret complexity, and technique load
  per section, and writes a fresh multi-tier phrase ladder directly into the
  sloppak on disk (`routes.py`'s `/generate` route) — after which the button
  reconnects the highway so the new data streams in immediately.
- A `/generate-library` route does the same as a best-effort sweep over every
  sloppak in the DLC folder, for filling in a whole library at once.
- Never touches an arrangement that already has phrase data unless `force`
  is set — existing hand-authored difficulty ladders are never clobbered.
- This is a fresh implementation against feedBack's own arrangement wire
  format (`lib/song.py`) — it does not port code from, or share a runtime
  with, the Slopsmith arrangement editor's differently-scoped difficulty
  feature; only the general "score note groups, bucket into tiers" heuristic
  approach was used as design inspiration.

**Instrument coverage**

| Instrument | Supported? | Notes |
|---|---|---|
| Guitar / bass (fretted) | ✅ | Fret complexity, span, technique (bend/slide/hammer-on/tremolo/harmonic), density, sustain-ease. |
| Keys / piano | ✅ | Separate pitch-based heuristic (polyphony, hand-span, density, sustain-ease) — keys notes encode `midi = string*24 + fret`, so the fretted heuristic doesn't apply and never runs against them. No fret anchors/hand-shapes generated (the piano renderer doesn't consume them). |
| Drums | ❌ | Drum parts are a `drum_tab.json` pointer, not a `notes`/`chords` file — outside this generator's data model entirely. Detected and skipped cleanly (`unsupported-instrument-drums`), never mis-scored. |

Arrangement type is detected the same way core does: the manifest's
`type` field (`"piano"`/`"keys"`/`"drums"`), falling back to the same
`/^(keys|piano|keyboard|synth)/i` name match the piano-roll chart mode uses.

**Per-song difficulty memory**
- Core persists master-difficulty as a single global value (whatever the
  mastery slider was last set to, for any song). This plugin additionally
  remembers each song's own last-used difficulty (keyed by filename +
  arrangement, in `localStorage`) and restores it whenever you come back to
  that song — so switching between a song you've mastered and one you're
  still working through no longer carries one song's difficulty into the
  other. Captures both manual slider moves and this plugin's own
  auto-adjustments, for songs with phrase-level difficulty data only.

**Live auto-adjustment**
- Reads live per-note hit/miss judgments from whichever note-detection scorer
  is active (e.g. the `note_detect` plugin) via `highway.getNoteStateProvider()`
  — this plugin doesn't score notes itself, it observes an existing scorer.
- Tracks a rolling accuracy average per song section (phrase) and nudges the
  master-difficulty slider (`window.setMastery`) up after a run of clean
  sections, or down after a rough one.
- Only ever changes difficulty at section boundaries — never mid-phrase.
- Stands down the instant you move the difficulty slider yourself. Manual
  action always wins; auto-adjust must be explicitly re-enabled afterward.
- No-ops entirely for songs without a phrase-level difficulty ladder
  (`highway.hasPhraseData() === false` — GP imports, legacy sloppak).

**Glass-filling section HUD**
- Renders upcoming sections in the player as "glasses" — taller glass = a
  harder section (scaled by that section's peak authored difficulty), fill
  level = how much of that section's difficulty range the current
  master-difficulty setting reaches.
- Purely a visualization; can be toggled independently of auto-adjust.

## Requirements

- feedBack core with the `note-detection` capability / `setNoteStateProvider`
  contract (spec 009) and phrase-level difficulty data (feedBack#48).
- A note-detection scorer plugin installed and active for auto-adjust to have
  any signal to react to. Without one, the HUD still renders (using only
  authored difficulty + the manual mastery slider), but auto-adjust has
  nothing to observe and stays idle.

## Settings

Exposed via Settings → Plugins → Dynamic Difficulty:

| Setting | Effect |
|---|---|
| Auto-adjust difficulty | Master on/off switch for automatic `setMastery()` calls. |
| Glass-filling section HUD | Show/hide the in-player glass row. |
| Sensitivity (1-3) | How fast and how far each adjustment moves the slider. |
| Min / Max % | Hard bounds auto-adjust will never cross. |

All settings persist in `localStorage`, prefixed `dynamic_difficulty.`.

## Plugin metadata

| Field | Value |
|-------|-------|
| id | `dynamic_difficulty` |
| version | 0.1.0 |
| category | practice |

## Design notes

This plugin is a fresh, feedBack-native implementation. It does not port code
or assumptions from the Slopsmith arrangement editor's own (differently
scoped) difficulty-generation feature — Slopsmith and feedBack are separate
apps with separate plugin contracts, and the two shouldn't be assumed
compatible just because they share a similar plugin-loader lineage.

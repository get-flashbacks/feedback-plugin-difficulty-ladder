# Dynamic Difficulty

A feedBack plugin that keeps a song's difficulty matched to how well you're
actually playing it, and shows upcoming sections as a row of glass-filling
difficulty indicators.

## What it does

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

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `generateLevels` setting (2-8, default 4): caps how many difficulty tiers "⚙️ Generate
  Difficulties" can give a phrase. Threaded straight into `/generate`'s existing `levels`
  parameter (`routes.py` already accepted and clamped it) — the frontend previously never sent
  it, so every generated ladder silently defaulted to 4 regardless of arrangement complexity.
  Clamping parses the stored value once and defaults only on `NaN` (a raw `|| 4` fallback would
  also misfire on a legitimately parsed `0`), applied consistently at both the settings-page
  render and the `/generate` request site; the range input also carries an accessible
  programmatic label via `aria-labelledby`.

### Changed
- Generated fretted lower tiers now favor beat landmarks, preserve arpeggio roots, and add
  intermediate authored groups when they avoid abrupt hand-position jumps. Long rests are not
  penalized because they provide time to reposition.
- Per-arrangement mastery records now retain the backend's instrument classification while
  remaining compatible with legacy numeric records.
- Drop-resistance settings now reject malformed or non-boolean persisted values and invalidate
  pending downward confirmation after manual mastery or settings changes.
- **Renamed plugin: `dynamic_difficulty` → `difficulty_ladder`** (display name "Dynamic
  Difficulty" → "Difficulty Ladder"), matching the repository's new name
  (`feedback-plugin-difficulty-ladder`). Every reference to the old id has been updated:
  `plugin.json`'s `id`/`name`, the `localStorage` prefix, the player-controls button label, the
  `/api/plugins/difficulty_ladder/...` routes, the `window` settings-changed event name, and the
  diagnostics `schema` key. No migration path from the old `dynamic_difficulty.*` `localStorage`
  keys — existing per-song mastery memory and settings under the old id are not carried forward.
- Auto-adjust no longer acts on the first phrase(s) of a fresh song: a `WARMUP_PHRASES` (2) window
  must be scored first, counted whether or not auto-adjust happens to be enabled yet, so pausing
  and resuming mid-song doesn't reset it.
- A qualifying accuracy streak now moves the mastery slider by `rampStep(th)` per phrase (a full
  `th.step` spread over `RAMP_PHRASES` (3) qualifying phrases) instead of jumping the whole step in
  one call — reads as an adaptation rather than a lurch, and stops early if the signal fades before
  a full step's worth of movement completes. `WARMUP_PHRASES`/`RAMP_PHRASES` are fixed constants for
  now (not settings), named to make a future settings-slider addition straightforward.

### Documented
- `tickScoring()`'s phrase accuracy only counts notes the provider reports as `'hit'`/`'miss'` — a
  sustain reported as `'active'` (currently being held correctly) doesn't contribute to the ratio.
  No behavior change here: this is recorded as a deliberate, revisit-able choice (the note's onset
  is assumed to already resolve to `'hit'`/`'miss'` elsewhere in the provider's lifecycle; `'active'`
  is understood to be an ongoing render signal, not a separate scoring event), in the same
  documented-not-silently-worked-around spirit as the library-card-badge entry above.
- Fretted-instrument ladder generation reworked to read as authored rather than
  mechanically bucketed, based on analysis of a wide sample of existing
  authored difficulty ladders (varied genres, both hand-tuned and
  tool-generated):
  - **Per-phrase adaptive depth**: the `levels` request parameter is now a cap,
    not a fixed depth — each phrase gets its own ladder length (2..cap) from
    how much difficulty *variation* it actually contains, so a simple riff
    gets a short ladder and a technical passage gets a long one instead of
    every phrase in the arrangement sharing one depth (`_phrase_level_count`).
  - **Convex retention curve**: the bottom tier now keeps a much smaller share
    of a phrase's content than a flat percentile split would (was ~1/n_levels
    per tier; now front-loaded via `_RETENTION_CURVE_EXPONENT`), matching how
    authored bottom tiers read as a sparse skeleton rather than a lightly
    trimmed copy.
  - **Explicit technique gating** (`_TECH_GATE_FRAC`): bends, palm mutes,
    vibrato, harmonics, hammer-ons/pull-offs, slides, tremolo, pinch
    harmonics, and taps are now stripped below tuned per-technique ladder
    fractions, instead of only being down-weighted through the composite
    score. Ordered to match how these actually appear in authored ladders:
    bends/mutes earliest, tremolo/pinch-harmonics/taps reserved for the top
    tier or two.
  - **Earlier chord widening**: partial-voicing chords now open up much
    earlier in the ladder (was a single root/partial split near the middle;
    now root-only is a bottom-tier-only state) — authored ladders widen
    chords quickly rather than holding them back.
  - Notes with a dangling `ln` (link-next) flag are now cleared when their
    linked target isn't guaranteed to survive into the same tier.
  - Keys/piano generation is unchanged (fixed depth) — its scoring wasn't
    part of this pass.

### Added
- Library card badge (issue #4): songs with a remembered per-song difficulty now show an
  indicator on their library card via `window.feedBack.libraryCardActions.register(...)`
  (`placement: 'overlay'`), reading the existing `difficulty_ladder.songMastery` map — no new
  storage, no `MutationObserver`. Note: the card-actions capability's `label`/`icon` are static per
  registration rather than computed per song, so the exact saved % isn't renderable as on-card
  text through it today; see `screen.js`'s `registerLibraryCardBadge` for the full writeup.
- `reactionSpeed` setting (1-3, issue #5): a second, independent auto-adjust axis mapping to
  `EMA_ALPHA` (how much weight a single section's result carries in the rolling accuracy average).
  `sensitivity` keeps its existing confidence-threshold/step-size meaning unchanged. Default
  `reactionSpeed` (2) resolves to the same 0.35 `EMA_ALPHA` this plugin always used, so existing
  users see no behavior change unless they touch the new slider.
- Keys/piano difficulty thinning now also collapses octave-doubled voicings
  (two notes of the same pitch class exactly 12 semitones apart) to a single
  note at every reduced tier, on top of the existing outer-voice thinning —
  a doubled root/octave plays identically to a beginner as the single note.
- Per-song difficulty memory: remembers each song's own last-used
  master-difficulty % (`localStorage`, keyed by filename + arrangement) and
  restores it on revisit, instead of inheriting whatever global value the
  previously played song left the slider at. Captures both manual slider
  moves and this plugin's own auto-adjustments by wrapping the shared
  `window.setMastery()` entry point. Songs without phrase-level difficulty
  data are unaffected (nothing to remember).

### Fixed
- Generator ran the fretted guitar/bass fret-complexity heuristic against
  every arrangement indiscriminately, including keys/piano charts — which
  encode `midi = string*24 + fret` (no fretboard at all), so it would have
  silently produced meaningless difficulty tiers instead of erroring. Added
  instrument detection (manifest `type` + the same `/^(keys|piano|keyboard|
  synth)/i` name match core's piano-roll mode uses) with a separate
  pitch/polyphony/hand-span heuristic for keys, and a clean
  `unsupported-instrument-drums` skip for drum-part entries (which point at
  a `drum_tab.json`, not a notes/chords file, and were never reachable by
  the old code path anyway).

### Added
- Phrase-ladder generation (`routes.py`): analyzes note/chord density, fret
  complexity, and technique load per section to build a fresh Easy..Hard
  phrase-level difficulty ladder for sloppak arrangements that don't have
  one, and writes it back into the sloppak (dir or zip form) in place.
  `/api/plugins/difficulty_ladder/generate` (single arrangement) and
  `/generate-library` (best-effort library-wide sweep) routes; a
  "⚙️ Generate Difficulties" player-controls button surfaces the single-song
  path whenever `highway.hasPhraseData()` is false, with a double-submit
  guard and an automatic highway reconnect on success.
- Initial release: live accuracy-driven master-difficulty auto-adjustment
  (reads existing note-detection scorer judgments via
  `highway.getNoteStateProvider()`, commits a hit-rate verdict per phrase
  boundary, nudges `window.setMastery()` up/down within configurable bounds).
- Manual-override detection — auto-adjust stands down the moment the player
  moves the master-difficulty slider themselves.
- Glass-filling section HUD: canvas overlay in the player showing upcoming
  phrases sized by peak authored difficulty and filled to the current
  master-difficulty setting.
- Settings panel: auto-adjust toggle, HUD toggle, sensitivity (1-3), and
  min/max difficulty bounds.

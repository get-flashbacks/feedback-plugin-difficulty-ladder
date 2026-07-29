# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
  `/api/plugins/dynamic_difficulty/generate` (single arrangement) and
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

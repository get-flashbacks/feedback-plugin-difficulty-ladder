# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

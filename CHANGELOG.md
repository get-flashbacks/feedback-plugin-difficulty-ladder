# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

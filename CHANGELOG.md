# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Removed leftover duplication from the scoring-pipeline unification and
  documented the single pipeline (Stage 4-4, #166, part of #139). Factored
  the thrice-repeated abandon-phrase reset (discontinuity step's two branches,
  transition step's reset) into `_abandonScorePhrase`; flagged the now-dead
  `stateForAttempt` hook field vestigial (kept for shape, no new uses);
  corrected stale comments that still described the removed sync layer and
  the pre-migration split copy. `CLAUDE.md` now describes the single
  parameterized machine (`_tickScoreHighway` + per-path commit hooks) instead
  of two pipelines. Guardrails verified: the shared tick does no per-frame
  DOM or `localStorage` work (highway/scorer reads + in-memory ledger only;
  persistence stays debounced, diagnostics contribution unchanged), no
  handler awaits a fetch, subscriptions suspend when the screen is inactive
  (unchanged `startRafLoops`/visibility paths). Settle-point output
  byte-identical before vs after. Version 0.30.2 -> 0.30.3 (internal
  cleanup, no scoring change).
- Migrated the Split Screen scoring path onto the shared implementation and
  deleted the duplicate (Stage 4-3, #165, part of #139). Both pipelines now
  run one parameterized `_tickScoreHighway` (guards, seek handling, phrase
  transitions, 0.6s maturity lookback) against their own state, with per-path
  commit side channels preserved via hooks; the split tick's ~70-line inline
  duplicate and the split enqueue/poll/cursor helpers' second copies are gone.
  The transition step now reports the completed ledger as a snapshot
  (`{ ratio, curPhraseIdx, phraseTotal, phraseHits, phraseJudgments }`) taken
  before the state resets, and the shared commit reads the attempt record off
  that snapshot -- previously the record read the live state AFTER the reset,
  which silently dropped every tick-driven attempt (phraseTotal 0 fails the
  record guard). The old split path recorded inline before resetting, so split
  attempts flowed, but the main tick-driven attempts were being dropped on the
  prior base too; the snapshot restores all of them through the shared step.
  Direct ratio commits (unit tests, settle tool) still read the ledger off the
  state, unchanged. Scoring is unchanged: all 21 Stage 4-1 characterization
  tests and the split finalization tests pass with identical committed
  outcomes; a new test pins the attempt to the COMPLETED phrase.
  Version 0.30.1 -> 0.30.2.
- Unified the main-player and Split Screen scoring paths onto one shared
  judgment-polling/commit state machine (Stage 4-2, #164, part of #139).
  The main player is now the default state: `_mainScore` holds the same
  fields a per-panel split state holds, and the shared steps
  (`_enqueueScoreEvents`, `_pollScorePending`, `_advanceScoreCursors`,
  `_updateScoreDiscontinuity`, `_advanceScorePhrase`, `_commitScoreRatio`)
  run against whichever state they are handed. The old per-field module
  `let` bindings are gone -- `_mainScore` IS the storage -- so the earlier
  sync-both-ways layer (and the class of stale-resync regressions it caused,
  e.g. a pre-seek judgment surviving a forward seek into the commit) is
  deleted, not worked around. The two pipelines differ only in side-channel
  hooks: attempt scope (main always records; split only with a player
  context), mastery streak (main only), manual-override scope (global vs
  panel), write channel (`window.setMastery` + `lastAutoAction` vs panel
  highway), and diagnostics (main only). Split still runs its own tick path
  -- migrating it is Stage 4-3 (#165), explicitly out of scope. No behavior
  change: all 21 Stage 4-1 characterization tests pass unchanged (one
  assertion updated to the new field-presence shape: split states carry a
  null `lastAutoAction` that stays null). New structural tests pin the
  unification itself (default-state field parity, in-place reset, shared-step
  commit/discontinuity/transition behavior, override scoping). Version
  0.30.0 -> 0.30.1 (internal refactor, no scoring change).
- Keys/piano ladders now charge for black-key / awkward-fingering content
  (#178, item 2 of the keys roadmap #175). `_score_groups_keys` previously
  ignored which keys are played, so a passage dense in accidentals cost
  exactly what its transposition to C major did. Each group now pays its
  black-key share (black notes / total notes, pitch classes 1/3/6/8/10)
  scaled to a `_KEYS_BLACK_KEY_MAX_BONUS` cap of 0.02: a single black
  melody note pays the full cap, a 4-voice chord with one black key pays a
  quarter of it. The term is mechanical like the #177 leap term, so it
  raises `cost` and `retention_score` together without touching the
  cost/retention separation. The cap sits below the keys metrical
  `_KEYS_BEAT_VALUE_COEF` (0.025) -- the same guard the #179 key-stability
  weight obeys -- and well under the fretted metrical 0.12 ceiling, so a
  fully-black passage scores at most 0.02/group above its all-white
  transposition: a nudge, not a reordering. Measured: +0.015/group mean on
  a C-major vs Db-major transposition pair (0.75 mean black share x 0.02);
  phrase `difficulty_cost` 0.2082 -> 0.2232 end to end with identical
  top-tier content and nesting; tier-0 content unchanged on mixed textures
  (0-2 notes symdiff over 15-19). No separate transition term: movement
  between onsets is already priced by the #177 leap term, and a
  white->black step at the same pitch distance would otherwise be charged
  twice for one move. The two #179/#178 terms compound on chromatic
  material but model different things (tonal expectation vs the key under
  the finger): a black key that IS stable in the estimated key still pays
  this mechanical cost while earning that stability discount; neither term
  disables the other. Evidence 🔴 weak per #103's convention (beginner-method
  precedent, not a cited finding -- same tier as #103/B10 and C7). Four
  existing keys tests isolate the term (zeroed) so their fixtures' incidental
  black/white content can't move them; the term itself is pinned by a
  formula test, a constant guard, transposition unit + end-to-end tests, a
  cost-vs-retention test, and a chord-share test, each verified to fail
  with the term zeroed.
- Test bootstrap preparation for sibling `scoring.py` module extraction (Stage 2b-3, #154). Added PEP 562 `__getattr__` to `routes.py` for lazy module loading and `load_sibling` context key for tests.

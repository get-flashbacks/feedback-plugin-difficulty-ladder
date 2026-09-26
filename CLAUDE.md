# Difficulty Ladder — AI Agent Guide

Keeps a song's difficulty matched to how well the player is doing, live,
by watching accuracy and nudging note-filtering thresholds. It also
*generates* the difficulty ladders it plays back, via a note-scoring and
tier-assignment pipeline that is the bulk of this repo's actual complexity —
don't assume "gameplay-loop plugin" means the interesting code is all in
`screen.js`.

## Two halves: generator (`routes.py`) vs. controller (`screen.js`)

- **`routes.py` — the generator.** Given a chart, scores every note/chord
  group's *cost* (how hard to play — fretting, technique, coordination,
  hand movement) and *value* (how much to keep — beat strength, phrase
  boundaries, melody shape, key/chord stability), then assigns tiers via
  `_score_groups` → `_tier_thresholds` → `_assign_tiers`, refining the
  lower-tier path with `_refine_lower_tier_path`/`_best_bridge_candidate`.
  Separate scoring paths exist for fretted (`_score_groups`) and keys
  (`_score_groups_keys`) instruments. `_notes_for_level` strips notes/
  techniques back down to what a given tier keeps. This is where nearly
  all of the roadmap work below lives.
- **`screen.js` — the live controller.** Watches accuracy per phrase
  (`commitPhraseResult`) and moves the mastery slider via an EMA with a
  dead band (see `thresholds()` for the actual up/down hit-rate cutoffs
  per Sensitivity setting — there is no simple closed-form settle-point
  formula; see `tools/settle_points.js` and README's "Where auto-adjust
  settles" section if you need to reason about where a setting lands).

**The design rationale for nearly everything in `routes.py` lives in
issue #103** (the science-grounded roadmap: motor learning / music
cognition citations, evidence-confidence tags, and a live status table of
what's shipped vs. still open) — read it before assuming a scoring term's
weight is arbitrary or before proposing a new one; it's probably already
evaluated and either shipped, rejected, or deliberately deferred there.
Also worth knowing before poking around:
- **`README.md`** — user-facing settings table, the "Possible Upgrades"
  design notes for anything not yet built, and (as of the roadmap C2 work)
  a settle-point table with citations for what's actually measured vs.
  heuristic.
- **`PLAYER_CONTEXT.md`** — the multi-player `(session_id, player_id)`
  contract shared with Split Screen, `note_detect`, karaoke, and Section
  Map; read this before touching anything that keys state by player.
- **`INTEGRATION.md`** — Section Map's phrase/glass-fill contract
  (`difficulty:sections-updated`, schema `difficulty_ladder.sections.v2`).
  Note: as of this writing its title line still reads "Integration —
  feedBack-plugin-sectionmap", left over from an earlier doc it was
  adapted from — the content is this repo's real, current contract
  despite the stale title; don't assume it describes a different plugin.
- **`COMPLIANCE.md`** — plugin-spec-v1/best-practices audit history.

## Plugin-spec compliance (see got-feedBack/feedBack-plugin-spec)

- **Idempotent script guard, already in place:** `window.__feedBackDynamicDifficulty`
  singleton at the top of `screen.js`, plus a second guard
  (`window.__ddCardBadgeRegistered`) for the library-card integration.
  The Host may re-execute `screen.js` on plugin reload — any new
  top-level listener/timer/observer needs the same treatment, not a bare
  `addEventListener` outside the guard.
- **Never touch DOM/layout on a per-frame or per-note path.** Read
  settings once and cache them.
- **Don't call `localStorage` inside a gameplay-event handler.** It's a
  synchronous main-thread read/write, so at per-note frequency it's a
  genuine frame-blocking stutter risk. Debounce writes instead.
- **Don't make a gameplay-event handler depend on an awaited
  `fetch(...)`'s result.** `await` yields immediately — it can't block a
  frame — but the handler's continuation runs a frame or more later and
  can race with subsequent notes / act on stale state. Restructure so the
  handler never blocks on the awaited result, rather than treating this
  as the same "stutter" hazard as the `localStorage` case above.
- **Suspend `requestAnimationFrame` / event subscriptions when the
  screen isn't active**, and keep state per-instance, not on a shared
  module global, so a second song/session doesn't inherit stale state.
- **Talk to other plugins through `window.feedBack`'s event bus and the
  capability `claim`/`dispatch`/`release` pipeline** — not by reaching
  into another plugin's globals directly. Unsubscribe from
  `window.feedBack.on(...)` handlers when the screen hides.
- **Folder name must equal `plugin.json`'s `id` exactly** (case-sensitive)
  — a mismatch is a silent skip at plugin discovery.

## Plugin dependencies

`screen.js` reaches directly into two other plugins' globals — `window.createNoteDetector` and `window.feedBackSplitscreen`/`window.slopsmithSplitscreen` — despite the event-bus best practice stated above; this is a real, pre-existing exception, not a hypothetical one, worth being explicit about since there's no manifest-level version enforcement for either:

- **`feedback-plugin-notedetect`** (`window.createNoteDetector`) — verified present as of notedetect **v1.32.0**; the `ownSource`-instance factory and player-context propagation used for split-screen adaptive scoring need higher floors (**v1.15.2**, **v1.33.0** respectively — see #130). Wrapped to register per-panel highways and inspect their state for adaptive difficulty.
- **`feedback-plugin-splitscreen`** (`window.feedBackSplitscreen`, preferred, falling back to the legacy `window.slopsmithSplitscreen` — same `||` pattern used everywhere else in this codebase for the slopsmith→feedBack rename) — verified present as of splitscreen **v1.14.5**. Globals are used to detect and gate whether splitscreen is active before registering per-panel highways; difficulty-ladder maintains the per-panel score state itself, keyed by each highway.

Both are feature-detected and optional — difficulty-ladder works standalone without either installed. See [feedback-plugin-splitscreen#47](https://github.com/get-flashbacks/feedback-plugin-splitscreen/issues/47) for why a `typeof` check alone doesn't catch a downstream contract change (that issue documents two other plugins' integrations going silently dead this way).

**Backend dependency on Chordr, not just frontend ones:** `routes.py`'s
`/group-chords` route (and the opt-in `staged_chords` generator field) call
`app.state.chordr_analyze_chart_chords_v1`, populated only if the Chordr
plugin has registered it — otherwise the route 503s. Auditable floor:
Chordr **v0.5.0**. This is a hard dependency for that one feature, not for
the plugin as a whole, which still loads and generates ordinary ladders
with Chordr absent.

**Core (feedBack) version floors are tracked externally, not restated
here** — issues #129 (generation/tier-semantics floor) and #130 (this
section's peer floors, formalized into a manifest-facing table) are the
live source of truth as of 2026-09-26; check their current state rather
than trusting a specific commit/version cited in an older doc snapshot,
including this one.

## Testing

```bash
node --test                                            # JS: screen.js, settle_points.js (154 tests)
python3 -m pytest tests/test_dd_generation.py \
  -k "not fastapi and not client and not chord_preview and not generate_library and not generate_route"
                                                        # Python generator tests (native deps missing in some sandboxes)
node tools/settle_points.js                            # auto-adjust settle-point simulation, see README
```

## Versioning

Bump `version` in `plugin.json` whenever a change is user-visible — new
capability, a fixed bug that affected real behavior, a changed setting or
UI flow (best-practices rule 4: bump on every release — the version is
used for cache-busting the served JS/CSS URL, so an unbumped version
means users keep getting stale cached files after an update). Patch
(`0.x.y`) for fixes, minor (`0.x.0`) for new features, matching normal
semver-during-0.x conventions.

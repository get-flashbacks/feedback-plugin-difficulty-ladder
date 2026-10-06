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
  formula; use `tools/settle_points.js` to reason about where a setting
  actually lands. A README section documenting its output is proposed in
  PR #132, not yet merged as of this writing — check whether it has
  landed before citing it as existing).

**The design rationale for nearly everything in `routes.py` lives in
issue #103** (the science-grounded roadmap: motor learning / music
cognition citations, evidence-confidence tags, and a live status table of
what's shipped vs. still open) — read it before assuming a scoring term's
weight is arbitrary or before proposing a new one; it's probably already
evaluated and either shipped, rejected, or deliberately deferred there.
Also worth knowing before poking around:
- **`README.md`** — user-facing settings table and the "Possible
  Upgrades" section, which covers both already-implemented features (e.g.
  chord-preview generation, `staged_chords`, both marked as shipped in the
  section itself) and design notes for what's still proposed — don't
  assume everything under that heading is unbuilt. A settle-point
  table/write-up for roadmap C2 is proposed in PR #132 (see above), but
  that PR covers only the Sensitivity dimension of C2's table — its own
  body states the Reaction-speed dimension stays open. Checking whether
  #132 has merged is not the whole check before citing C2 as fully
  documented; confirm which dimension(s) actually
  landed.
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

- **`feedback-plugin-notedetect`** (`window.createNoteDetector`) — split-screen panels are only registered when their detector was built with the `ownSource` instance factory (`screen.js:2119`), so **v1.15.2** is the real gate for that path; **v1.33.0** is a fidelity floor rather than a gate, since older providers fall back to unpersisted, per-highway player identities (see #130). Wrapped to register per-panel highways and inspect their state for adaptive difficulty.
- **`feedback-plugin-splitscreen`** (`window.feedBackSplitscreen`, preferred, falling back to the legacy `window.slopsmithSplitscreen` — same `||` pattern used everywhere else in this codebase for the slopsmith→feedBack rename) — verified present as of splitscreen **v1.14.5**. Globals are used to detect and gate whether splitscreen is active before registering per-panel highways; difficulty-ladder maintains the per-panel score state itself, keyed by each highway.

Both are feature-detected and optional — difficulty-ladder works standalone without either installed. See [feedback-plugin-splitscreen#47](https://github.com/get-flashbacks/feedback-plugin-splitscreen/issues/47) for why a `typeof` check alone doesn't catch a downstream contract change (that issue documents two other plugins' integrations going silently dead this way).

**Backend dependency on Chordr, not just frontend ones:** `routes.py`'s
`analyze_chords` handler (`POST /api/plugins/difficulty_ladder/analyze-chords`
— documented under that path in README.md; the opt-in `staged_chords`
generator field consumes the same capability) calls
`app.state.chordr_analyze_chart_chords_v1`, populated only if the Chordr
plugin has registered it — otherwise the route 503s. Auditable floor:
Chordr **v0.5.0**. This is a hard dependency for that one feature, not for
the plugin as a whole, which still loads and generates ordinary ladders
with Chordr absent.

**Splitscreen's real minimum is unestablished** beyond "checked against
v1.14.5" — issue #130 itself calls that a proxy, not a confirmed floor;
don't treat it as one. Core (feedBack) floors for generation/tier-
semantics live in issue **#129**. All floors on this page are moving
targets under active audit as of 2026-09-26 — issues **#129** and
**#130** are the live source of truth; re-check their current state
rather than trusting any specific commit/version cited in a doc
snapshot, this one included.

## Testing

```bash
node --test                                            # JS: screen.js, settle_points.js (249 tests)
python3 -m pip install -r requirements-test.txt        # Python test environment
python3 -m pytest tests/test_dd_generation.py -q       # Python generator tests — CI's own invocation
node tools/settle_points.js                            # auto-adjust settle-point simulation, see README
```

The pytest file bootstraps `sys.path` from a **sibling `feedBack` checkout**
(`tests/test_dd_generation.py`'s `_PLUGIN_DIR.parent / "feedBack" / "lib"`)
and imports `pydantic` at module scope — without both, the whole file fails
at collection, not per-test. The Python packages are declared in
`requirements-test.txt`; CI installs that manifest and checks out `feedBack`
next to this repo. If your sandbox lacks the sibling checkout or those
dependencies, a filtered subset still exercises the pure-scoring/tier-assignment
code without the FastAPI-route tests:
`-k "not chord_preview and not generate_library and not generate_route"`
deselects 27 of 333 (16 chord-preview-route, 9 generate-library-route, 2
generate-route tests) — a fallback, not the real suite; run the unfiltered
form whenever the prerequisites are available.

## Versioning

Bump `version` in `plugin.json` whenever a change is user-visible — new
capability, a fixed bug that affected real behavior, a changed setting or
UI flow (best-practices rule 4: bump on every release — the version is
used for cache-busting the served JS/CSS URL, so an unbumped version
means users keep getting stale cached files after an update). Patch
(`0.x.y`) for fixes, minor (`0.x.0`) for new features, matching normal
semver-during-0.x conventions.

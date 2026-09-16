# Difficulty Ladder — AI Agent Guide

Keeps a song's difficulty matched to how well the player is doing, live,
by watching accuracy and nudging note-filtering thresholds. Frontend-heavy
gameplay-loop plugin — most of the risk here is per-frame/per-note code,
not backend logic.

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

- **`feedback-plugin-notedetect`** (`window.createNoteDetector`) — verified present as of notedetect **v1.32.0**. Wrapped to register per-panel highways and inspect their state for adaptive difficulty.
- **`feedback-plugin-splitscreen`** (`window.feedBackSplitscreen`, preferred, falling back to the legacy `window.slopsmithSplitscreen` — same `||` pattern used everywhere else in this codebase for the slopsmith→feedBack rename) — verified present as of splitscreen **v1.14.5**. Globals are used to detect and gate whether splitscreen is active before registering per-panel highways; difficulty-ladder maintains the per-panel score state itself, keyed by each highway.

Both are feature-detected and optional — difficulty-ladder works standalone without either installed. See [feedback-plugin-splitscreen#47](https://github.com/get-flashbacks/feedback-plugin-splitscreen/issues/47) for why a `typeof` check alone doesn't catch a downstream contract change (that issue documents two other plugins' integrations going silently dead this way).

## Versioning

Bump `version` in `plugin.json` whenever a change is user-visible — new
capability, a fixed bug that affected real behavior, a changed setting or
UI flow (best-practices rule 4: bump on every release — the version is
used for cache-busting the served JS/CSS URL, so an unbumped version
means users keep getting stale cached files after an update). Patch
(`0.x.y`) for fixes, minor (`0.x.0`) for new features, matching normal
semver-during-0.x conventions.

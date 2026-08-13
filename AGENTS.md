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
  settings once and cache them; don't call `localStorage` or `await
  fetch(...)` synchronously inside a gameplay-event handler — that's a
  per-note stutter waiting to happen. Debounce writes instead.
- **Suspend `requestAnimationFrame` / event subscriptions when the
  screen isn't active**, and keep state per-instance, not on a shared
  module global, so a second song/session doesn't inherit stale state.
- **Talk to other plugins through `window.feedBack`'s event bus and the
  capability `claim`/`dispatch`/`release` pipeline** — not by reaching
  into another plugin's globals directly. Unsubscribe from
  `window.feedBack.on(...)` handlers when the screen hides.
- **Folder name must equal `plugin.json`'s `id` exactly** (case-sensitive)
  — a mismatch is a silent skip at plugin discovery.

## PR Format

If this plugin is a fork of a canonical upstream repo, structure PRs to clarify scope:

### For PRs targeting this fork:
- **Fork-specific changes** — adapt for local use, integrate with local infrastructure, or experiment with features not yet upstreamed
- **Candidate changes** — improvements that could eventually go upstream; mark these clearly so maintainers can cherry-pick or fast-track them

### For changes intended for the canonical repo:
- **Open the PR upstream first** — ensure alignment with the canonical project's standards and roadmap
- **Link both PRs** — reference the upstream PR in your fork PR's description so reviewers see the full context
- **Rebase after merge** — once the upstream PR merges, rebase your fork onto the canonical main to avoid divergence

When in doubt, ask: "Would this benefit the broader community, or is it specific to this deployment?" Upstream changes raise the quality bar for everyone.

## Versioning

Bump `version` in `plugin.json` whenever a change is user-visible — new
capability, a fixed bug that affected real behavior, a changed setting or
UI flow (best-practices rule 4: bump on every release; the plugin manager
uses this to detect updates). Patch (`0.x.y`) for fixes, minor (`0.x.0`)
for new features, matching normal semver-during-0.x conventions.

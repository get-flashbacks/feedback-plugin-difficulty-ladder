# Compliance review — plugin-spec-v1 and best-practices

Tracks issue #9 ("Verify dynamic-difficulty plugin compliance with plugin-spec-v1 and best
practices"). Reviewed against `get-flashbacks/feedBack-plugin-spec` `spec/plugin-spec-v1.md`
(spec) and `spec/best-practices.md` (best practices), both fetched at their `main` HEAD on
2026-07-29, and cross-checked against the actual Host implementation (`feedBack/static/highway.js`,
`feedBack/static/app.js`, and the bundled first-party plugins under `feedBack/plugins/`) where the
spec is intentionally non-normative about exact mechanics.

## Evidence

- `python tools/validate.py` (the spec repo's reference validator, `schemas/plugin.schema.json`)
  run against this plugin checked out under its manifest `id` (at the time, `dynamic_difficulty/`,
  matching how it's actually deployed — this source repo's own directory name differs from the
  plugin `id`, which is expected and immaterial: the repo is cloned/copied into an `id`-named
  directory at install time, same as every other plugin in this org):

  ```
  ok   .../dynamic_difficulty
  ```

  No schema errors, no missing manifest-referenced files, no id mismatch once laid out correctly.
  **Note:** the plugin was subsequently renamed `dynamic_difficulty` → `difficulty_ladder` (see
  `CHANGELOG.md`); this evidence predates that rename and reflects a validator run against the old
  `id`. The schema/contract checklist below has been updated to describe the current `id`, but the
  validator itself has not been re-run since — worth doing before the next compliance sign-off.
- Manual comparison of `screen.js` / `routes.py` / `plugin.json` / `settings.html` against every
  numbered rule in `best-practices.md` and every normative MUST/SHOULD in `plugin-spec-v1.md`.
- Cross-referenced open questions (localStorage settings persistence, script-without-`screen`
  loading, Tailwind utility-class reliance) against the real Host source at
  `D:\Github\SoundLabs\pakr\feedBack`, since the spec is explicit that its client/server *runtime*
  surface (§6.3) is Host-provided and Host-versioned, not itself normative.

## Schema / contract checklist (plugin-spec-v1.md)

- [x] `id` (`difficulty_ladder`) matches `^[a-z0-9][a-z0-9_-]*$` (§4.2).
- [x] Directory-name-equals-`id` rule (§5.2) — satisfied at deploy time; not meaningful for this
      source repo's own folder name (see Evidence above).
- [x] No collision with a bundled plugin id — checked every `plugin.json` under
      `feedBack/plugins/*`; no `difficulty_ladder`.
- [x] Every manifest-referenced file (`script`, `settings.html`, `routes`) exists (validator: pass).
- [x] `routes.py setup(app, context)` does no work at import time — all logic is inside `setup`
      and the request handlers (§7.1, §7.3).
- [x] Routes are namespaced under `/api/plugins/difficulty_ladder/...` (§7.4) — `generate` and
      `generate-library`.
- [x] Handlers are plain `def`, not `async def` — correct, since they do blocking file/zip I/O; the
      Host runs sync handlers in a threadpool (best-practices rule 35).
- [x] `context["log"]` used for all server-side logging; no `print()` (rule 37).
- [x] `setup()` validates (arrangement lookup, sloppak-format check) before registering the
      request handlers themselves; the two routes it registers are unconditional but side-effect
      free until called (rule 6's concern is registering-then-throwing mid-`setup`, which doesn't
      happen here — there are only two `@app.post` calls and nothing between them that can fail).
- [x] Filesystem writes are confined to the resolved sloppak path within the configured DLC root
      (`_resolve_dlc_path`, `safe_join`, zip-member path-traversal guards) — no writes outside the
      Host-designated data tree (§10, rule 53).
- [x] `standards: ["plugin-runtime-idempotent.v1"]` — verified true: `screen.js`'s singleton guard
      (`window.__feedBackDynamicDifficulty.installed`) returns unconditionally on any re-run, so a
      second execution installs zero additional listeners/timers/wrappers (§6.1).
- [x] rAF loops (`tickScoring`, `drawHud`) stop themselves when the player isn't active and restart
      on `highway:visibility` / `visibilitychange` (§6.4, rule 13).
- [x] No `querySelector`/`querySelectorAll`/layout reads inside a per-frame path; DOM refs
      (`_hudCanvas`, `_playerEl`) are cached and only re-resolved via a cheap `.isConnected` check
      (rule 9).
- [x] No `MutationObserver` anywhere in the plugin (rule 10).
- [x] `localStorage` writes are debounced/change-gated (`_onMasteryApplied` skips the write when
      the value is unchanged) and never happen inside `tickScoring`'s or `drawHud`'s per-frame body
      — only from the `setMastery` wrapper, which fires on user/auto-adjust action, not per frame
      (rule 11). Confirmed this is the established, org-wide pattern: `localStorage` is used
      extensively by first-party bundled plugins (`achievements`, `career`, `highway_3d`,
      `keys_highway_3d`, `drum_highway_3d`, `tuner`, ...) for exactly this kind of client-side
      settings/state — not a deviation from Host convention.
- [x] `window.setMastery` wrap (rule 32) always calls through via `.apply(this, arguments)`,
      forwards the return value, is installed exactly once (guarded by `.__ddWrapped`).
- [x] Diagnostics contribution via `window.feedBack.diagnostics.contribute(PLUGIN_ID, {...})` with
      a `schema` field (`difficulty_ladder.v1`) and no secrets/paths/usernames (rule 40/41).
- [x] Player-controls buttons mount into `window.feedBack.ui.playerControlSlot()` (the v3 slot),
      feature-detected via `uiVersion === 'v3'` and the slot function's existence — not a hardcoded
      DOM container (rule 42, spec §6.3's contribution-registries guidance).

## Gaps found and how they were handled

1. ~~**v2 player chrome is not supported.**~~ **Resolved by upstream removal, not by this plugin.**
   As of feedBack core v0.3.0, the v3 UI is the *only* UI — the classic v2 shell and its
   `FEEDBACK_UI`/`/v2` opt-outs are gone entirely (see feedBack core's own `CLAUDE.md`, "v3 UI"
   section). `mountControls()`'s `window.feedBack.uiVersion !== 'v3'` guard is therefore no longer
   a compliance gap against best-practices rule 33 — there is no second shell left to support, so
   the rule's "MUST work in both" condition is vacuously satisfied. The guard itself is harmless
   dead code against any Host running the only UI that exists; left in place rather than stripped
   since removing it wouldn't change behavior on any real Host. No follow-up issue needed.

2. **No `README.md` "target Host version"** callout of the kind the spec's own `full-plugin`
   example carries (rule 54 asks for "which Host version it targets"). Low-risk, mechanical —
   **fixed directly** by adding a short line to `README.md`.

3. **No formal `capabilities` declaration for section-level difficulty.** The plugin doesn't
   declare a capability domain for this surface, which is *correct*, not a gap: best-practices rule
   52 is explicit that declaring a capability you don't service is worse than declaring none. The
   actual cross-plugin surface for section-level difficulty is the plugin's own
   `difficulty:sections-updated` event — see "Section-map integration assumptions" below for the
   current (post-#63) contract; this bullet's original text described a since-superseded
   getters-only architecture. (Separately, unrelated to section difficulty: the plugin *does* now
   participate in the Host's capability dispatch pipeline for player-scoped difficulty commands —
   `fb.capabilities.dispatch('player-difficulty.v1', ...)` — as part of the multi-player work; that
   capability is intentionally narrower than a full section-difficulty domain and doesn't change
   this bullet's conclusion.)

## Best-practices alignment — intentional deviations

- **Settings persist via `localStorage`, not the plugin's own routes.** The spec repo's own
  `examples/full-plugin/settings.html` comment recommends persisting "through the plugin's own
  routes... not by writing files directly from the client." This plugin instead does the
  `localStorage`-with-a-`storage`-event pattern. Reviewed against the actual Host and found this is
  the *dominant* pattern among first-party bundled plugins (`achievements`, `career`,
  `highway_3d`, `keys_highway_3d`, `drum_highway_3d`, `tuner` all do the same) — treated as an
  accepted, intentional deviation from the example's suggestion, not a bug, because it matches
  established Host-ecosystem convention rather than the one narrower example.
- **No shipped `styles`/compiled stylesheet (rule 38).** The two player-control buttons use
  Tailwind-style utility classes (`fb-text`, `fb-primary`, `text-xs`, `px-2`, `py-1`, `rounded`,
  `hover:bg-white/10`, `flex`, `items-center`, `gap-1`) with no `styles` entry in `plugin.json`.
  Verified these are not made-up/arbitrary-value classes: `fb-text` / `fb-primary` are the Host's
  own design-system classes (present in the compiled `feedBack/static/tailwind.min.css`), and the
  remaining classes are common general-purpose utilities the app itself uses throughout
  `player-chrome.js`/`app.js`, so they're already in the Host's compiled sheet. This is a
  deliberate, lower-risk choice (matching the *existing* buttons in the same `playerControlSlot()`
  visually) rather than shipping a redundant/divergent stylesheet — flagged here so a future
  reviewer knows it was a decision, not an oversight, but not changed.

## Section-map integration assumptions (for `feedBack-plugin-sectionmap#1` / this repo's `#8`)

**Superseded by issue #63 (shipped 0.9.11) — the getters-only architecture described below is no
longer how this integration works.** This section originally documented issue #9's acceptance
criteria against the state of the plugins at the time (both reading `window.highway` getters
independently, no dedicated event). That's since changed: `difficulty_ladder` now emits a bespoke
`difficulty:sections-updated` event on `window.feedBack` (fired from
`calculateAndEmitSectionDifficulties()`, debounced), and `section_map` no longer calls
`highway.getPhrases()` / `hasPhraseData()` / `getMastery()` for section-difficulty data at all — it
renders whatever `fillPercentage`/`glassSize` the event payload carries per section, and nothing
else. See [`INTEGRATION.md`](INTEGRATION.md) for the authoritative, current contract (event shape,
fallback/timing behavior, and why the fill formula was unified with `drawHud()`'s own discrete
tiers in the same pass). Kept below for historical context only; do not treat the bullets as
current:

- ~~This plugin does **not** expose a bespoke API or event for section difficulty.~~ It does now
  (`difficulty:sections-updated`).
- ~~`feedBack-plugin-sectionmap` should therefore read `window.highway.getPhrases()` /
  `hasPhraseData()` / `getMastery()` directly...~~ It doesn't anymore; it's a pure event consumer.
- No "difficulty maker" screen exists anywhere in the current feedBack codebase (checked
  `feedBack/plugins/` and `feedBack/static/` for any "maker" screen) — still true, and unaffected
  by the event-contract change above.
- Missing/delayed phrase data must degrade to "no glass HUD," never an error — still the case on
  both sides: this plugin's own `drawHud()` fail-soft path is unchanged, and `section_map` simply
  has nothing to render when no `difficulty:sections-updated` event has fired yet.

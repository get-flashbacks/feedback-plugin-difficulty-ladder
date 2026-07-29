# Compliance review — plugin-spec-v1 and best-practices

Tracks issue #9 ("Verify dynamic-difficulty plugin compliance with plugin-spec-v1 and best
practices"). Reviewed against `get-flashbacks/feedBack-plugin-spec` `spec/plugin-spec-v1.md`
(spec) and `spec/best-practices.md` (best practices), both fetched at their `main` HEAD on
2026-07-29, and cross-checked against the actual Host implementation (`feedBack/static/highway.js`,
`feedBack/static/app.js`, and the bundled first-party plugins under `feedBack/plugins/`) where the
spec is intentionally non-normative about exact mechanics.

## Evidence

- `python tools/validate.py` (the spec repo's reference validator, `schemas/plugin.schema.json`)
  run against this plugin checked out under its manifest `id` (`dynamic_difficulty/`, matching how
  it's actually deployed — this source repo's own directory name differs from the plugin `id`,
  which is expected and immaterial: the repo is cloned/copied into an `id`-named directory at
  install time, same as every other plugin in this org):

  ```
  ok   .../dynamic_difficulty
  ```

  No schema errors, no missing manifest-referenced files, no id mismatch once laid out correctly.
- Manual comparison of `screen.js` / `routes.py` / `plugin.json` / `settings.html` against every
  numbered rule in `best-practices.md` and every normative MUST/SHOULD in `plugin-spec-v1.md`.
- Cross-referenced open questions (localStorage settings persistence, script-without-`screen`
  loading, Tailwind utility-class reliance) against the real Host source at
  `D:\Github\SoundLabs\pakr\feedBack`, since the spec is explicit that its client/server *runtime*
  surface (§6.3) is Host-provided and Host-versioned, not itself normative.

## Schema / contract checklist (plugin-spec-v1.md)

- [x] `id` (`dynamic_difficulty`) matches `^[a-z0-9][a-z0-9_-]*$` (§4.2).
- [x] Directory-name-equals-`id` rule (§5.2) — satisfied at deploy time; not meaningful for this
      source repo's own folder name (see Evidence above).
- [x] No collision with a bundled plugin id — checked every `plugin.json` under
      `feedBack/plugins/*`; no `dynamic_difficulty`.
- [x] Every manifest-referenced file (`script`, `settings.html`, `routes`) exists (validator: pass).
- [x] `routes.py setup(app, context)` does no work at import time — all logic is inside `setup`
      and the request handlers (§7.1, §7.3).
- [x] Routes are namespaced under `/api/plugins/dynamic_difficulty/...` (§7.4) — `generate` and
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
      a `schema` field (`dynamic_difficulty.v1`) and no secrets/paths/usernames (rule 40/41).
- [x] Player-controls buttons mount into `window.feedBack.ui.playerControlSlot()` (the v3 slot),
      feature-detected via `uiVersion === 'v3'` and the slot function's existence — not a hardcoded
      DOM container (rule 42, spec §6.3's contribution-registries guidance).

## Gaps found and how they were handled

1. **v2 player chrome is not supported.** `mountControls()` bails out entirely unless
   `window.feedBack.uiVersion === 'v3'`. Best-practices rule 33 explicitly requires: *"A plugin that
   injects controls into the player MUST work in both [v2 and v3]... verify your plugin in both UIs
   before shipping."* This plugin only ever mounts its Auto-Difficulty / Generate-Difficulties
   controls in v3; on a v2 Host they silently never appear (no console warning, no fallback). The
   HUD canvas itself doesn't depend on `uiVersion` (it looks up `#player` directly) so the
   glass-filling overlay still renders on v2 — only the two control buttons are v3-only.

   This isn't confidently fixable without access to a running v2 Host to verify a mount point (v2's
   control-slot equivalent isn't documented in the spec, which explicitly calls the exact mount API
   Host-versioned, not spec-frozen) — guessing at a v2 DOM selector would itself violate rule 10
   (no injecting into unversioned app-shell markup). **Filed as a follow-up issue** rather than
   guessing: see below.

2. **No `README.md` "target Host version"** callout of the kind the spec's own `full-plugin`
   example carries (rule 54 asks for "which Host version it targets"). Low-risk, mechanical —
   **fixed directly** by adding a short line to `README.md`.

3. **No formal `capabilities` declaration.** The plugin doesn't declare a capability domain, which
   is *correct*, not a gap: it doesn't participate in the capability control plane
   (claim/dispatch/release) today, and best-practices rule 52 is explicit that declaring a
   capability you don't service is worse than declaring none. The actual cross-plugin data surface
   for section-level difficulty is the Host's own `window.highway.getPhrases()` /
   `hasPhraseData()` / `getMastery()` — a Host-mediated read, not something this plugin produces or
   owns. This is the operative fact for `feedBack-plugin-sectionmap#1` / this repo's `#8`: see
   "Section-map integration assumptions" below.

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

Documented explicitly per issue #9's acceptance criteria:

- This plugin does **not** expose a bespoke API or event for section difficulty. All section-level
  difficulty data (`start_time`/`end_time`/`max_difficulty` per phrase) and the live
  master-difficulty value flow through the Host's own `window.highway` object —
  `getPhrases()`, `hasPhraseData()`, `getMastery()` — exactly the same surface this plugin's own
  glass-filling HUD (`screen.js`'s `drawHud()`) consumes.
- `feedBack-plugin-sectionmap` should therefore read `window.highway.getPhrases()` /
  `hasPhraseData()` / `getMastery()` directly, the same way this plugin does, rather than expecting
  `dynamic_difficulty` to forward or re-emit that data itself. There is no ordering dependency
  between the two plugins at runtime — both are independent consumers of Host state — except that
  phrase data must actually exist for a song (via this plugin's `/generate` route, or hand-authored)
  before either plugin's glass HUD has anything non-trivial to show.
- No "difficulty maker" screen exists anywhere in the current feedBack codebase (checked
  `feedBack/plugins/` and `feedBack/static/` for any "maker" screen) — the only place phrase
  difficulty currently surfaces is the player. If sectionmap's maker-UI acceptance criterion refers
  to a screen that doesn't exist yet, that's a sectionmap-side scoping question, not a
  dynamic-difficulty contract gap.
- Missing/delayed phrase data must degrade to "no glass HUD," never an error — this plugin's own
  `drawHud()` already does exactly that (`hasPhraseData()` false → canvas hidden, function returns
  early) and sectionmap's consumer should mirror the same fail-soft check.

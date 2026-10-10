# Song Mastery consolidation and cutover

This document is the consolidation/cutover record for issue #86 (parent #81):
how the separate Slopsmith-era **Song Mastery** plugin's behaviour was absorbed
into Difficulty Ladder, what happens to an existing Song Mastery user's data on
upgrade, how the two plugins coexist, and the gate that must pass before the
old plugin is retired.

It is a **validation artefact**. Its outcome is recorded honestly below,
including the rows that are not yet verifiable here. Per the issue's own
acceptance criteria, **Song Mastery is not declared obsolete by this document**
— the deprecation notice is the last step and is gated on every matrix row
passing.

## 1. What was absorbed

The Song Mastery plugin kept, per song (and per arrangement), the last-used
master difficulty, and showed it as a library-card badge. Both were absorbed:

- **Per-song / per-arrangement difficulty memory** — the legacy map is read
  once and migrated into the canonical progress store (`migrateLegacyData`,
  `screen.js:1895`).
- **Long-term best mastery** — a monotonic, arrangement-scoped `bestMastery`
  written only by live scoring (`_updateBestMastery`, `screen.js:2001`),
  distinct from the current `currentDifficulty` (see the Settings table in
  `README.md`).
- **Library-card badge** — one registration through the Host
  `window.feedBack.libraryCardActions` capability
  (`registerLibraryCardBadge`, `screen.js:798`; action id
  `difficulty_ladder.mastery_badge`).
- **Adaptive-difficulty behaviour** — the Song Mastery controller is replaced
  by Difficulty Ladder's section-aware controller; the settings mapping is
  recorded in §6.

## 2. Storage contract

All keys live in `localStorage`, prefixed `difficulty_ladder.` (`LS_PREFIX`,
`screen.js:16`).

| Key | Schema / shape | Written by | Lifecycle |
|---|---|---|---|
| `difficulty_ladder.progress.v2` | schema `difficulty_ladder.progress.v2`, version 2: `profiles → players → songs → arrangements → instruments → roles → skills` nodes (`currentDifficulty`, `bestMastery`, `updatedAt`) plus a `migrations` map | live scoring, migration | canonical; replaced `songMastery` |
| `difficulty_ladder.phraseAttempts.v2` | schema `difficulty_ladder.phrase_attempts.v2`, version 2 | live scoring | canonical; replaced `phraseAttempts.v1` |
| `difficulty_ladder.player_context.v1` | schema `difficulty_ladder.player_context.v1` | runtime | player-context identity |
| `difficulty_ladder.songMastery` | legacy per-song map, `filename::arrangement` → numeric or `{instrument, role}` record | **read-only at runtime** (`screen.js:97`) | legacy migration source; retained (`source_retained: true`) for rollback |
| `difficulty_ladder.phraseAttempts.v1` | legacy attempt array | read-only left shift during migration (`screen.js:98`) | legacy migration source; retained |
| `difficulty_ladder.autoAdjust`, `.dropResistance`, `.levelUpOnly`, `.sensitivity`, `.downStepRatio`, `.reactionSpeed`, `.minMastery`, `.maxMastery`, `.generateLevels`, `.showDifficultyGuide` | booleans / numbers | settings UI | current settings. `showGlasses` is the renamed predecessor of `showDifficultyGuide` and is migrated forward once (`_resolveDifficultyGuideSetting`, `screen.js:78`) |

Key naming history: the plugin was renamed `dynamic_difficulty` →
`difficulty_ladder`; the old `dynamic_difficulty.*` keys were **not** migrated
(documented breaking change). The legacy *content* keys that **are** migrated
are this plugin's own `songMastery` / `phraseAttempts.v1` maps — the same
per-filename shape the Slopsmith Song Mastery plugin used.

## 3. Migration and cutover marker

One-time, idempotent, claim-once migration (`migrateLegacyData`,
`screen.js:1895`):

- Runs **only** for a confirmed single-player compatibility context
  (`ctx.compatibility_adapter`); explicit concurrent player contexts never take
  this path, so a splitscreen session cannot claim shared legacy data.
- Writes a **cutover marker** on completion:
  `progress.migrations.songMasteryV1 = { completed: true, claimed_by,
  claimed_player_id, source_retained: true, completed_at }`
  (`screen.js:1930`), and `phraseStore.migrations.phraseAttemptsV1`
  (`screen.js:1975`). The marker is the record that the cutover happened and
  which profile/player claimed it; the migration never runs twice.
- Each legacy key is 1:1 with `(song, arrangement)`; a claim marker stamps the
  migrated node with `legacy_claim_player_id`, so only the claiming player can
  read it and another player on the same profile cannot inherit it.
- **Source keys are retained**, never deleted — the rollback path (§7).
- The migration writes `currentDifficulty` only when the target node's
  `currentDifficulty` is still unset, and never writes `bestMastery`.

The plugin also guards against the Host re-executing `screen.js` on reload:
the `plugin-runtime-idempotent.v1` re-hydration guard at the top of
`screen.js`, plus `window.__ddCardBadgeRegistered` for the card badge
(`screen.js:801`), make both the runtime and the badge registration
single-shot. These are the "idempotent cutover guards" for the *plugin
runtime*, distinct from the data cutover marker above.

## 4. Compatibility matrix

| Scenario | Expected outcome | Evidence / status |
|---|---|---|
| **Fresh install** (no Song Mastery, no legacy keys) | No migration fires; stores initialise empty; defaults apply | Code path guarded on absent `songMastery` map; **verified in code** |
| **Song Mastery-only user installs Difficulty Ladder** | Legacy `songMastery` / `phraseAttempts.v1` migrate once into `progress.v2` / `phraseAttempts.v2` under `skill: "overall"`, claimed by the first compatibility context | `migrateLegacyData`; test "legacy song difficulty and phrase attempts migrate once into overall for a ready profile" (`tests/screen.test.js:1411`); **verified for this plugin's legacy shape** |
| **Difficulty Ladder-only user** | No-op; migration marker absent, nothing to read | **verified in code** |
| **Both plugins installed** | See §5 (conflict decision). No cross-writes; duplicate badge prevention is by distinct action id + the idempotency guard | **partially verified** — depends on the Host's card rendering; not run here |
| **Missing Note Detection** (`window.createNoteDetector` absent) | Standalone operation: slider + authored difficulty still work; no live mastery/best-mastery updates, auto-adjust idle, badge shows only remembered values | Documented in README ("Requirements"); README `## Requirements` (line ~489); **verified in code (feature-detected)** |
| **Missing phrase data** | Tier rail renders from authored difficulty + manual mastery only; generation (`/generate`) still available | README peer matrix; **verified in code** |
| **Multiple arrangements** | Each `(song, arrangement)` migrates independently; a live phrase finalization does not leak across arrangements | Tests named in `CHANGELOG.md` (#82/#83 edge cases): "two arrangements of the same song migrating independently", "a live phrase finalization not leaking across arrangements"; **verified** |
| **Private-mode / storage unavailable** | Writes fail quota-safely: bounded exponential retry (`PERSISTENCE_RETRY_MAX = 3`), then give up until the next save; no unbounded loop | `PERSISTENCE_FLUSH_MS` / `PERSISTENCE_RETRY_MAX`, `screen.js:106`; **verified in code** |
| **Host has no `window.feedBack.libraryCardActions`** | Badge registration no-ops cleanly | `registerLibraryCardBadge` capability check; **verified in code** |
| **Cross-plugin upgrade from the *separate* Song Mastery plugin's own storage** | **Unverified here**: the Song Mastery repository is not present in this workspace, so its storage schema cannot be inspected. This is the one row that must be validated before the deprecation step | **OPEN — blocks §8** |

## 5. Conflict behaviour when both plugins are installed

**Decision: read-only compatibility mode.** Difficulty Ladder never reads,
writes, or deletes Song Mastery's storage, and registers its own card action
under a distinct id (`difficulty_ladder.mastery_badge`) behind the idempotency
guard, so it cannot double-register itself. It does not call any Song Mastery
global.

The alternatives were rejected for concrete reasons:

- *Explicit warning* requires a Host surface to warn through; this plugin has
  no such capability, and inventing a DOM warning would violate the "no
  per-frame DOM" guardrails for a load-time-only condition.
- *Disabled duplicate UI* would require this plugin to reach into another
  plugin's registration — the exact cross-plugin global coupling `CLAUDE.md`
  warns against.

Operator guidance: if both plugins render a card badge, disable one badge in
the Host's plugin/library settings. This is documented here and cross-referenced
from `README.md`.

## 6. Song Mastery DD settings mapping (#85)

The legacy controller's settings map onto the current settings with explicit
precedence; no legacy value is silently reinterpreted:

| Song Mastery concept | Difficulty Ladder setting | Precedence / default |
|---|---|---|
| Adaptive on/off | Difficulty mode (`autoAdjust`) | Current value wins; legacy "off" maps to **Standard** |
| Reaction / responsiveness | Reaction speed (`reactionSpeed`, `EMA_ALPHA`) | Current value wins; default 2 reproduces pre-#5 behaviour |
| Confidence / thresholds | Sensitivity (`sensitivity`, 1-3) | Current value wins; step 10/15/20 points |
| Downward easing | Difficulty drop speed (`downStepRatio`) | Current wins; default 1× |
| Isolated-drop damping | Resist isolated difficulty drops (`dropResistance`) | Current wins; default off |
| "Never lower" comfort preference | Level up only (`levelUpOnly`) | Read as a **comfort preference**, default off; must **not** be derived from a migrated value (see `CHANGELOG.md` #111/#85 note) |
| Bounds | Min / Max % (`minMastery`, `maxMastery`) | Current wins |
| Ladder depth | Generate ladder depth cap (`generateLevels`) | Current wins |

Precedence rule: **current Difficulty Ladder settings always win**; a legacy
value is only a *default* for a setting the user has never touched, and the
manual slider always overrides automatic changes. This mapping is documentation
only — the live UI/algorithm reconciliation (and its regression tests) remains
tracked in #85.

## 7. Rollback path

Rollback is non-destructive by construction:

1. Legacy source keys (`difficulty_ladder.songMastery`,
   `difficulty_ladder.phraseAttempts.v1`) are **retained**
   (`source_retained: true`), so reinstalling the prior plugin release finds its
   data intact.
2. The canonical `progress.v2` / `phraseAttempts.v2` stores are **additive**:
   migration writes nodes only where `currentDifficulty` was unset and never
   touches `bestMastery` produced by live scoring.
3. The cutover marker (`migrations.songMasteryV1`) records that migration ran;
   reverting keeps the legacy keys and ignores the v2 tree.

There is no code path that deletes a legacy key or the migrated tree during
normal operation, so rollback needs no special tooling.

## 8. Removal timeline (gated)

Not yet started. The deprecation notice in the Song Mastery repository and the
eventual removal of any shim are gated on:

- [ ] The **cross-plugin upgrade row** in §4 verified against the actual Song
      Mastery storage schema (blocked: that repository is not available here).
- [ ] #84 session-summary item landed and its contract fixtures passing.
- [ ] #85 live settings reconciliation landed with regression tests.
- [ ] The v2 sections event retired only after Section Map migrates to v3
      (#159; depends on feedback-plugin-sectionmap#14 — **still open**).

Until those pass, this plugin keeps the v2 event and the legacy read path, and
**does not declare Song Mastery obsolete**.

## 9. Release checklist

- [ ] `node --test` and `python3 -m pytest tests/` green.
- [ ] `plugin.json` version bumped; `CHANGELOG.md` entry under Unreleased.
- [ ] Migration marker still one-shot: re-running `screen.js` does not
      re-migrate (guard test present).
- [ ] Legacy keys present after migration (`source_retained`).
- [ ] No `dynamic_difficulty.*` key is read.
- [ ] `git grep -i glass` empty apart from the changelog (tier-rail removal,
      #159) — not a gate for this issue.

## 10. Manual QA script

With representative **Lead / Bass / Rhythm / Keys** arrangements:

1. **Fresh install** — confirm no `difficulty_ladder.songMastery` key is
   created, the slider works, and no badge appears on songs with no saved
   difficulty.
2. **Legacy upgrade** — start from a store containing a
   `difficulty_ladder.songMastery` map; open a song in that map in a
   single-player session; confirm the remembered difficulty restores and
   `progress.migrations.songMasteryV1.completed` becomes true.
3. **Multi-arrangement** — confirm each arrangement's value restores
   independently and migrating one does not alter another.
4. **Both installed** — with Song Mastery present, confirm Difficulty Ladder
   writes nothing to Song Mastery's keys and shows at most its own badge.
5. **Missing Note Detection** — remove `window.createNoteDetector`; confirm the
   plugin still loads, the slider and ladder still work, and auto-adjust stays
   idle without errors.
6. **Missing phrase data** — open a song with no phrase data; confirm the tier
   rail renders from authored difficulty only.
7. **Private mode** — run with `localStorage` full/unavailable; confirm the
   bounded retry gives up cleanly with no console loop and no gameplay stall.
8. **Rollback** — reinstall the prior release; confirm the `songMastery` map is
   still present and the prior plugin reads it.

Record the outcome of rows 2–7 per arrangement; any failure blocks §8.

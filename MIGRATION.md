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
  written only through `_updateBestMastery` (`screen.js:2001`) — phrase
  commits, and the first Note Detect session of a node, which alone may write
  from whole-session accuracy — distinct from the current `currentDifficulty`
  (see the Settings table in `README.md`).
- **Library-card badge** — one registration through the Host
  `window.feedBack.libraryCardActions` capability
  (`registerLibraryCardBadge`; action id `difficulty_ladder.mastery_badge`),
  now showing each arrangement's **best mastery** (the cutover's
  `migratedMastery` where one exists) rather than the remembered difficulty.
- **Note Detect session summary** — one `★ N% mastery` row added to the finished
  results card from Note Detect's synchronous `notedetect:session` event
  (`_onNoteDetectSession`, `_injectSessionMasteryRow`), stamped `first play`
  when the session establishes the node's first best.
- **Adaptive-difficulty behaviour** — the Song Mastery controller is replaced
  by Difficulty Ladder's section-aware controller; the settings mapping is
  recorded in §6.

## 2. Storage contract

All keys live in `localStorage`, prefixed `difficulty_ladder.` (`LS_PREFIX`,
`screen.js:16`).

| Key | Schema / shape | Written by | Lifecycle |
|---|---|---|---|
| `difficulty_ladder.progress.v2` | schema `difficulty_ladder.progress.v2`, version 2: `profiles → players → songs → arrangements → instruments → roles → skills` nodes (`currentDifficulty`, `bestMastery`, `migratedMastery`, `updatedAt`) plus a `migrations` map | live scoring, migration | canonical; replaced `songMastery` |
| `difficulty_ladder.phraseAttempts.v2` | schema `difficulty_ladder.phrase_attempts.v2`, version 2 | live scoring | canonical; replaced `phraseAttempts.v1` |
| `difficulty_ladder.songMastery` | legacy per-song map, `filename::arrangement` → numeric or `{mastery, instrument}` record | read by migration (and, only before a profile context exists, by the badge's fallback); **also written** by `_rememberSongInstrument` (`screen.js:545`) and **pruned** by stale-label cleanup `_clearStaleSongInstrument` (`screen.js:618`) | legacy migration source. Not strictly read-only: the difficulty value is migrated+retained, but its `instrument` label is rewritten on contact and an entry judged stale is deleted, so rollback is best-effort, not guaranteed (§7) |
| `difficulty_ladder.phraseAttempts.v1` | legacy attempt array | read-only left shift during migration (`screen.js:98`) | legacy migration source; retained |
| `difficulty_ladder.autoAdjust`, `.dropResistance`, `.levelUpOnly`, `.sensitivity`, `.downStepRatio`, `.reactionSpeed`, `.minMastery`, `.maxMastery`, `.generateLevels`, `.showDifficultyGuide` | booleans / numbers | settings UI | current settings. `showGlasses` is the renamed predecessor of `showDifficultyGuide` and is migrated forward once (`_resolveDifficultyGuideSetting`, `screen.js:78`) |

Not `localStorage` keys (schema/capability identifiers, listed so readers
don't look for persisted data that is never written):
`difficulty_ladder.player_context.v1` (`PLAYER_CONTEXT_SCHEMA`),
`difficulty_ladder.sections.v2` / `.v3`, `difficulty_ladder.difficulty_event.v1`,
`difficulty_ladder.difficulty_request.v1`.

**External source keys (the separate plugin's own storage, never prefixed with
`difficulty_ladder.` and never written by this plugin):**

| Key | Schema / shape | Lifecycle |
|---|---|---|
| `song_mastery_v1` | unscoped / pre-profile Song Mastery store: `filename → { ArrangementName → { difficulty?, mastery? } }` (older flat records: `filename → { difficulty?, mastery? }`) | read-only import source (#215), retained; see §11 |
| `song_mastery_v1_<first 12 of player_hash>` | profile-scoped Song Mastery store, same shape | read-only import source (#215) for the matching profile only, retained; see §11 |
| `song_mastery_dd` | the Song Mastery plugin's Dynamic Difficulty settings blob | read once for the §6 settings import; retained |
| `difficulty_ladder.songMasterySourceImport.v1` (prefixed, written by this plugin) | `{ schema, version, claimed: { <sourceKey>::<profile>::<song>::<arrangement>::<instrument>::<role>: { source_key, difficulty, mastery, at } }, deferred: [ { key, source_key, song_id, arrangement_name, reason } ], updated_at }` | import claim/deferral record (#215); absence never blocks a later import (§11) |

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
- **The marker is written even when the legacy source is absent or empty:** the
  marker write is outside the per-key loop, so a compatibility context that
  runs before any legacy data exists still records completion, and legacy
  entries added later (a restored backup, a re-installed prior plugin) are
  **not** imported — the migration is gated on the marker, not on the source.
  Treat the marker as "the one-time window has passed", not "the source was
  present".
- Each legacy key is 1:1 with `(song, arrangement)`; a claim marker stamps the
  migrated node with `legacy_claim_player_id`, so only the claiming player can
  read it and another player on the same profile cannot inherit it.
- **Source keys are retained** by the migration itself (it never deletes them),
  but they are not frozen afterwards: see §7 for the runtime paths that can
  still rewrite or prune a legacy entry.
- The migration writes `currentDifficulty` only when the target node's
  `currentDifficulty` is still unset, and never writes `bestMastery`. It also
  writes `migratedMastery` — the immutable preserve of the carried-over legacy
  score, the only field the mastery surface may present as the migrated value,
  so a later difficulty change can never masquerade as cutover mastery.

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
| **Both plugins installed** | See §5 (conflict decision). No cross-writes; each plugin registers under its own action id, so the idempotency guard only prevents *this* plugin double-registering — the Host may render **both** badges | **partially verified** — duplicate *across* plugins is not deduplicated by this plugin; depends on the Host's card rendering |
| **Missing Note Detection** (`window.createNoteDetector` absent) | Standalone operation: slider + authored difficulty still work; no live mastery/best-mastery updates, auto-adjust idle, badge shows only remembered values | Documented in README ("Requirements"); README `## Requirements` (line ~489); **verified in code (feature-detected)** |
| **Missing phrase data** (`hasPhraseData()` false) | The tier rail is **hidden** — `_railHighwaySnapshot` returns null on `!hasPhraseData()` (`screen.js:3418`). The manual mastery slider and ladder generation (`/generate`) still work | **verified in code** (corrected: the rail does not fall back to authored difficulty without phrase data) |
| **Multiple arrangements** | Each `(song, arrangement)` migrates independently; a live phrase finalization does not leak across arrangements | Tests named in `CHANGELOG.md` (#82/#83 edge cases): "two arrangements of the same song migrating independently", "a live phrase finalization not leaking across arrangements"; **verified** |
| **Concurrent tabs** | **Best-effort, merge-on-write (not a cross-tab lock):** the debounced `progress.v2` / `phraseAttempts.v2` stores re-read the key at write time and fold the foreign tab's value into the flush instead of overwriting the whole store. The progress merge **adopts no foreign-only branch**, so it neither re-adopts a branch the stale-label repair deleted nor adopts a new branch the other tab wrote during the race window; it only folds in the `migrations` subtree (preserving a foreign `migrations.songMasteryV1` marker) plus two invariant field rules — keep the higher monotonic `bestMastery`, fill an unset `migratedMastery`. The phrase store unions concurrent attempts by identity. Two writes that interleave inside one synchronous task are still last-write-wins (no `navigator.locks` serialization) | `_mergeStores` / `_mergeAttemptLists` / `_PROGRESS_MERGE_FIELD_RULES`, `screen.js` write hook; tests "a whole-store flush keeps a concurrent tab's higher best-mastery and cutover marker", "…never adopts a lower best-mastery", "…does not resurrect a node the stale-label repair deleted", "…unions concurrent phrase attempts" (`tests/screen.test.js`); **mitigated, residual documented** |
| **Private-mode / storage unavailable** | Writes fail quota-safely: bounded exponential retry (`PERSISTENCE_RETRY_MAX = 3`), then give up until the next save; no unbounded loop | `PERSISTENCE_FLUSH_MS` / `PERSISTENCE_RETRY_MAX`, `screen.js:106`; **verified in code** |
| **Host has no `window.feedBack.libraryCardActions`** | Badge registration no-ops cleanly | `registerLibraryCardBadge` capability check; **verified in code** |
| **Cross-plugin upgrade from the *separate* Song Mastery plugin's own storage** | Source keys `song_mastery_v1` / `song_mastery_v1_<hash12>`, shape `filename → { ArrangementName → { difficulty?, mastery? } }` (flat records inventoried separately), read-only; saved difficulty → remembered `currentDifficulty`, best mastery → `migratedMastery`, kept distinct; arrangement names resolved against Host metadata, unresolved/flat records deferred (§11) | Schema pinned from the reference source (`jamesgaiser/feedBack-plugin-song-mastery`, `screen.js` `_playerStorageKey`/`_getMasteryMap`/`_saveArrangementEntry`); importer covered by fixtures in `tests/screen.test.js`; **verified in code + fixtures** — live-Host certification still pending |

## 5. Conflict behaviour when both plugins are installed

**Decision: read-only compatibility mode.** Difficulty Ladder never writes or
deletes any of the *separate Song Mastery plugin's* storage. It reads that
plugin's **score** storage once (the §11 source-store import) and its Dynamic
Difficulty settings once (the §6 import), both through the bare
`song_mastery_v1*` / `song_mastery_dd` keys, and writes only into its own
namespaced keys. It registers
its own card action under a distinct id (`difficulty_ladder.mastery_badge`)
behind the idempotency guard, so it cannot double-register *itself* — it
cannot suppress the other plugin's badge, so if both render one, disable one in
the Host's library settings (below). It does not call any Song Mastery global.
(This is about the other plugin's storage — Difficulty Ladder's *own* legacy
`difficulty_ladder.songMastery` key is still read and, for instrument labels,
written; see §2.)

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

The separate Song Mastery plugin stores its Dynamic Difficulty settings under
the bare `song_mastery_dd` key (its `window._smDD` object). Difficulty Ladder
imports the subset it can express **exactly**, once, on first run
(`_importSongMasteryDDSettings`, `screen.js`), and only where Difficulty Ladder
has **no preference of its own** (the setting is still at its default). The
attempt is recorded under `difficulty_ladder.songMasteryDDImport.v1` as
`{ completed: true, applied: { <setting>: <value>, ... } }` — `applied` is an
audit record of which Difficulty Ladder settings the import initialized (empty
when nothing mapped); only `completed` is consumed, as the run-once guard. So
the import never re-runs or re-applies over a later Difficulty Ladder choice.
The import only changes settings — it creates **no second DD loop**.

"Unset" is decided by **key presence**, not value: an explicit stored
`levelUpOnly: false` or a blank (`null`) movement limit is a real user choice
and is respected, exactly as an explicit `true` or numeric value is.

| `song_mastery_dd` key | Difficulty Ladder setting | Rule |
|---|---|---|
| `allowDecrease: false` | Level up only (`levelUpOnly`) | Set true only from the explicit flag, and only if Level up only is not already on; never inferred from thresholds or presets |
| `defaultDifficulty` | Starting difficulty for new songs (`startDifficulty`, #216) | Only if unset; the numeric value is clamped 0..100 |
| `maxStepsUp` + `stepSize` | Per-play movement limits → Max rise (`maxRisePct`, #217) | Only if unset; `maxStepsUp × stepSize` points (Song Mastery `0` = unlimited → not imported) |
| `maxStepsDown` + `stepSize` | Per-play movement limits → Max fall (`maxFallPct`, #217) | Only if unset; `maxStepsDown × stepSize` points (`0` = unlimited → not imported) |

**Unsupported — documented, not silently reinterpreted:**

| `song_mastery_dd` key | Why it is not imported |
|---|---|
| `enabled` | Song Mastery's adaptive controller is a distinct, clock/rolling-window loop; Difficulty Ladder's section-aware Adaptive mode is a different design. Importing `enabled:true` would silently switch a migrated user into automatic movement, so the mode is left at Difficulty Ladder's own default. |
| `increaseThreshold`, `decreaseThreshold` | Absolute accuracy thresholds with no exact match in the Sensitivity (1-3) presets; do not approximate-and-label-equivalent — pick Sensitivity explicitly if desired. |
| `stepSize` (alone) | Only meaningful here as the multiplier for `maxSteps*`; there is no standalone step-size setting to translate. |
| `confirmWindows`, `cooldownSec`, `gracePeriodSec` | Time/clock-based confirmation and cooldown semantics. Difficulty Ladder adjusts at phrase boundaries with its own confirm/ramp logic; there is no clock-cooldown setting, and no second legacy loop is created. |
| `minNotes`, `maxWindow` | Rolling per-note window parameters internal to Song Mastery's tracker; Difficulty Ladder scores per phrase — no equivalent. |
| `debug` | Song Mastery's debug HUD; use Difficulty Ladder's diagnostics (`contributeDiagnostics`). |

Precedence rule: **current Difficulty Ladder settings always win**; a legacy
value only initializes a setting the user has never set, and the manual slider
always overrides automatic changes. The outcome is deterministic and covered by
the "Song Mastery DD settings import" tests in `tests/screen.test.js`.

## 7. Rollback path

Rollback is **best-effort, not guaranteed** — the migration itself is
non-destructive, but two runtime paths can still change the legacy source:

1. The migration retains the legacy source keys
   (`source_retained: true`) and never deletes them, so for entries it touched
   the difficulty value survives a reinstall of the prior release.
2. The canonical `progress.v2` / `phraseAttempts.v2` stores are **additive**:
   migration writes nodes only where `currentDifficulty` was unset and never
   touches `bestMastery` produced by live scoring; the preserved legacy value
   lives in `migratedMastery` (set once, immutable).
3. The cutover marker (`migrations.songMasteryV1`) records that migration ran;
   reverting keeps the legacy keys and ignores the v2 tree.

Caveats that make rollback best-effort rather than guaranteed:

- `_rememberSongInstrument` (`screen.js:545`) **rewrites** a legacy entry to
  `{ mastery, instrument }` when a song is played, so the record shape can
  change after migration.
- `_clearStaleSongInstrument` (`screen.js:618`) **deletes** a legacy entry
  judged to carry a stale instrument label.
- Neither path is user-facing data loss for the canonical store (the migrated
  v2 node keeps the value), but a reinstall of the prior release, which reads
  only the legacy map, can therefore miss an entry that runtime paths rewrote
  or pruned.

There is no rollback tooling beyond the retained keys; capture a manual backup
of `localStorage` before uninstalling if exact restoration matters.

## 8. Removal timeline (gated)

Not yet started. The deprecation notice in the Song Mastery repository and the
eventual removal of any shim are gated on:

- [x] The **cross-plugin upgrade row** in §4 verified against the actual Song
      Mastery storage schema (pinned reference source) and implemented by #215
      (§11). Live-Host certification remains a separate, non-code step.
- [x] The **empty-source marker** caveat (§3) and the **concurrent-tab**
      whole-store flush (§4) resolved or explicitly accepted and documented.
      The empty-source marker is an accepted, documented design point: it marks
      "the one-time window has passed", not "the source was present", and the
      #215 source-store import (§11) deliberately does not share that limitation
      (an absent source writes no marker). The concurrent-tab flush is mitigated
      by merge-on-write (§4), with the un-lockable simultaneous-write residual
      documented there.
- [x] #84 session-summary item landed and its contract fixtures passing.
- [x] #85 live settings reconciliation landed with regression tests.
- [x] #215 source-store import landed with fixtures against the pinned schema
      (§11); the source keys stay read-only and unmatched records are deferred.
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
   rail is **hidden** (not rendered from authored difficulty) and generation
   still works.
7. **Private mode** — run with `localStorage` full/unavailable; confirm the
   bounded retry gives up cleanly with no console loop and no gameplay stall.
8. **Rollback** — reinstall the prior release; confirm the `songMastery` map is
   still present for entries the runtime did not rewrite/prune and the prior
   plugin reads it (§7).
9. **Empty-source marker** — with no legacy data present, open a song, then
   inject a `songMastery` entry and reload; confirm it is **not** imported
   (§3), documenting the one-time window.
10. **Concurrent tabs** — load the same store in two tabs, write in each, and
    confirm the known whole-store clobber (§4): the later tab's write wins.
11. **Song Mastery source-store import (#215)** — seed `song_mastery_v1_<the
    active profile's hash12>` with an arrangement-name record for the song you
    open next, load that arrangement, and confirm its `difficulty` restores as
    the current difficulty and its `mastery` appears as best mastery, with the
    source key byte-identical afterwards. Open a song whose record uses an
    unresolved/duplicate arrangement name and confirm the record is listed under
    `difficulty_ladder.songMasterySourceImport.v1.deferred` rather than applied.

Record the outcome of rows 2–11 per arrangement; any failure blocks §8.

## 11. Song Mastery source-store import (#215)

Follow-up to #82, which migrated this plugin's **own** legacy
`difficulty_ladder.songMastery` map. #82's migration does not read the separate
Song Mastery plugin's storage, so an actual Song Mastery user upgrading had
their remembered difficulty and best mastery left behind. #215 closes that gap
by importing the source plugin's real store.

**Schema (pinned from the reference source).** Verified against
`jamesgaiser/feedBack-plugin-song-mastery` `screen.js` (`STORAGE_KEY_BASE =
'song_mastery_v1'`, `_playerStorageKey()`, `_getMasteryMap`,
`_saveArrangementEntry`, `_normalizePath`):

| Source | Shape |
|---|---|
| `song_mastery_v1` (unscoped) | `{ filename → { ArrangementName → { difficulty?, mastery? } } }` |
| `song_mastery_v1_<first 12 of player_hash>` | same shape, profile-scoped |
| older flat record | `{ filename → { difficulty?, mastery? } }` — no arrangement key (`_getFileEntry` treats this as unreadable) |

**Import rules (implemented in `_importSongMasterySource`, `screen.js`):**

- **Ownership.** A profile-aware Host reads only its own
  `song_mastery_v1_<hash12>` key and never another profile's. The unscoped
  `song_mastery_v1` store is claimable only by a confirmed single-player
  compatibility context on a Host with **no** profile API — the same rule #82's
  migration follows. A context with no profile identity claims nothing. The
  import runs after profile readiness (`_acceptMainPlayerContext`), never before.
- **Arrangement names are resolved, never guessed.** The source keys are
  display names, so a name is imported only when Host metadata for that song
  matches it to **exactly one** arrangement. An unknown name, or a duplicate
  display name, is deferred; a flat record with no arrangement key is deferred.
- **Difficulty and mastery stay separate.** `difficulty` initializes the
  canonical `currentDifficulty` only when that node has no difficulty yet;
  `mastery` is written once to the immutable `migratedMastery` (what the mastery
  surface and badge present). A saved difficulty is never written as a score,
  and a source mastery never lowers a higher live `bestMastery` (it is stored
  separately; the live best still wins).
- **Idempotent, and re-runnable after a restore.** Each imported record is
  claimed by `(source key, profile, song, arrangement, instrument, role)` under
  `difficulty_ladder.songMasterySourceImport.v1`, so a repeat run never
  duplicates or lowers a value. Unlike #82's one-time marker, an **absent**
  source writes no completion marker — a record restored from a backup later is
  still imported. Deferred records are recorded once (bounded) in the same
  marker's `deferred` list, which is the reviewable recovery path.
- **Source retained; recoverable.** The source keys are never written or
  deleted. Rollback is to delete the prefixed
  `difficulty_ladder.songMasterySourceImport.v1` marker (the imported canonical
  values remain, additive), leaving the untouched source store readable by the
  prior plugin.
- **Malformed/quota-safe.** A non-JSON or non-object source is treated as
  absent; all storage access is already quota-swallowing, and the call site is
  guarded so a legacy import can never block gameplay.

Because the canonical arrangement identity is the Host arrangement **id**, a
record can only be imported for a song whose arrangement metadata the Host can
resolve; records for songs not currently loaded are deferred rather than mapped
by name. This is deliberate: writing by name would place the value where the
play-time (id-keyed) context could never read it, silently losing the data.


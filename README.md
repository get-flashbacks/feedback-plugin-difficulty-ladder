# Difficulty Ladder

A feedBack plugin that keeps a song's difficulty matched to how well you're
actually playing it, and shows upcoming sections as a row of glass-filling
difficulty indicators.

## Multi-player and profile isolation

Difficulty Ladder supports the v1 player-context model used for concurrent
play. Each player has independent profile, song, arrangement, instrument/role,
skill, current difficulty, best mastery, and phrase-attempt state. The model is
designed for at least four Split Screen players and normalizes karaoke players
to `role: "karaoke"` / `instrument: "voice"`; vocal state never shares a
fretted-instrument record.

Saved state is separated as `profile → player → song → arrangement → instrument
→ role → skill`. `player_id` remains part of persistence identity even when two
local players select the same profile. `overall` is the default skill and a missing skill may fall back to it;
skill-specific values never overwrite `overall`. Profile-aware Hosts are gated
until identity is ready, so a pending profile cannot accidentally read or write
another player's progress. See [`PLAYER_CONTEXT.md`](PLAYER_CONTEXT.md) for
the event, capability, `note_detect`, karaoke, Split Screen, and Section Map
contract.

## What it does

**Generate missing difficulty ladders**
- Many charts (GP imports, plain single-level sloppaks) have no phrase-level
  Easy/Medium/Hard data at all — `highway.hasPhraseData()` is `false` and the
  mastery slider has nothing to filter. The "⚙️ Generate Difficulties" button
  (shown automatically whenever the current song lacks phrase data) analyzes
  every *supported* arrangement in the song independently (see instrument
  coverage below). It uses the matching fretted or keys heuristic for that
  arrangement, writes fresh multi-tier phrase ladders directly into the
  sloppak on disk (`routes.py`'s `/generate` route), and explicitly skips
  anything it doesn't support (drums, or another instrument type it doesn't
  recognize) rather than guessing — after which the button reconnects the
  highway so the new data streams in immediately.
- A `/generate-library` route does the same as a best-effort sweep over the
  sloppaks in the DLC folder, up to a `max_songs` cap (default 500, maximum
  2000, via the request body) — it stops there rather than sweeping every
  song in an arbitrarily large library in one call.
- Never touches an arrangement that already has phrase data unless `force`
  is set — existing hand-authored difficulty ladders are never clobbered.
- Both routes report `generated` / `unsupported` / `skipped` / `failed`
  counts in their response, so a caller can tell "nothing to do" (already had
  phrases, too little content) apart from "this generator doesn't support
  that instrument" apart from an outright failure.
- **One difficulty scale per song.** Every phrase is laid out on the same
  `levels`-tier scale (default 4), so a slider position means the same
  difficulty in the verse as in the solo. A note group enters a tier when it
  is easy for this song *and* on a fixed score scale, or when it's among its
  own phrase's easiest (so a hard phrase always keeps a playable skeleton).
  An easy phrase is therefore complete a tier or two in and stops changing,
  while a hard phrase differs at every tier. Tiers where a phrase didn't
  change are dropped from the file, and the remaining levels keep their tier
  numbers (e.g. `difficulty` 0, 1, 3 with `max_difficulty` 3). feedBack core
  maps the slider through those numbers; an older core still plays the
  ladder, just scaled per phrase.
- **Simplifications keep the pitch the note is struck at.** A pre-bend or
  release (struck already bent) is simplified to a fretted note at the bent
  pitch rather than an unbent one; a natural harmonic is only turned into a
  fretted note where that sounds the same pitch (frets 12, 19, 24). Chords
  reduce toward their bass note (the lowest string; string 0 is the lowest),
  which in standard open and barre shapes is usually the root.
- This is a fresh implementation against feedBack's own arrangement wire
  format (`lib/song.py`) — it does not port code from, or share a runtime
  with, the Slopsmith arrangement editor's differently-scoped difficulty
  feature; only the general "score note groups, bucket into tiers" heuristic
  approach was used as design inspiration.

**Instrument coverage**

| Instrument | Supported? | Notes |
|---|---|---|
| Guitar / bass (fretted) | ✅ | Fret complexity, span, string-skip/hand-shape distance, tempo/syncopation-aware density, sustain-ease. Technique scoring covers bend (base + pre-bend/round-trip/shaped-curve difficulty, `bt`/`bnv`), slide, hammer-on/pull-off, tremolo, natural vs. pinch harmonic (scored independently), palm/string mute, vibrato, fret-hand mute, and bass slap/pop (scored independently, slap weighted harder). Timing thresholds (grouping window, beat tolerance, fret-jump window) scale with the song's own tempo instead of fixed wall-clock constants. |
| Keys / piano | ✅ | Separate pitch-based heuristic (polyphony, hand-span, density, sustain-ease) — keys notes encode `midi = string*24 + fret`, so the fretted heuristic doesn't apply and never runs against them. No fret anchors/hand-shapes generated (the piano renderer doesn't consume them). |
| Drums | ❌ | Drum parts are a `drum_tab.json` pointer, not a `notes`/`chords` file — outside this generator's data model entirely. Detected and skipped cleanly (`unsupported-instrument-drums`), never mis-scored. |
| Anything else (vocals, harmony, notation-only, …) | ❌ | An arrangement whose `type` is a specific, non-empty value this generator doesn't recognize is rejected explicitly (`unsupported-instrument-type`) rather than silently treated as fretted. |

Arrangement type is detected via an explicit allowlist, the same convention
core uses: the manifest/arrangement's `type` field — `"piano"`/`"keys"` (or
`"drums"`/`"drum"`, detected and skipped before this generator runs) for
keys, `"lead"`/`"rhythm"`/`"bass"`/`"combo"`/`"chord"`/`"humstrum"` for
fretted — falling back to the same `/^(keys|piano|keyboard|synth)/i` name
match the piano-roll chart mode uses when `type` doesn't say. An **absent or
blank** `type` still defaults to fretted (unchanged from before this fix,
and required for compatibility: the GP importer never sets `type` on
fretted/keys arrangements at all); a **present but unrecognized** `type` is
the case that's now rejected explicitly instead of guessed at.

**Per-song difficulty memory**
- Core persists master-difficulty as a single global value (whatever the
  mastery slider was last set to, for any song). This plugin additionally
  remembers each song's own last-used difficulty (keyed by filename +
  arrangement, in `localStorage`) and restores it whenever you come back to
  that song — so switching between a song you've mastered and one you're
  still working through no longer carries one song's difficulty into the
  other. Captures both manual slider moves and this plugin's own
  auto-adjustments, for songs with phrase-level difficulty data only.

The legacy single-player storage is migrated conservatively into the
`difficulty_ladder.progress.v2` and `difficulty_ladder.phraseAttempts.v2`
stores under `skill: "overall"`; unscoped legacy data is not claimed by
concurrent profiles. A legacy record can be claimed by only one player, and its
claim marker prevents another player sharing that profile from reading it.

**Live auto-adjustment**
- Reads live per-note hit/miss judgments from whichever note-detection scorer
  is active (e.g. the `note_detect` plugin) via `highway.getNoteStateProvider()`
  — this plugin doesn't score notes itself, it observes an existing scorer.
- Tracks a rolling accuracy average per song section (phrase) and nudges the
  master-difficulty slider (`window.setMastery`) up after a run of clean
  sections, or down after a rough one.
- Records monotonic best mastery at phrase finalization as the live difficulty
  percentage multiplied by the phrase hit rate. This never changes the separate
  current-difficulty target.
- Only ever changes difficulty at section boundaries — never mid-phrase.
- Stands down the instant you move the difficulty slider yourself. Manual
  action always wins; auto-adjust must be explicitly re-enabled afterward.
- No-ops entirely for songs without a phrase-level difficulty ladder
  (`highway.hasPhraseData() === false` — GP imports, legacy sloppak).

**Glass-filling section HUD**
- Renders upcoming sections in the player as "glasses" — taller glass = a
  harder section (scaled by that section's peak authored difficulty), fill
  level = how much of that section's difficulty range the current
  master-difficulty setting reaches.
- At the configured maximum mastery, three consecutive phrases at 95%
  accuracy or better light a gold Mastery streak badge. Pausing or entering
  or leaving a split-screen session resets it.
- Purely a visualization; can be toggled independently of auto-adjust.

## Requirements

- Target Host: feedBack core implementing `plugin-spec-v1.md` with the v3 player chrome
  (`window.feedBack.ui.playerControlSlot()`) — the only chrome feedBack core ships as of v0.3.0.
  The player-controls buttons (Auto-Difficulty, Generate Difficulties) mount via a
  `window.feedBack.uiVersion === 'v3'` guard, which is vacuously satisfied on any current Host;
  see `COMPLIANCE.md` for why this is no longer tracked as a gap. The glass-filling HUD itself
  never depended on `uiVersion` and renders regardless.
- feedBack core with the `note-detection` capability / `setNoteStateProvider`
  contract (spec 009) and phrase-level difficulty data (feedBack#48).
- A note-detection scorer plugin installed and active for auto-adjust to have
  any signal to react to. Without one, the HUD still renders (using only
  authored difficulty + the manual mastery slider), but auto-adjust has
  nothing to observe and stays idle.

## Settings

Exposed via Settings → Plugins → Difficulty Ladder:

| Setting | Effect |
|---|---|
| Difficulty mode | **Standard** (default) keeps difficulty fixed — no automatic movement. **Adaptive** enables today's live auto-adjust (`setMastery()` calls driven by accuracy). |
| Resist isolated difficulty drops | Require two consecutive below-threshold sections before a downward adjustment; upward adjustments remain immediate. Off by default. |
| Glass-filling section HUD | Show/hide the in-player glass row. |
| Sensitivity (1-3) | How confident auto-adjust must be (hit-rate thresholds) before it moves the slider, and how big a step it takes. |
| Reaction speed (1-3) | How much weight a single section's result carries in the rolling accuracy average (`EMA_ALPHA`) — independent of Sensitivity. Default (2) reproduces this plugin's original, pre-#5 behavior. |
| Difficulty drop speed (1×-2×) | Multiplies only the downward auto-adjust target so difficulty can ease off faster than it climbs. The default 1× preserves symmetric behavior. |
| Min / Max % | Hard bounds auto-adjust will never cross. |
| Generate ladder depth cap (2-8) | Maximum difficulty tiers "⚙️ Generate Difficulties" can give a phrase when building a ladder for a song that doesn't have one yet — threaded into `/generate`'s existing `levels` parameter. |

**Library card badge** — songs with a remembered per-song difficulty (see above) show a small
indicator on their library card via `window.feedBack.libraryCardActions` (`placement: 'overlay'`,
never a `MutationObserver`). The exact saved percentage is available via the action's click
result/title rather than as on-card text — see this repo's `COMPLIANCE.md`-adjacent note in
`screen.js` (`registerLibraryCardBadge`) for why: the card-actions capability's `label`/`icon` are
static per registration, not computed per song, so a literal "shows N%" on-card text isn't
expressible through it as it exists today.

**Profile baseline card** — the v3 Profile screen shows the average and median
remembered difficulty for fretted and keys arrangements. The card is read-only,
appears only after at least one classified arrangement has a saved mastery, and
does not change the starting difficulty for new songs.

All settings persist in `localStorage`, prefixed `difficulty_ladder.`.

## Plugin metadata

| Field | Value |
|-------|-------|
| id | `difficulty_ladder` |
| version | see [`plugin.json`](plugin.json) — bumped on every user-visible release, kept out of this table so it can't drift out of sync |
| category | practice |

## Possible Upgrades

Design notes only — not yet implemented. Each item should ship as an
independent, opt-in setting so existing behavior doesn't change unless a
user turns it on.

**Also considered:**

- Per-section custom difficulty override — let a section being looped in
  Section Practice carry its own difficulty %, independent of the
  song-wide master-difficulty slider. A genuinely new capability, not
  something the plugin does today; would need to interact cleanly with
  auto-adjust and with any step-practice plugin active for the same
  section, rather than duplicating it. (Measure-aligned fallback phrase
  windows, added for songs with no authored sections, make this more
  practical than it used to be — those songs previously only had blind
  30s chunks to hang a per-section override on.)
- Per-technique player profile driving adaptive difficulty — go beyond a
  passive per-instrument baseline (above) to a persisted, per-technique
  proficiency profile (bends, pinch harmonics, slap/pop, vibrato, etc. —
  the same vocabulary `_tech_score`/`_TECH_GATE_FRAC` now model at
  generation time) built from live hit/miss judgments, then have
  auto-adjust weight a phrase's accuracy signal by how much that phrase
  leans on techniques the player is specifically weak or strong at,
  instead of today's flat hit-rate. A rough pre-bend streak wouldn't need
  to drag down a phrase's difficulty as much as an equally rough streak
  on a technique the player has never struggled with, and vice versa.
  Belongs entirely on the **live** side (auto-adjust's phrase-scoring
  weight in `screen.js`), not as a per-player fork of `/generate`'s
  output — generated ladders are written once into the shared sloppak
  file, not regenerated per player, so the generation heuristic itself
  should stay player-agnostic. The technique data needed to attribute a
  miss already exists on every note the highway streams (`getFilteredNotes()`/
  `getChords()` carry the same `bn`/`ho`/`hp`/`slp`/etc. flags `_tech_score`
  reads), so no new capability contract would be required. Open questions:
  cold-start (a new song or a technique never seen before has no signal
  yet, so needs a neutral default weighting rather than an assumed
  weakness), and how per-technique weighting composes with the existing
  Sensitivity/Reaction speed settings and Resist isolated difficulty
  drops — replacing them, scaling them, or applying only as a tiebreaker.
  Meaningfully larger than the passive baseline above (its own persisted
  store beyond the existing `songMastery` map, plus new live-scoring
  logic) — closer in scope to the per-technique skill profile ruled out
  below, but aimed at adapting difficulty rather than only displaying it.

**Not planned:**

- Forced note-fading or any step-through/note-reveal UI — that's the
  separate `step_mode` plugin's job.
- Real-time (sub-second) adjustment — would require a live per-note event
  stream; no such channel exists today short of reimplementing detection
  judgment.
- Confidence/pitch/timing-weighted scoring — the note-state provider
  contract is hard-capped to `hit`/`active`/`miss`; not something a
  consuming plugin can add unilaterally.
- Generating dozens of levels per phrase — the cap is trivial to raise,
  but the thinning heuristic needs enough distinct note groups per phrase
  to populate that many meaningfully different tiers. (Richer per-
  technique scoring gives whatever depth *is* chosen a more honest score
  spread to work from, but doesn't touch this group-count constraint, so
  the verdict here is unchanged.)
- A full cross-song, per-technique skill profile — a materially larger
  feature than the scoped per-instrument baseline above, and lower value
  for a tool where users pick what to practice. (`_tech_score`/
  `_TECH_GATE_FRAC` now cover the note wire format's full technique
  vocabulary — bend shape, both harmonic types, all four mute/pop/slap
  variants — instead of roughly half of it, so the data-layer cost of
  ever revisiting this decision is lower than it used to be. Still not
  something this plugin builds today.)

## Design notes

This plugin is a fresh, feedBack-native implementation. It does not port code
or assumptions from the Slopsmith arrangement editor's own (differently
scoped) difficulty-generation feature — Slopsmith and feedBack are separate
apps with separate plugin contracts, and the two shouldn't be assumed
compatible just because they share a similar plugin-loader lineage.

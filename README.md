# Difficulty Ladder

A feedBack plugin that keeps a song's difficulty matched to how well you're
actually playing it, and shows upcoming sections as a row of glass-filling
difficulty indicators.

## What it does

**Generate missing difficulty ladders**
- Most charts (GP imports, plain single-level sloppaks) have no phrase-level
  Easy/Medium/Hard data at all — `highway.hasPhraseData()` is `false` and the
  mastery slider has nothing to filter. The "⚙️ Generate Difficulties" button
  (shown automatically whenever the current song lacks phrase data) analyzes
  the arrangement's note/chord density, fret complexity, and technique load
  per section, and writes a fresh multi-tier phrase ladder directly into the
  sloppak on disk (`routes.py`'s `/generate` route) — after which the button
  reconnects the highway so the new data streams in immediately.
- A `/generate-library` route does the same as a best-effort sweep over every
  sloppak in the DLC folder, for filling in a whole library at once.
- Never touches an arrangement that already has phrase data unless `force`
  is set — existing hand-authored difficulty ladders are never clobbered.
- This is a fresh implementation against feedBack's own arrangement wire
  format (`lib/song.py`) — it does not port code from, or share a runtime
  with, the Slopsmith arrangement editor's differently-scoped difficulty
  feature; only the general "score note groups, bucket into tiers" heuristic
  approach was used as design inspiration.

**Instrument coverage**

| Instrument | Supported? | Notes |
|---|---|---|
| Guitar / bass (fretted) | ✅ | Fret complexity, span, technique (bend/slide/hammer-on/tremolo/harmonic), density, sustain-ease. |
| Keys / piano | ✅ | Separate pitch-based heuristic (polyphony, hand-span, density, sustain-ease) — keys notes encode `midi = string*24 + fret`, so the fretted heuristic doesn't apply and never runs against them. No fret anchors/hand-shapes generated (the piano renderer doesn't consume them). |
| Drums | ❌ | Drum parts are a `drum_tab.json` pointer, not a `notes`/`chords` file — outside this generator's data model entirely. Detected and skipped cleanly (`unsupported-instrument-drums`), never mis-scored. |

Arrangement type is detected the same way core does: the manifest's
`type` field (`"piano"`/`"keys"`/`"drums"`), falling back to the same
`/^(keys|piano|keyboard|synth)/i` name match the piano-roll chart mode uses.

**Per-song difficulty memory**
- Core persists master-difficulty as a single global value (whatever the
  mastery slider was last set to, for any song). This plugin additionally
  remembers each song's own last-used difficulty (keyed by filename +
  arrangement, in `localStorage`) and restores it whenever you come back to
  that song — so switching between a song you've mastered and one you're
  still working through no longer carries one song's difficulty into the
  other. Captures both manual slider moves and this plugin's own
  auto-adjustments, for songs with phrase-level difficulty data only.

**Live auto-adjustment**
- Reads live per-note hit/miss judgments from whichever note-detection scorer
  is active (e.g. the `note_detect` plugin) via `highway.getNoteStateProvider()`
  — this plugin doesn't score notes itself, it observes an existing scorer.
- Tracks a rolling accuracy average per song section (phrase) and nudges the
  master-difficulty slider (`window.setMastery`) up after a run of clean
  sections, or down after a rough one.
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
- Purely a visualization; can be toggled independently of auto-adjust.

## Requirements

- Target Host: feedBack core implementing `plugin-spec-v1.md` with the v3 player chrome
  (`window.feedBack.ui.playerControlSlot()`). The player-controls buttons (Auto-Difficulty,
  Generate Difficulties) are v3-only today — see `COMPLIANCE.md` for the tracked v2 gap. The
  glass-filling HUD itself does not depend on `uiVersion` and renders on either chrome.
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
| Auto-adjust difficulty | Master on/off switch for automatic `setMastery()` calls. |
| Glass-filling section HUD | Show/hide the in-player glass row. |
| Sensitivity (1-3) | How confident auto-adjust must be (hit-rate thresholds) before it moves the slider, and how big a step it takes. |
| Reaction speed (1-3) | How much weight a single section's result carries in the rolling accuracy average (`EMA_ALPHA`) — independent of Sensitivity. Default (2) reproduces this plugin's original, pre-#5 behavior. |
| Min / Max % | Hard bounds auto-adjust will never cross. |
| Generate ladder depth cap (2-8) | Maximum difficulty tiers "⚙️ Generate Difficulties" can give a phrase when building a ladder for a song that doesn't have one yet — threaded into `/generate`'s existing `levels` parameter. |

**Library card badge** — songs with a remembered per-song difficulty (see above) show a small
indicator on their library card via `window.feedBack.libraryCardActions` (`placement: 'overlay'`,
never a `MutationObserver`). The exact saved percentage is available via the action's click
result/title rather than as on-card text — see this repo's `COMPLIANCE.md`-adjacent note in
`screen.js` (`registerLibraryCardBadge`) for why: the card-actions capability's `label`/`icon` are
static per registration, not computed per song, so a literal "shows N%" on-card text isn't
expressible through it as it exists today.

All settings persist in `localStorage`, prefixed `difficulty_ladder.`.

## Plugin metadata

| Field | Value |
|-------|-------|
| id | `difficulty_ladder` |
| version | 0.3.0 |
| category | practice |

## Possible Upgrades

Design notes only — not yet implemented. Each item should ship as an
independent, opt-in setting so existing behavior doesn't change unless a
user turns it on.

**Recommended (low effort, self-contained):**

| Upgrade | What it would do |
|---|---|
| Hand-position continuity check in generated ladders | The note-thinning heuristic scores each note group in isolation, with no check on fret distance between consecutive kept notes, and no explicit preference for keeping each group's root note or landing on-beat. A generated lower tier could introduce an awkward hand jump or drop the harmonic/rhythmic anchor the full arrangement doesn't have. Medium effort — touches generation output, needs fixture tests. |
| Punishment-drop resistance | Require a short run of consecutive below-threshold phrases (not just one dip in the rolling average) before a downward step commits, so a single isolated mistake doesn't trigger a full difficulty drop. |

**Also considered:**

- Standard ⇄ Adaptive difficulty mode toggle — an explicit two-mode
  selector (fixed %, no automatic movement vs. today's live auto-adjust)
  that, once in Adaptive, unlocks the finer toggles above as independently
  switchable.
- Per-section custom difficulty override — let a section being looped in
  Section Practice carry its own difficulty %, independent of the
  song-wide master-difficulty slider. A genuinely new capability, not
  something the plugin does today; would need to interact cleanly with
  auto-adjust and with any step-practice plugin active for the same
  section, rather than duplicating it.
- Adaptive baseline per instrument, shown on the Profile screen — a card
  computed from this plugin's own per-song mastery memory, grouped by
  instrument, using the v3 Profile screen's plugin extension point
  (`v3:profile-rendered`). Informational only to start — no change to how
  a brand-new song's starting difficulty is chosen.
- Direction-asymmetric step size (larger downward steps than upward).
- A passive "mastery streak" indicator on the glass HUD when accuracy
  stays high at max difficulty for several consecutive phrases — visual
  only.

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
  to populate that many meaningfully different tiers.
- A full cross-song, per-technique skill profile — a materially larger
  feature than the scoped per-instrument baseline above, and lower value
  for a tool where users pick what to practice.

## Design notes

This plugin is a fresh, feedBack-native implementation. It does not port code
or assumptions from the Slopsmith arrangement editor's own (differently
scoped) difficulty-generation feature — Slopsmith and feedBack are separate
apps with separate plugin contracts, and the two shouldn't be assumed
compatible just because they share a similar plugin-loader lineage.

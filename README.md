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
- **Ladder depth and mechanical difficulty are reported separately.** Each
  generated phrase carries `max_difficulty` (how many tiers the ladder has —
  purely about how much this phrase's content gets thinned) alongside
  `difficulty_cost` (the mean of the internal, purely-mechanical `cost` score
  — fretting/technique/density/sustain/hand-shift, see `_score_groups` — across
  the phrase's full, untiered content). The two can diverge: a short phrase
  built from one hard chord and a long, easy phrase can both collapse to
  `max_difficulty: 0` (nothing to thin) while having very different
  `difficulty_cost`. `difficulty_cost` is unclamped, like the internal `cost`
  field it averages — a phrase stacking several hand-shift/posture penalties
  can read above `1.0`. It's additive: an older reader that doesn't know the
  key simply ignores it. Weights feeding into `cost` remain heuristic (see
  the fretting-movement note below), so treat `difficulty_cost` as a relative
  ranking signal within one song, not a calibrated absolute score.
- **Simplifications keep the pitch the note is struck at.** A pre-bend or
  release (struck already bent) is simplified to a fretted note at the bent
  pitch rather than an unbent one; a natural harmonic is only turned into a
  fretted note where that sounds the same pitch (frets 12, 19, 24). Chords
  reduce toward their bass note (the lowest string; string 0 is the lowest),
  which in standard open and barre shapes is usually the root.
- **Fretting movement is time-aware.** The generator applies a bounded
  `log2(distance / width + 1)` shift cost, discounted by the time between
  onsets, and adds a modest posture cost for wide shapes low on the neck.
  The two-fret target width, time scale and weights are heuristics, not
  measured player thresholds. Bar/phrase join priority is implemented in
  the path-refinement helper but that helper remains disabled in normal
  generation until arrangement-wide refinement can preserve the shared
  tier scale.
- **Technique difficulty counts coordination demand, not only the hardest
  single technique.** `_tech_score`'s max-over-notes term still picks out
  the single hardest technique in a group, but two things it can't see are
  scored on top of it: a group using more than one distinct technique at
  once (a chord mixing a bend and a palm mute, rather than a single
  technique repeated) and a group whose technique differs from the last
  technique-bearing group before it — not necessarily the physically
  adjacent group, so switching from palm-muting into a slide still counts
  as a switch even with a plain-picked note in between (switching costs
  more than repeating the same technique). Both bonuses are 0 for the
  common case — a group using at most one technique, unchanged from the
  last technique-bearing group — so single-technique passages score
  exactly as before. The coordination bonus is deliberately **not**
  re-clamped against `_tech_score`'s own per-note 0–1 range: `_tech_score`
  already saturates at 1.0 the moment a single note stacks enough
  techniques (e.g. a tapped, round-trip bend), so re-clamping the combined
  technique score would silently swallow the coordination bonus in exactly
  the peak-demand passages it exists to score. The per-extra-technique and
  per-switch weights themselves are heuristics, not measured player
  thresholds, and stay modest relative to `_tech_score`'s range so
  coordination compounds an existing demand rather than dominating it.
- **Beat strength is graded, not on/off, and syncopation is metrically
  aware (#103/B2).** A downbeat outranks the mid-bar strong beat (beat 3 of
  a 4-beat bar), which outranks other beats, then eighth- and
  sixteenth-note subdivisions, then off the grid entirely — derived purely
  from the arrangement's own `beats[]` spacing and `measure` flag, no new
  pack data. This ranking feeds the retention `value` term (a downbeat is
  discounted more than a weak subdivision, rather than either getting the
  same flat discount an on-beat note used to), the tie-break when two
  groups land at the same score, and bridge-note selection. Syncopation is
  a Longuet-Higgins & Lee (1984) style measure: a note on a weak position
  is more syncopated when a *stronger* position before the next onset goes
  silent, rather than simply measuring distance to the nearest beat.
  Without a usable downbeat grid (no `measure` data, or no trustworthy
  tempo), both fall back to the pre-#103 on/off behavior exactly — this is
  additive on top of real chart data, not a requirement for it.
- **Phrase starts and endings get retention value (#103/B3).** The first
  and last note group of each *authored* phrase (from the caller's section
  timeline, or the arrangement's own sections) gets a modest push toward
  being kept at low tiers, whenever the rest of the tier's content allows
  it — listeners split music into phrases and remember their boundaries,
  though that keeping boundary notes specifically aids learning is
  inferred, not tested (moderate evidence). Phrases generated from
  8-bar/30s fallback windows (no authored section data) don't get this at
  their internal window edges, since those aren't real musical phrases —
  except the very first window's start and the very last window's end,
  which are always genuine boundaries (the song's own beginning and end)
  regardless of how the windows in between were generated: both
  generated-window builders clamp their final window's end to the song's
  actual duration, so unlike an internal edge, that one really is the
  song's end.
- **A single-note line's turning points get retention value (#103/B5).**
  Beginners remember a melody's rising-and-falling shape before its exact
  intervals (Dowling, 1978) — strong as a perception finding, though that
  keeping the shape specifically aids learning is inferred, not tested
  (moderate evidence). Each note that is a strict local high or low among
  the arrangement's single-note groups, no further than
  `tempo.fret_jump_window_seconds` from its nearest single-note neighbor
  on either side, gets the same modest retention push a downbeat or phrase
  boundary gets, so a thinned tier still traces the melody's contour
  instead of collapsing to whichever notes happened to score hardest.
  Chord and multi-note cluster groups never participate — chord-heavy
  passages are unaffected by construction, not by a special case. Pitch
  direction is approximated from string/fret using standard tuning
  intervals, rather than an exact MIDI pitch, since only the rise/fall
  *direction* between neighboring notes matters for finding a turning
  point, not its precise size. Only the 5-string row is instrument-
  dependent, matching feedBack core's own `base_open_string_midis`
  contract: a 5-string bass is all perfect fourths, while a 5-string
  non-bass borrows the 6-string guitar's low strings instead (one major
  third higher up) — a name/type sniff for "bass" picks the right row,
  mirroring `lib/song.py`'s `arrangement_is_bass()` (a case-insensitive
  "bass" substring in the name, or an exact `type == "bass"`). The sniff
  runs against the EFFECTIVE name/type — a manifest entry's own
  `name`/`type`, when it declares one, takes precedence over the
  embedded arrangement JSON's, same as `tuning` below — so a manifest
  entry authored as a bass part still gets the bass row even when the
  embedded arrangement's own name/type doesn't say so. The offset added
  on top is the EFFECTIVE
  tuning — a manifest entry's own `tuning`, when the pack's manifest
  declares one (and is actually a list; a malformed override is ignored
  rather than raising), takes precedence over the embedded arrangement
  JSON's, mirroring `lib/sloppak.py`'s `load_song()`; this is resolved
  for scoring only, on a copy, and is never written back into the arrangement
  file. Two single-note groups that shouldn't be
  compared at all — either side of an intervening chord section, or the
  end of one authored phrase and the start of an unrelated one — are
  excluded from each other's neighbor comparison when they're more than
  `tempo.fret_jump_window_seconds` apart (the same tempo-relative "long
  enough that this isn't one continuous passage" threshold the fret-jump
  bonus already uses). This is a time-gap heuristic, not true phrase
  awareness: two genuinely separate phrases close enough in time to fall
  inside that window can still be compared as if they were one
  continuous line. A real fix needs the same phrase/section boundaries
  the generator's windowing already has threaded into this scan, which
  is a larger change than this item's effort-S scope covers.
- **Key and chord awareness (#103/B7).** Each phrase gets its own key
  estimate — the classic Krumhansl-Schmuckler algorithm: a duration-weighted
  pitch-class histogram (reusing the same tuning/instrument-aware pitch
  approximation as the B5 melody-turning-point signal) correlated against
  all 24 rotations of the Krumhansl & Kessler (1982) major/minor key
  profiles, keeping the best-fitting rotation. Notes are then ranked by
  tonal stability — tonic > a chord/triad tone > another scale tone >
  chromatic — with chord tones ranked above passing tones of the same
  category when a chord is sounding (looked up from the arrangement's own
  chord track for non-chord groups — a single note or run that lands
  under a sustained chord picks up its notes' pitch classes, not just a
  chord group's own self-evidently-chord-tone constituents), and the
  group containing the most stable note gets a modest retention push.
  That push is deliberately weighted *below* beat/metrical strength's
  (#103/B2) and the melody-shape bonus's (#103/B5) weight — a heuristic
  key estimate is a weaker signal than measured beat position, so it
  shouldn't be able to outrank it — and is applied per section BEFORE the
  shared tier scale is frozen (the same point B2's beat-value term and
  B5's melody bonus already apply at), so it participates in tier-cutoff
  construction instead of re-labeling an already-frozen scale and
  silently collapsing a tier on ordinary tonal material. A section whose
  best-fit correlation falls below a fixed threshold skips the weighting
  entirely rather than confidently ranking notes against a key estimate
  the data doesn't support — despite the "poor tonal fit" framing, that
  threshold mainly rejects near-uniform/atonal pitch-class content
  (whole-tone, fully chromatic); ordinary diatonic, modal, and blues
  material all correlate well above it and get the weighting like any
  other tonal section. Separately, chord and arpeggio
  reduction (the bottom-tier "pick one note to represent this chord" step)
  now try a real harmonic root — parsed from the matched authored
  `ChordTemplate`'s `name` (e.g. "Am7" → A, "G/B" → G; only the root letter
  before any slash is used, since a slash chord's bass isn't its root) —
  before falling back to the pre-existing lowest-string-index heuristic
  when the name doesn't parse or no template matched.
- This is a fresh implementation against feedBack's own arrangement wire
  format (`lib/song.py`) — it does not port code from, or share a runtime
  with, the Slopsmith arrangement editor's differently-scoped difficulty
  feature; only the general "score note groups, bucket into tiers" heuristic
  approach was used as design inspiration.

**Instrument coverage**

| Instrument | Supported? | Notes |
|---|---|---|
| Guitar / bass (fretted) | ✅ | Fret complexity, low-position stretch posture, time-aware hand shifts, string-skip/hand-shape distance, tempo/syncopation-aware density, sustain-ease. Technique scoring covers bend (base + pre-bend/round-trip/shaped-curve difficulty, `bt`/`bnv`), slide, hammer-on/pull-off, tremolo, natural vs. pinch harmonic (scored independently), palm/string mute, vibrato, fret-hand mute, and bass slap/pop (scored independently, slap weighted harder), plus a coordination bonus for a group using more than one distinct technique at once or switching technique from the group before (a chord mixing a bend and a palm mute scores above either alone; see "Technique difficulty counts coordination demand" below). Timing thresholds (grouping window, beat tolerance, movement time scale) scale with the song's own tempo instead of fixed wall-clock constants. |
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
- Stands down when the legacy, originless Host API reports an unexpected
  difficulty change. This conservatively protects manual slider changes, but
  Host restoration or another plugin can look identical because
  `window.setMastery` supplies no source metadata. Auto-adjust must be
  explicitly re-enabled afterward.
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

`staged_chords` (#103/B10) is an additional opt-in `/generate` and
`/generate-library` request field — `false` by default — not yet exposed
as a settings.html checkbox. Pass it explicitly in the request body to
enable the chord-landmark bottom tier (see "Possible Upgrades" below for
what it does); the acceptance criteria for a full UI toggle is broader
validation against real (not just one) arrangements first. Like
`levels`, it only takes effect while generating: it has no effect on an
arrangement that already has phrases unless the request also sets
`force`, matching `/generate`'s existing regenerate-only-on-request
behavior.

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

Chordr now supplies a read-only grouping preview at
`POST /api/plugins/difficulty_ladder/analyze-chords` with body
`{"filename":"Song.feedpak","arrangement_index":0}`. It reports each chord
event's Chordr identity and parent group; a played subset of the preceding
full chord stays in that chord's group. Chordr must be active for this route.
The preview does not rewrite the pack or change generated tiers. Inspect its
grouping on real arrangements before enabling a chord-led generator stage.

Design notes for the general generator. **The chord-landmark stage below is
now implemented** (#103/B10, `staged_chords` — see the Settings section),
off by default; everything else in this section (strum-onset/voicing/
technique staging beyond what the existing continuous difficulty curve
already does, and the "enable a chord-led stage by default" step — see
issue #121's acceptance criteria) remains a design note, not implemented.
The one-off *Bring Me to Life* preview in the library tests Chordr grouping
on that song only — not the broad, multi-arrangement validation #121
requires before any of this could default to on. Each general feature
should ship as an independent, opt-in setting so existing behavior doesn't
change unless a user turns it on.

**Musically staged fretted ladders:**

- Add meaningful stages to the current density, voicing, and technique
  progression rather than replacing the existing generator or silently
  changing existing packs. More tiers (within the supported depth cap), or
  intermediate tiers between current stages, are appropriate when each tier
  makes a distinct, playable change. Preserve the full authored chart at the
  highest tier; do not preserve a defective generated tier merely to keep its
  number.
- **Implemented (#103/B10, opt-in via `staged_chords`):** in a chord-led
  passage, the bottom tier now retains the harmonic path: one **full
  chord landmark** per identified chord identity, dropping its repeated
  strumming pattern entirely — not just thinning each repeat's voicing,
  which the pre-existing per-group reduction already did on its own.
  `_staged_chord_drop_ids` prefers the identity's longest-sustained
  occurrence, among the occurrences already at the bottom tier, as the
  landmark (usually the first, but not assumed to be — measured, not
  guessed) — scoped to `level == 0` rather than every occurrence in the
  whole phrase, since picking a landmark from a higher-tier occurrence
  could drop the only bottom-tier occurrence of that identity with
  nothing to replace it there (caught in PR #127 review). A chord group
  that resolves to a named identity but carries no notes (a `notes: []`
  chord, reachable from a GP import with an out-of-range chord id or a
  fully-muted template) is excluded from landmark/resolution candidacy
  entirely, so it can never silently occupy an identity's kept slot while
  contributing nothing to the tier (also caught in review). Separately,
  always protects the phrase's own final, note-bearing bottom-tier chord
  group from being dropped regardless of duration (the resolution-
  protection rule below). Unnamed/unidentified partial voicings (no
  matched `ChordTemplate`, a template with no `name`, or a non-string
  `name` from a hand-edited pack) are never collapsed —
  `_resolvable_chord_identity` requires a positively-identified parent
  chord before a group is even a drop candidate. A short, difficult
  passing/transition chord (the brief Bmadd11 in *So Far Away* Rhythm is
  the example) is a drop candidate exactly when it's the EARLIER of two
  bottom-tier occurrences of the same identity and a later one happens to
  sustain longer — the earlier, shorter one is what the landmark rule
  drops in that case, which includes this chord itself when it's the
  earlier occurrence. This lands only the bottom tier's group selection;
  every tier above it shows every occurrence, going through the same
  voicing/technique reduction as when the setting is off.
  **Known approximation:** resolution protection is positional (the
  phrase's last bottom-tier chord group), not harmonic — a short mid-
  phrase cadence resolution elsewhere in the phrase isn't specially
  protected, since the wire data has no way to express "this is a
  resolution." Flagged as a residual risk to settle before this ever gets
  a settings.html checkbox, not a defect in this PR's own scope.
- In a rhythm-led passage such as *Bring Me to Life* guitar, use the staged
  order: chord landmarks; then **every authored strum onset** played as one
  note; then existing easy/intermediate voicing material; then complete
  rhythm and fingering without bends, vibrato, or harmonic effects; then
  vibrato; then bends; then the unchanged authored performance (which adds
  harmonics). Omit a technique stage when no notes in that phrase use it.
  The one-note rhythm stage must preserve onset timing; it must not thin
  away the groove. Keep full landmark chords at their selected onsets in
  later rhythm stages so the ladder does not discard earlier content.
- Bend-free practice must preserve the pitch struck at note onset: ordinary
  bend-ups/round trips can lose the bend, while a pre-bend or release struck
  at the peak needs an equivalent higher fret when one exists. If no fret
  can produce that onset pitch, keep the authored bend instead of making a
  wrong-pitched easy note. Vibrato may be removed independently without
  changing the onset pitch. In the *Bring Me to Life* preview, Lead has
  vibrato but no bends; Rhythm has both, so only Rhythm receives a distinct
  bend-introduction tier.
- The harmonic-fingering stage removes only natural/pinch harmonic effects
  (`hm`/`hp`), retaining their authored string, fret, and onset. A different
  sounding pitch is acceptable **at this specific practice stage** so the
  player can learn the fingering before the effect. This does not relax the
  pitch-preserving rule for bend, slide, or other simplifications, and the
  highest tier always retains the original harmonic effects.
- Whether silent/muted `X` strums should form their own stage remains open;
  do not automatically drop them from an existing ladder on this proposal's
  authority. Chordr output, group boundaries, resolution protection, and
  the audible/playable result need fixtures and review before implementation.

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

# Difficulty Ladder

A feedBack plugin that keeps a song's difficulty matched to how well you're
actually playing it, and shows upcoming sections as a compact, accessible
tier rail (lit segments for the current tier, an outlined segment for the
full-detail tier).

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
  is set. Every ladder this plugin writes is stamped with `x_difficulty_ladder`,
  including the marker schema version, the Difficulty Ladder plugin version,
  level count, and generation time. Existing markers from before plugin-version
  tracking still identify the generator, but do not identify its release.
- Each library card's menu offers **Regenerate difficulty ladder**
  (`difficulty_ladder.regenerate`, `placement: 'menu'`, destructive). Before any
  write, it previews the song's existing ladders and confirms the overwrite.
  The prompt identifies Difficulty Ladder-generated ladders and their recorded
  plugin version. A missing or invalid marker is shown as **unknown**—possibly
  handmade or generated before provenance tracking—and requires explicit
  confirmation (`overwrite_authored: true`) before `/generate` can replace it.
  Drums and other unsupported arrangements are skipped, and the highway
  reconnects only if the regenerated song is currently open in the player.
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
  approximation as the B5 melody-turning-point signal on the fretted path;
  reading the note's real MIDI pitch directly on the keys path, which needs
  none of the fretted version's pitch-approximation) correlated against
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
- **Keys/piano generator parity (#103/B8).** `_score_groups_keys` now
  applies the same graded beat-strength retention term (#103/B2's
  `_beat_value`), melody-turning-point retention (#103/B5), and per-section
  key-stability discount (#103/B7, #179) the fretted
  path already had — the keys path needs none of the fretted version's
  tuning/string pitch-approximation, since a keys note already carries a
  real MIDI pitch (`_note_midi_keys`). The terms use keys-SPECIFIC
  coefficients (`_KEYS_BEAT_VALUE_COEF`/`_KEYS_MELODY_TURNING_BONUS`,
  0.025 each, `_KEYS_KEY_STABILITY_RETENTION_BONUS`, 0.018), not the fretted
  path's 0.12/0.12/0.08 — keys' `cost` model has
  a much narrower dynamic range for melodic content (measured spread
  ~0.10 across an entire passage vs. the fretted path's typical 0.3-0.6),
  so reusing the fretted weight verbatim would let one discount alone
  outweigh the whole cost signal it's supposed to merely nudge. Phrase-
  boundary retention (#103/B3) already applied to keys automatically,
  since it's computed on shared `phrase_groups` code outside the
  fretted/keys branch. Separately,
  `_notes_for_level_keys`'s chord-voicing reduction now grows by a smooth
  per-tier budget for chords wider than 3 notes — a fixed voice-add order
  (outer voices first, then alternating inward from both ends) is
  truncated to a count that rises with tier level, instead of the old
  fixed three-step jump (outer only → outer + one middle voice →
  everything) regardless of how many tiers the ladder has. This is a
  note-COUNT budget, not the mechanical-cost budget Nakamura & Yoshii
  (2018) frame piano reduction around (a group's cost terms — polyphony,
  span, density, speed, sustain — aren't attributed per note), a
  simplification declared in the code rather than left implicit. A 2-3
  note chord has no real room for a graded budget and keeps its
  pre-#103/B8 behavior exactly (outer only at the bottom tier, everything
  above it). Octave-duplicate collapsing (`_collapse_octave_duplicates`)
  runs in the fixed voice-ADD order, not pitch-sorted order — sorting by
  pitch first would make collision priority pitch order instead of add
  order, letting a harder tier's newly-added lower voice win an octave
  collision and drop an upper voice an easier tier had already exposed,
  which is neither a superset nor a subset and breaks the #99 nesting
  guarantee this function exists to provide (caught in PR #126 review on
  a 7-voice voicing with an octave-adjacent interior pair). Every REDUCED
  tier (`level < max_level`) is still pitch-sorted for presentation, after
  the collapse decides which voices survive — the top tier skips
  reduction entirely and returns the chord's own authored note order
  unchanged, same as always.
- **Keys/piano hand movement is charged per hand (#177).** `_score_groups_keys`
  applies the keys counterpart of the fretted path's time-aware fret-jump cost
  (`_fitts_shift_bonus`, #19): a bounded `log2(distance / 5 + 1)` index,
  discounted by the time available, capped at 0.05 and zeroed past
  `tempo.fret_jump_window_seconds` — the same tempo-relative window the
  fretted shift bonus and the melody-turning-point term already use, rather
  than a second one. Each group is anchored at the MEAN of its MIDI pitches
  (keys notes carry exact pitch, so a hand's position is best read as the
  centre of what it is holding), and measured against the previous group of
  the SAME hand: `_group_notes_keys` tags each notes-bearing group
  `"lower"`/`"upper"`, taken from `_split_keys_hands` for a split onset and
  from the same skyline register judgement that picks the melody for an
  unsplit one, so scoring the two halves of one onset against each other
  cannot charge a phantom leap. That label is a register guess, so a group with
  no same-hand predecessor falls back to the group immediately before it (the
  fallback is off for the halves of a split onset, where having no predecessor
  really does mean the hand has not played yet); without it a single melodic
  line whose contour straddles the median would charge 0.0000 for every note.
  A 5-semitone target width (a hand shifts for free within about a fourth),
  a 19-semitone reference (a twelfth, where it saturates) and a 0.05 cap —
  about half the fretted 0.10, for the narrower keys `cost` range, but still
  twice `_KEYS_BEAT_VALUE_COEF` — are heuristics, not measured player
  thresholds. The term is keys-only; the fretted path's own shift bonus is
  unchanged.
- **Keys/piano black-key content costs more (#178, item 2 of the keys roadmap
  #175).** `_score_groups_keys` now charges each group its black-key share
  (black notes / total notes over pitch classes 1/3/6/8/10), scaled to a
  `_KEYS_BLACK_KEY_MAX_BONUS` cap of 0.02 — a single black melody note pays
  the full cap, a 4-voice chord with one black key pays a quarter of it.
  Mechanical like the leap term above, so it raises `cost` and
  `retention_score` together. The cap sits below the keys metrical
  `_KEYS_BEAT_VALUE_COEF` (0.025) and well under the fretted 0.12 ceiling,
  so a fully-black passage scores at most 0.02/group above its all-white
  transposition (measured +0.015/group mean on a C-major vs Db-major pair).
  No separate white→black transition term — movement between onsets is
  already priced by the leap term. Evidence is weak (beginner-method
  precedent, 🔴 per #103's convention); the term compounds with, but never
  disables, #179's key-stability discount.
- **Keys/piano bottom tier has a minimum musical density (#181, item 6 of
  the keys roadmap #175).** The proportional tier floor in `_assign_tiers`
  guarantees tier 0 a share of the phrase's GROUPS (~15%), but a keys group
  is one onset — often a whole chord — so on a dense chordal passage that
  share materialized as only 1-2 NOTES against 20+ at the top tier: a
  learner at the easiest setting had almost nothing to play (measured on a
  2-bar phrase of quarter-note 4-voice chords: tier 0 held 2-3 notes while
  the top tier held 32). The keys path now applies a per-phrase floor after
  tier assignment: a strong-beat skeleton (one group at tier 0 covering
  every grid position graded at least `_STRENGTH_STRONG_BEAT` — a
  downbeat, or the mid-bar strong beat of a 4/4 measure), plus a
  note-density backstop (tier 0 materializes at least the bottom tier's
  equal share of the phrase's notes, 1/n_levels, capped at what a full
  demotion of the phrase could emit at tier 0 — a voicing whose outer
  voices are an octave apart collapses to one note at tier 0 however
  many voices it has, so the raw share can be unreachable even with
  every group at tier 0; the emitted count is tracked incrementally —
  a group's tier-0 materialization is independent of every group's
  level, so the per-group sizes are computed once and each demotion
  adds its group's size — instead of rematerializing the whole phrase
  after every demotion, which is quadratic in the phrase's group count
  on dense passages), topping up with the
  cheapest remaining groups when the skeleton alone is thin — in 3/4 or
  6/8, or when no graded beat grid exists at all). Measured on the same
  fixture: tier 0 now holds 7 notes (the bass voice of nearly every
  chord); on 2 bars of sixteenth notes, 9 of 32 with every strong beat
  covered. The floor only relabels levels downwards within a phrase (the
  arrangement-wide thresholds are untouched and every tier's group set only
  grows), but a demotion can make an ADJACENT pair of tiers
  byte-identical: demoting every group that sat at level k+1 equalizes
  the pair's group sets, and when their per-tier voice budgets then agree
  (always for two-voice groups, and for octave voicings whose extra voices
  duplicate exposed outer voices) `_collapse_identical_levels` would
  silently merge the pair, costing the ladder a tier. The anti-collapse
  guard therefore never lets the floor create a new identical pair: a pair
  already identical beforehand is a pre-existing collapse (issue #70's own
  territory), and the surgical fix restores the most recently demoted group
  of the level the boundary lost — one restoration per threatened boundary,
  every other demotion intact, sacrificing backstop additions before
  strong-beat skeleton positions. Where the guard and the floor conflict,
  the ladder without the collapsed tier wins: on a passage of nothing but
  two-voice chords, any demotion at all would create a new identical pair,
  so the floor leaves the tier assignment untouched. The floor is keys-only;
  the fretted path is untouched.
- **Keys/piano reduction is voice-aware (#180, item 5 of the keys roadmap
  #175).** Three gaps in the hand-aware thinning above, each measured on a
  synthetic fixture. (1) *Bass root on downbeats.* The #181 strong-beat
  skeleton keeps ONE group per strong position — whichever sits nearest it —
  and on a two-hand onset that is usually the melody, which tends to score
  cheaper than the accompaniment; the downbeat bass root was then left out of
  the bottom tier while still sounding (measured: an Alberti-bass phrase's
  tier 0 held the melody but not the downbeat root under it). The skeleton now
  also keeps the lowest-pitch group of the same onset. Only groups sounding at
  that onset qualify, so a position with no bass note under it — a rest, or a
  right-hand-only bar — forces nothing into a sparse window. (2) *Per-hand
  floor.* The skeleton and the note-density backstop both reason about groups
  GLOBALLY, so on a texture where one hand plays a dense run and the other
  only a few widely spaced notes, the dense hand's many cheap groups satisfy
  both on their own and the sparse hand is absent from every reduced tier but
  the top (measured: right-hand sixteenths under a left-hand bass sounding
  only on two off-beats put the left hand at the top tier alone). Tier 0 now
  keeps one group from each hand, so one hand's density cannot starve the
  other. Hands come from the `hand_split` groups when the phrase has any;
  otherwise from `_keys_phrase_hands`, which bands the phrase's own pitches at
  the widest gap between consecutive distinct pitches, so hands that are
  *staggered* (never sounding together, hence never split) are covered too.
  (3) *Crossed / interleaved hands.* `_split_keys_hands` split at the widest
  internal gap, but with several equal gaps — the crossed case — the first is
  not necessarily the seam: `[36, 48, 60, 72]` has three equal 12-semitone
  gaps, and taking the first returned a lower part spanning 24 semitones —
  two octaves, impossible for one hand. Candidate seams are now ranked by
  widest gap and then by balance (the split whose larger part spans least), so
  ties resolve to the most compact split (`[36, 48] | [60, 72]` here, each part
  within a hand); among seams that leave both parts within a hand's reach
  (`_KEYS_HAND_SPAN_SEMITONES`, a 9th) that ranking is applied, and a genuine
  split is never dropped for being wide — the widest gap is still the
  fallback. Fixtures for Alberti bass, stride, and crossed-hand passages pin
  tiers 0-2 and nesting in `tests/test_dd_generation.py`. The bass-root and
  per-hand *demotions* only relabel levels downwards; the anti-collapse guard
  prefers restoring a non-required demotion, so a required group normally keeps
  its tier-0 place, and restores a required one only when nothing else can
  separate an otherwise-identical pair — keeping the ladder's full tier count,
  which outranks the guarantee when the two conflict (leaving the pair would
  let the floor create 22 new identical tier pairs across these fixtures). The
  split change is upstream of tiering — it affects which notes share a group
  and their hand/melody labels, and therefore scoring — and is covered by the
  nesting tests plus the `_split_keys_hands` unit tests.
- This is a fresh implementation against feedBack's own arrangement wire
  format (`lib/song.py`) — it does not port code from, or share a runtime
  with, the Slopsmith arrangement editor's differently-scoped difficulty
  feature; only the general "score note groups, bucket into tiers" heuristic
  approach was used as design inspiration.

**Instrument coverage**

| Instrument | Supported? | Notes |
|---|---|---|
| Guitar / bass (fretted) | ✅ | Fret complexity, low-position stretch posture, time-aware hand shifts, string-skip/hand-shape distance, tempo/syncopation-aware density, sustain-ease. Technique scoring covers bend (base + pre-bend/round-trip/shaped-curve difficulty, `bt`/`bnv`), slide, hammer-on/pull-off, tremolo, natural vs. pinch harmonic (scored independently), palm/string mute, vibrato, fret-hand mute, and bass slap/pop (scored independently, slap weighted harder), plus a coordination bonus for a group using more than one distinct technique at once or switching technique from the group before (a chord mixing a bend and a palm mute scores above either alone; see "Technique difficulty counts coordination demand" below). Timing thresholds (grouping window, beat tolerance, movement time scale) scale with the song's own tempo instead of fixed wall-clock constants. |
| Keys / piano | ✅ | Separate pitch-based heuristic (polyphony, hand-span, density, sustain-ease, per-hand position shift, black-key share) — keys notes encode `midi = string*24 + fret`, so the fretted heuristic doesn't apply and never runs against them. No fret anchors/hand-shapes generated (the piano renderer doesn't consume them). |
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
- **Warm-up start (Adaptive only).** The first time you resume a song in a
  session, the song starts slightly *below* its remembered difficulty —
  half of one auto-adjust ramp step, i.e. at most one full step and
  normally less: 5% at Sensitivity 1, 8% at 2, 10% at 3 (see **Sensitivity**
  in the settings table for what a "step" is). Your first sections of a
  session tend to dip below the level you settled on, and starting at the
  peak makes early misses likely enough to provoke a step-down you didn't
  need. The ramp climbs back once your rolling accuracy clears its
  up-adjustment threshold — a session that settles mid-band plays out at
  the slightly lower start, which is the intent — and **WARMUP_PHRASES**
  still keeps the ramp quiet until it has real evidence (the two compose —
  neither replaces the other). It applies once per remembered difficulty (a
  song/arrangement/instrument/role/skill combination) per session: a
  re-restore of the record already in play puts you back exactly where the
  start left you, while a *different* song mid-session gets its own warm-up
  start. Never in **Standard** mode (nothing would climb back, so it would
  just strand you below your own value), and never below your **Min %**
  floor — if the song's remembered value already sits at or under it, there is
  no room to give and it starts as it is. The remembered value itself is
  untouched: the start is a live-only value, and the ramp's first
  adjustment — or you moving the slider — is what re-records it.

The legacy single-player storage is migrated conservatively into the
`difficulty_ladder.progress.v2` and `difficulty_ladder.phraseAttempts.v2`
stores under `skill: "overall"`; unscoped legacy data is not claimed by
concurrent profiles. A legacy record can be claimed by only one player, and its
claim marker prevents another player sharing that profile from reading it.

**Best mastery** — alongside the remembered difficulty, each song/arrangement
keeps a long-term best: `difficulty × that phrase's hit rate`, rounded to 2 decimals,
stored per player/song/arrangement/skill and **monotonic** — a weaker session never
lowers it, and it is never touched by difficulty writes or the legacy migration. Each
NEW best emits one `difficulty:mastery-updated` event (schema
`difficulty_ladder.mastery-updated.v1`) with the player context, the new `best_mastery`
and the `previous_best` it surpassed (null for a first best), so an integration can
track the long-term score from the event stream without reading the store. It rides the
same discrete phrase-commit write as the record — never a per-frame path — and a host
without the event bus drops it silently.

**Live auto-adjustment**
- Reads live per-note hit/miss judgments from whichever note-detection scorer
  is active (e.g. the `note_detect` plugin) via `highway.getNoteStateProvider()`
  — this plugin doesn't score notes itself, it observes an existing scorer.
- Tracks a rolling accuracy average per song section (phrase) and nudges the
  master-difficulty slider (`window.setMastery`) up after a run of clean
  sections, or down after a rough one. With the *Level up only* setting on,
  the downward nudge never happens — step-ups, warm-up counting, bounds, and
  the manual-override stand-down below are all unchanged.
- Won't act on a song's very first section at all, and starts a song's first
  session slightly below its remembered difficulty (see **Per-song difficulty
  memory** above) — so a rusty first section is neither acted on nor
  provoked.
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

**Difficulty guide**
- Renders upcoming sections in the player as bars — taller bar = a
  harder section (scaled by that section's peak authored difficulty), fill
  level = how much of that section's difficulty range the current
  master-difficulty setting reaches.
- At the configured maximum mastery, three consecutive phrases at 95%
  accuracy or better light a gold Mastery streak badge. Pausing or entering
  or leaving a split-screen session resets it.
- Purely a visualization; can be toggled independently of auto-adjust.
- With Section Map installed the standalone overlay is suppressed and the
  guide renders in Section Map's own section bar instead. The *Show
  difficulty guide* setting only gates this plugin's overlay; it is not
  wired to Section Map's section bar.

## Requirements

- Target Host: feedBack core implementing `plugin-spec-v1.md` with the v3 player chrome
  (`window.feedBack.ui.playerControlSlot()`) — the only chrome feedBack core ships as of v0.3.0.
  The player-controls buttons (Auto-Difficulty, Generate Difficulties) mount via a
  `window.feedBack.uiVersion === 'v3'` guard, which is vacuously satisfied on any current Host;
  see `COMPLIANCE.md` for why this is no longer tracked as a gap. The difficulty guide itself
  never depended on `uiVersion` and renders regardless.
- feedBack core with the `note-detection` capability / `setNoteStateProvider`
  contract (spec 009) and phrase-level difficulty data (feedBack#48).
- A note-detection scorer plugin installed and active for auto-adjust to have
  any signal to react to. Without one, the tier rail still renders on song
  load (using only authored difficulty + the manual mastery slider) but has no
  phrase-transition events to advance it, and auto-adjust stays idle.

## Host and peer compatibility

`plugin.json` has never declared a `minHost` — see
[#129](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/129).
Loading/generating ladders and getting fully correct *live* adaptive
behavior have different core requirements, and conflating them into one
number would either be too strict (blocking generation on hosts that
support it fine) or too lax (silently mis-rendering phrase tiers on hosts
that don't). feedBack core has no tags or releases (it versions itself
via a root `VERSION` file, currently `0.3.0-alpha.2`), so until a
release-qualified audit exists against a version core actually ships,
this documents the known source-level floors instead:

| Capability | Core requirement | Evidence |
|---|---|---|
| Loading / generating ladders (`routes.py`) | `dlc_paths._resolve_dlc_path` (core `0dcc913`, Jul 10) and `sloppak.read_member_bytes` (core `d876ded`, Jul 13) | Direct imports in `routes.py`; feedBack core has no tags/releases and versions itself via a root `VERSION` file (currently `0.3.0-alpha.2`) — both commits postdate that version line, which is the load-bearing fact until a release-qualified audit exists |
| Correct phrase-tier semantics (mastery slider ↔ `getPhrases().top_difficulty`) | Core `e5339c0` (Sep 23) — switched mastery selection to tier-number semantics and exposed `top_difficulty` | `screen.js` consumes `top_difficulty` when present but only *falls back* when it's absent — that doesn't prove older-core rendering agrees with the HUD/attempt records |
| Full concurrent-player support (`playerContexts`, `player-difficulty.v1`) | Core `7633211` (Sep 17) | Older hosts fall back to a legacy per-player-unaware compatibility path — see [`PLAYER_CONTEXT.md`](PLAYER_CONTEXT.md) |

These are source/history findings (inspected against plugin revision
`d6e60f6`), not an end-to-end certification of any specific older host —
treat `e5339c0` (or a tested descendant) as the candidate baseline for
full adaptive-difficulty behavior, and the two `Jul` commits as the floor
for generation working at all, until a release-qualified audit lets
`minHost` be set honestly. Related: [#87](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/87)
and [#88](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/88)
implement the player-context features this table only versions.

The plugin cannot declare `minHost` (no release to pin to), so it ships an
**actionable runtime diagnostic instead**: `contributeDiagnostics` publishes
`host_reports_phrase_tiers` — `true` when every phrase the host reports
carries `top_difficulty` (core `e5339c0`+), `false` when at least one phrase
lacks it (tiers are then derived from `max_difficulty` alone on those
phrases, which cannot tell a collapsed single-level phrase from a full
one — a mixed host is still a legacy host for those phrases), and `null`
when there is no phrase data to judge. The three phrase-tier consumers
(HUD glass, the `difficulty_ladder.sections.v3` event, and the phrase-attempt
log) and their one intentional divergence on a collapsed ladder
(`top_difficulty < max_difficulty`) are pinned by `tests/screen.test.js`'s
host-contract tests.

**Peer plugin requirements** (see also `CLAUDE.md` → *Plugin dependencies*,
[#130](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/130)).
None of these block the plugin from loading or generating ordinary
ladders — each is a feature-scoped optional dependency, not a hard one:

| Peer | Feature it enables | Required / optional | Lowest auditable version | Degraded behavior when absent/older |
|---|---|---|---|---|
| [Chordr](https://github.com/get-flashbacks/feedback-plugin-chordr) | The read-only chord-grouping preview at `POST /api/plugins/difficulty_ladder/analyze-chords` (`routes.py:460`), which calls `app.state.chordr_analyze_chart_chords_v1` (`routes.py:496`) | Required, for that route only | v0.5.0 (first version registering `app.state.chordr_analyze_chart_chords_v1`) | Route returns HTTP 503 when Chordr is absent. `staged_chords` (the chord-landmark generator stage, `GenerateIn.staged_chords`) is **not** Chordr-gated — it's a plugin-local option that re-derives chord identity from the arrangement's own templates and works identically with Chordr absent or missing |
| [Note Detect](https://github.com/get-flashbacks/feedback-plugin-notedetect) | Split-screen adaptive scoring (`window.createNoteDetector`) | Optional | v1.15.2 for the factory/`ownSource` split-panel registration path at all; v1.33.0 for stable `player_context` propagation | Below 1.15.2: split-panel instances aren't registered for adaptive scoring. 1.15.2–1.33.0: works, but panel identity is unpersisted and per-highway rather than stable |
| [Split Screen](https://github.com/get-flashbacks/feedback-plugin-splitscreen) | Per-panel difficulty state (`window.feedBackSplitscreen`/`window.slopsmithSplitscreen`) | Optional | v1.10.6 — the earliest auditable Split Screen version (the repo's history begins at a `Clean release snapshot` tagged 1.10.6) that already exposes the consumed `window.slopsmithSplitscreen` with `isActive()`. The `window.feedBackSplitscreen` alias is newer (v1.10.8), but the integration reads `window.feedBackSplitscreen \|\| window.slopsmithSplitscreen`, so 1.10.6 is sufficient at the source level | Feature-detected via a bare `typeof ss.isActive === 'function'` presence check — that catches a missing global or a missing `isActive` method, but not a present `isActive` whose contract moved underneath it, which is exactly the silent-death case splitscreen#47 (below) describes |
| [Section Map](https://github.com/get-flashbacks/feedback-plugin-sectionmap) | Glass-fill difficulty indicators via the `difficulty:sections-updated` event, rendering only its `sectionDifficulties[].fillPercentage`/`.glassSize` fields | Consumer of this plugin's event, not the other way around | Difficulty Ladder v0.12.0 — where `CHANGELOG.md` documents the `difficulty_ladder.sections.v2` payload contract (the event itself shipped in v0.2.0); Section Map does not actually inspect a `schema` field, so this floor is about when the current fill-percentage payload shape stabilized, not a version string Section Map validates | See `INTEGRATION.md` for the full contract |

A `typeof` check alone doesn't catch a downstream contract change on an
otherwise-present global — see
[feedback-plugin-splitscreen#47](https://github.com/get-flashbacks/feedback-plugin-splitscreen/issues/47),
which documents two other plugins' integrations going silently dead this
way (the Split Screen row above is exactly this kind of gap). Capability
probing (rather than a bare presence check) remains a follow-up; the
missing/minimum/current degraded behavior of the Note Detect and Split
Screen surfaces is now pinned by `tests/peer_compat.test.js`, and the
Chordr absent/failing path by
`test_chord_preview_reports_missing_service_and_service_failure`.

These peer floors are a snapshot inspected against plugin revision
`d6e60f6` (same as the core table above), not a continuously-verified
contract — `CLAUDE.md` → *Plugin dependencies* carries the same four
numbers and is this repo's own warning that they're moving targets under
active audit in #129/#130; treat this README section as that snapshot for
the same facts, not an independent source.

## Song Mastery consolidation

This plugin absorbed the Slopsmith-era **Song Mastery** plugin's per-song
difficulty memory, long-term best mastery, and library-card badge. The
consolidation record — what was absorbed, the exact storage/migration contract
(the `migrations.songMasteryV1` cutover marker and its "one-time window"
caveat, the runtime-rewritten legacy `songMastery` key, and the best-effort
rollback path), an end-to-end compatibility matrix (including the unhandled
concurrent-tab clobber), the both-installed conflict decision, and the gated
removal timeline — is in [`MIGRATION.md`](MIGRATION.md). The two plugins
coexist in **read-only compatibility mode**: this plugin never reads or writes
the *separate Song Mastery plugin's* storage, and registers its own card action
under a distinct id. **The separate Song Mastery plugin is not declared
obsolete** — that step is gated on the matrix's cross-plugin upgrade row
passing and on #84/#85.

## Settings

Exposed via Settings → Plugins → Difficulty Ladder:

| Setting | Effect |
|---|---|
| Difficulty mode | **Standard** (default) keeps difficulty fixed — no automatic movement. **Adaptive** enables today's live auto-adjust (`setMastery()` calls driven by accuracy). |
| Resist isolated difficulty drops | Require two consecutive below-threshold sections before a downward adjustment; upward adjustments remain immediate. Off by default. Inert while *Level up only* is on. |
| Level up only (#111) | Opt-in comfort switch: auto-adjust raises difficulty as usual but never lowers it, at any accuracy. The manual slider still works, and moving it still stands auto-adjust down entirely — both directions — exactly as without this setting. Off by default, and deliberately presented as a comfort option rather than a learning aid (a 2022 meta-analysis, McKay et al., found the self-controlled-practice benefit close to zero after bias correction). |
| Show difficulty guide | Show/hide this plugin's standalone difficulty guide: a compact, **event-driven** segmented **tier rail**. One cell per visible phrase; the first `currentTier + 1` segments are lit and an outlined segment marks `topTier` (the full-detail threshold) — difficulty is never encoded by changing a component's size. Hover, focus and an `aria-label` give the exact `Tier N of M · full detail at K` (a single focus stop reads the current + upcoming phrases). It updates on song load, phrase commit, mastery change and settings change — never from a `requestAnimationFrame` loop. With Section Map installed the overlay is suppressed and the guide renders in Section Map's section bar instead; this setting does not control that surface. |
| Sensitivity (1-3) | How confident auto-adjust must be (hit-rate thresholds) before it moves the slider, and how big a step it takes. A step is 10 / 15 / 20 percentage points; the per-song warm-up start is a fraction of one (see above). |
| Reaction speed (1-3) | How much weight a single section's result carries in the rolling accuracy average (`EMA_ALPHA`) — independent of Sensitivity. Default (2) reproduces this plugin's original, pre-#5 behavior. |
| Difficulty drop speed (1×-2×) | Multiplies only the downward auto-adjust target so difficulty can ease off faster than it climbs. The default 1× preserves symmetric behavior. Inert while *Level up only* is on. |
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

`preview: true` on `/generate` returns per-arrangement provenance without
writing the pack. A ladder with a valid `x_difficulty_ladder` marker reports
its recorded Difficulty Ladder plugin version; an older marker without that
field reports the generator but not its release. A ladder without a valid
marker has unknown provenance: it may be handmade or generated before marker
tracking existed, so the plugin cannot reliably identify it as handmade.

`overwrite_authored` is an additional `/generate` and `/generate-library`
request field — `false` by default — required before `force` may overwrite a
ladder with unknown provenance. The library-card action first previews all
arrangements, shows the available provenance in its confirmation, then sends
`overwrite_authored: true` only if an unknown ladder was included and the user
confirmed. This flag never overrides the skip for drums/unsupported arrangements.

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

**Where auto-adjust settles (roadmap C2, #55)** — Sensitivity and Reaction
speed don't map to a target accuracy directly; where the controller
actually settles was measured by simulation (`tools/settle_points.js`
drives the real `commitPhraseResult`, not a re-implementation), since the
controller is an EMA with a dead band rather than the simple weighted
up-down rule the roadmap originally assumed a settle-point formula for.
20 seeds × 400 phrases per setting, 16 notes/phrase, drop resistance off,
reaction speed 2 unless noted. Reproduce with
`node tools/settle_points.js --sens=1,2,3 --react=2 --notes=16 --drop=0`:

| Sensitivity | Mean accuracy | Settles across runs | Slider moves / 100 phrases |
|---|---|---|---|
| 1 (lenient) | 0.80 | 0.77–0.84 | 0.4–0.5 |
| 2 (default) | 0.78–0.79 | 0.76–0.81 | 5–12 |
| 3 (strict) | 0.77 | 0.76–0.78 | 25–35 |

This table only varies Sensitivity. Reaction speed also moves the
movement column a lot, but its effect on the settle range differs by
setting (`--sens=1,2 --react=1,2,3 --notes=16 --drop=0`): at sensitivity
2, moves/100 goes from 1–6 (reaction 1) to 5–12 (reaction 2) to 11–19
(reaction 3), while the settle range barely shifts (0.75–0.82 / 0.76–0.81
/ 0.77–0.80). Sensitivity 1 is not similarly stable — its mean (union
across slopes) rises to 0.79–0.83 and its per-run spread widens to
0.76–0.86 at reaction 1, against 0.80 and 0.77–0.84 at reaction 2 (see
the burn-in caveat below for why sensitivity 1's spread is less
trustworthy than 2/3's to begin with).
Roadmap C2 (#103) calls for a table across *both* dimensions; this
section documents only Sensitivity at a fixed Reaction speed — the
Reaction-speed dimension stays open under #55.

Findings, in order of confidence:
- **All three settings land slightly below the ~0.80–0.85 figure roadmap
  C2 (#103) cites** — a target the plugin itself doesn't declare anywhere
  in its settings or code; the controller's only actual targets are the
  hit-rate thresholds in `thresholds()`. That figure traces to Wilson,
  Shenhav, Straccia & Cohen's "Eighty Five Percent Rule" (*Nature
  Communications* 10:4646, 2019), which derives the *training* accuracy
  that maximizes the rate of learning under gradient-descent rules for a
  broad class of classifiers — about 85% for Gaussian label noise, less
  for heavier-tailed noise (82% Laplacian, 75% Cauchy) — not a level a
  controller should settle at during ordinary play. Comparing it to this
  section's steady-state settle points is a category mismatch on top of
  the motor-learning extrapolation: the paper prescribes a difficulty to
  hold *while learning*, and the simulated player here explicitly has no
  learning process to hold it for (constant skill, no fatigue — see
  Confidence below). Roadmap C1 (#54, reviewing this rule and its limits)
  is still open; treat every comparison against 0.80–0.85 in this section
  as indicative at best, not a target the controller is failing. Sensitivity
  1 reads closest to it, but see the burn-in caveat below for why that
  row's own settle point is the least trustworthy of the three.
- **"Strict" has the *tightest* settle point of the three, not the
  loosest** — despite moving the slider far more often. The settle
  ordering (0.80 / 0.79 / 0.77) sits within 0.01 of the sensitivity
  thresholds' own dead-band midpoints (`thresholds()` in `screen.js`), so
  it follows directly from those thresholds — no simulation needed to
  explain *that* part. What the simulation adds is the movement column:
  the frequent moves are mostly per-phrase noise (a single miss swings a
  short phrase's hit rate a lot) amplified by strict's bigger step and
  narrower dead band, not evidence the setting is unstable in the sense
  its name implies.
- **Short phrases are the dominant source of that noise.** At every
  sensitivity, the hit rate the controller actually observes spans wider
  with fewer notes per phrase — roughly 0.50–1.00 (10th–90th percentile)
  at 6 notes, 0.56–0.94 at 16, 0.68–0.90 at 40
  (`--notes=6,16,40 --react=2 --slopes=0.1 --drop=0`); the per-sensitivity
  spans at 16 notes are 0.69–0.94 / 0.63–0.94 / 0.56–0.94, so "independent
  of sensitivity" describes the conclusion (fewer notes always widens it),
  not identical numbers across sensitivities.

**Confidence:** moderate that the *direction* of each finding holds — it's
consistent across every slope, seed set, and note count tested. Low
confidence in the *exact numbers* — the simulated player is a smooth
logistic curve with constant skill (no learning/fatigue), every phrase has
the same note count, and the slider maps straight to success probability
rather than the chart's real discrete tiers. No real player data was used,
and none of this is a claim about actual players. Every run above
discards the first 100 (of 400) phrases as burn-in. A `--start` sweep
(10/30/50/70) confirms this fully for sensitivities 2 and 3 — byte-
identical settle ranges at every start — but only partially for
sensitivity 1: at 0.4–0.5 moves per 100 phrases, its post-burn-in window
sees roughly one slider move across the 300 measured phrases, so it
rarely re-equilibrates after burn-in ends. Its reported range (0.77–0.84)
is the union across starts, not evidence of a single stable equilibrium
the way 2/3's identical-across-starts numbers are — the per-run settle
itself varies by start/slope combination too (0.77–0.83 at slope 0.20
regardless of start; as wide as 0.78–0.84 at slope 0.05, start 10).
Finding 1's "sensitivity 1 is closest" ranking rests on this least-settled
row; take it as directional, not as precise as the table formatting
implies.

**Not done here:** no setting has been retuned as a result of these
numbers (e.g. weighting the EMA by note count, or adjusting strict's step
size/dead band) — that's a separate decision pending review of the data
above, not something this measurement does on its own authority. The
Reaction-speed half of C2's table, the broader comparison against
Elo/Glicko, Kalman, and IRT estimators, and the synthetic
learner/plateau/fatigue player scenarios all remain open under #55.

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
  the example) is a drop candidate whenever another bottom-tier
  occurrence of the same identity sustains as long or longer — including
  when the short chord is the LATER of the two, and including an exact
  sustain tie, which the landmark rule gives to the earlier occurrence.
  The only group besides that landmark that is never dropped is the
  phrase's own final, note-bearing bottom-tier chord group. This lands
  only the bottom tier's group selection;
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

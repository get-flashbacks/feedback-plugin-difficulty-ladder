# Per-technique controller weighting — design note

**Issue:** [#167](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/167) (roadmap C7-1)
**Parent:** [#122](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/122) (roadmap C7) · [#103](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/103) item C7
**Status:** design only — no code lands from this note.

## 1. Problem

Auto-adjust scores each finalized phrase with a single flat hit rate and feeds
that number through one EMA (`state.emaHitRate`, `screen.js`). The same ratio
therefore carries the same evidence whether the phrase is plain downpicking or
a run of pinch harmonics. That is the item-difficulty confound [#31](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/31)
names: an average hit rate cannot tell a strong player on a hard phrase from a
weak player on an easy one.

C7 asks auto-adjust to weight a phrase's accuracy signal by how much the phrase
leans on techniques the player is specifically weak or strong at, instead of
today's flat hit rate. This note resolves the two questions #122 left open —
**cold start** and **composition with the existing controller settings** — with
a recommendation and the rejected alternative for each, states the technique
vocabulary, and records the evidence confidence and the gate for proceeding.

Nothing here is implemented. [#168](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/168)
(go/no-go), [#169](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/169)
(store) and [#170](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/170)
(implementation) build on it.

## 2. Technique vocabulary

This reuses the vocabulary the generator already models — no new flags and no
new capability contract. `scoring.py`'s `_tech_score` weights and
`_TECH_GATE_FRAC` gates these wire flags:

| Flag | Technique | Flag | Technique |
| --- | --- | --- | --- |
| `bn` (`bt`, `bnv`) | bend, with intent and curve | `hm` | natural harmonic |
| `ho` / `po` | hammer-on / pull-off | `hp` | pinch harmonic |
| `tp` | tap | `plk` | pop |
| `sl` / `slu` | slide / slide-up | `slp` | slap |
| `tr` | tremolo | `pm` / `mt` | palm mute / string mute |
| `vb` | vibrato | `fhm` | fret-hand mute |

`_technique_categories` folds these into 13 categories (the mapping in
`_TECHNIQUE_FLAG_CATEGORIES`, plus `hopo` for `ho`/`po` and `slide` for
`sl`/`slu`). The flags already ride every streamed note
(`getFilteredNotes()` / `getChords()`), so the controller can attribute a miss
to a technique from the notes it is already iterating — the same source
`_tech_score` reads at generation time.

## 3. Where the weighting lives (controller only)

The weighting belongs entirely on the live/controller side, in auto-adjust's
phrase scoring in `screen.js`. Generated ladders are written **once** into the
shared sloppak file (the canonical per-song store, [#82](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/82)),
not regenerated per player. A per-player technique proficiency is therefore
per-player and time-varying, and it must not fork `/generate`'s output: the
generation heuristic stays player-agnostic. This mirrors C7's scope note in
#122 and keeps the shared ladder identical for every player of a song.

## 4. Cold start

**Recommendation — neutral default, never an assumed weakness.** Model a
per-technique proficiency `p_t ∈ [0, 1]` with a neutral value of `0.5` meaning
"no evidence yet". The per-phrase weight is a bounded multiplier that is
**exactly `1.0` when every technique the phrase leans on is still at neutral**,
so a brand-new song or a never-seen technique scores *exactly* as it does
today. Each technique contributes to the phrase weight only once it has at
least `N` finalized judgments (a small evidence floor, e.g. the existing
`WARMUP_PHRASES` scale); below that it is treated as neutral.

*Rejected — assume weakness (`p_t = 0` on first sight).* Penalizing every
technique the first time it appears is precisely the confound #31 warns about,
and it would make auto-adjust act before it has any real signal about the
player.

*Rejected — song-level or cross-song prior seeding.* Seeding a new song from
"similar songs" requires a cross-song skill profile the README explicitly rules
out, and it adds state whose failure modes (stale, mis-scoped, profile-leaking)
outweigh a neutral start when C5's data does not yet justify any prior.

## 5. Composition with the existing controller settings

The existing controls each own a distinct job (see `screen.js`):

- **Sensitivity** (`thresholds()`): the up/down hit-rate gates and the step
  size (`10/15/20`).
- **Reaction speed** (`emaAlpha()`): how much one phrase moves the EMA.
- **Resist isolated difficulty drops** (`dropResistance` + `downStepRatio` +
  `DOWN_CONFIRM_PHRASES`): the downward confirmation window and step
  multiplier.

**Recommendation — pre-scale the evidence, do not touch the controls.** Apply
the bounded per-technique correction to the phrase's accuracy ratio *before* it
enters the EMA and the existing gate pipeline. Concretely, the order stays
`ratio → [technique correction] → EMA → rampDirection(thresholds()) →
dropResistance/ramp`. Sensitivity keeps governing thresholds and step;
reaction speed keeps governing the EMA; drop resistance keeps governing
downward confirmation. Technique weighting never edits those settings or their
thresholds — it only shifts *where a phrase's evidence lands inside them*.

The correction is clamped to at most one threshold margin, so technique
weighting alone can never move a phrase across both the up and down gates at
once.

*Rejected — replace Sensitivity / Reaction speed / Resist.* They answer
orthogonal questions (how strict, how fast, how hard to drop) that technique
attribution does not. Replacing them breaks the documented contract and the
byte-for-byte default behavior a user who touches nothing still gets.

*Rejected — tiebreaker only.* Applying technique weighting solely to break
otherwise-tied decisions is too weak to change any real decision; it would add
all the state and complexity for no behavioral effect.

*Rejected — edit the thresholds afterward.* Widening/narrowing
`thresholds()` by proficiency double-counts against Sensitivity's own step
scaling and makes the two controls non-separable, so a user could not reason
about either one.

## 6. Evidence confidence and gate

**Confidence: 🔴 weak.** As #122 tags it, this is design judgment with no direct
citation. The mechanism — weight subgroup evidence by subgroup proficiency — is
plausible and has analogues in mastery learning, but it is unmeasured for this
plugin.

**Gate — do not implement blind.** C7 proceeds only after C5
([#113](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/113))
has shipped and logged enough real accuracy-vs-technique data to:

1. estimate `p_t` with a defensible per-technique sample size (set `N`);
2. show the weighting changes decisions often enough to matter — otherwise the
   added store and live-scoring logic are unjustified; and
3. validate the neutral cold-start choice (that first-exposure accuracy on a
   new technique is not systematically worse than neutral).

If C5's logs fail (2) or (3), the recommendation is to **not** implement C7, and
to close #122 as measured-not-worth-the-complexity rather than ship a second
adaptive signal.

## 7. Future contract notes (not specified here)

Per-technique proficiency is per-player state. When #169 builds it, the store
must follow the profile/player-context layout from [#87](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/87)
and [#88](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/88)
(keyed by profile identity and instrument/role), and update only on finalized
phrases per the [#65](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/65)
contract — never per note or per frame, per the repo's `CLAUDE.md` performance
doctrine.

## 8. Open questions handed to later sub-issues

- [#168](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/168): the concrete go/no-go gate using C5's logged data.
- [#169](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/169): the persisted per-technique store.
- [#170](https://github.com/get-flashbacks/feedback-plugin-difficulty-ladder/issues/170): the live implementation behind a setting.

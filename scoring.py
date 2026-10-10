"""Difficulty Ladder plugin — pure chart-scoring core.

Extracted from `routes.py` (Stage 2b, #146/#155) so the scoring math lives
apart from pack I/O and HTTP. This module is the plugin's pure half: constants,
arrangement classification, tempo handling, note grouping, fret/span/posture/
technique/beat/syncopation scoring, tiering, phrase windowing and level
materialization. Every function here takes plain dicts/lists and returns plain
dicts/lists — no Path, zipfile, sloppak, os or FastAPI in any signature.
Locked in by the `pure-core-has-no-io` CI check, which splits this file at the
`Pure chart-scoring core` banner below.

Loaded by `routes.setup()` through `context["load_sibling"]("scoring")` — the
host's namespaced sibling loader. Nothing here imports from `routes`; the
dependency is one-way (routes -> scoring), so there is no import cycle.
"""

import bisect
import math
import re
from dataclasses import dataclass
from itertools import pairwise


# ── Pure chart-scoring core (no Path/zipfile/FastAPI) ────────────────────────
#
# This region owns all of the plugin's scoring math: constants, arrangement
# classification, tempo handling, note grouping, fret/span/posture/technique/
# beat/syncopation scoring, tiering, phrase windowing, and level
# materialization. Every function here takes plain dicts/lists and returns
# plain dicts/lists — no Path, zipfile, sloppak, os or FastAPI in any
# signature. Locked in by the `pure-core-has-no-io` CI check; see the module
# map in the docstring.

MIN_EVENTS_FOR_GENERATION = 8  # skip near-empty arrangements — nothing to grade
FRET_JUMP_WINDOW_SECONDS = 1.0  # longer rests give the player time to reposition

# ── Arrangement classification ────────────────────────────────────────────────

# Same convention core uses for piano-roll mode (CLAUDE.md: "Any arrangement
# named Keys, Piano, Keyboard, or Synth renders as a piano-roll chart").
_KEYS_NAME_RE = re.compile(r"^(keys|piano|keyboard|synth)", re.IGNORECASE)
_DRUMS_NAME_RE = re.compile(r"^(drums?|percussion|kit)", re.IGNORECASE)
_UNSUPPORTED_NAME_RE = re.compile(
    r"^(sax|saxophone|vocals?|voices?|violin|cello|flute|trumpet|trombone|lyrics?|notation)",
    re.IGNORECASE
)


_FRETTED_TYPES = frozenset({"lead", "rhythm", "bass", "combo", "chord", "humstrum"})
_KEYS_TYPES = frozenset({"piano", "keys"})
_DRUM_TYPES = frozenset({"drums", "drum"})


def _instrument_kind(arr_type: str, arr_name: str) -> str:
    """Classify an arrangement for generation purposes: 'fretted', 'keys',
    'drums', or 'unsupported'. Keys notes encode `midi = string*24 + fret`
    (no fretboard at all), so the guitar/bass fret-complexity heuristic below
    is meaningless for them and must not run — they get their own
    pitch/polyphony-based scoring. Drums arrangements never reach this module
    in the first place (see setup()'s manifest-entry check) since they carry
    no notes/chords file — 'drums' here is just for a clear, honest skip
    reason.

    'unsupported' (issue #66) is an explicit allowlist miss: a *specific,
    non-empty* type string that isn't one of the known fretted/keys/drums
    values, e.g. a vocals/harmony/notation arrangement whose `file` happens
    to point at something this generator can read structurally but whose
    content this generator has no business scoring. An *absent/blank* type
    still requires name-sniffing to detect unsupported instruments (drums,
    sax, vocals, etc.) — feedpakr (the GP importer, the primary source of
    packs in the wild) omits `type` for fretted/keys, but the name alone
    can indicate an unsupported instrument (issue #102). Only when type is
    blank AND the name doesn't match any unsupported pattern do we default
    to fretted.
    """
    t = (arr_type or "").strip().lower()
    n = (arr_name or "").strip()

    # Explicit type always takes precedence
    if t in _DRUM_TYPES:
        return "drums"
    if t in _KEYS_TYPES:
        return "keys"

    # When type is blank, use name-sniffing to detect unsupported instruments
    # and keys before defaulting to fretted
    if t == "":
        if _KEYS_NAME_RE.match(n):
            return "keys"
        if _DRUMS_NAME_RE.match(n):
            return "drums"
        if _UNSUPPORTED_NAME_RE.match(n):
            return "unsupported"
        # Blank type with no unsupported indicators: default to fretted
        # (preserves compatibility with legacy packs from feedpakr)
        return "fretted"

    # Non-blank type: explicit classification
    if t in _FRETTED_TYPES:
        return "fretted"
    return "unsupported"


def _is_bass_arrangement(arr_type: str, arr_name: str) -> bool:
    """Mirrors lib/song.py's `arrangement_is_bass()`: an editor-authored
    `type == "bass"` (exact match) first, then a case-insensitive "bass"
    substring in the name (so "Bassline" counts, unlike a `\\bbass\\b`
    word-boundary match). `path_bass` -- core's third, most-authoritative
    signal -- isn't part of the wire dict this generator reads and so has
    no equivalent here; the other two are the ones available to a chart
    without that XML-only flag anyway. Callers should pass the EFFECTIVE
    type/name (after any manifest entry override), not necessarily the
    embedded arrangement's own. A manifest entry's `name`/`type` is
    unschema'd YAML, so either can be a non-string (a list, a number,
    ...) -- str()'d first, same as lib/sloppak.py's load_song() coerces
    a truthy manifest override (`arr.type = str(entry["type"])...`),
    so a malformed manifest value degrades to a stringified comparison
    instead of raising AttributeError on a bare `.strip()`/`.lower()`."""
    type_str = str(arr_type) if arr_type else ""
    name_str = str(arr_name) if arr_name else ""
    if type_str.strip().lower() == "bass":
        return True
    return "bass" in name_str.lower()


def _is_unsupported_skip(reason) -> bool:
    """True for a skip reason meaning "this generator doesn't support this
    arrangement's instrument" (issue #66) — drums or an explicit allowlist
    miss — as opposed to "supported, but nothing to do" (already-has-phrases,
    not-enough-content) or a structural problem (malformed-arrangement).

    A pure string predicate classifying an arrangement's skip reason, so it
    belongs up here with _instrument_kind rather than in the I/O half below.
    """
    return isinstance(reason, str) and reason.startswith("unsupported-instrument")


# ── Tempo-relative constants ─────────────────────────────────────────────────
#
# A wall-clock constant (150ms, 0.06s, 1.0s, 2.0s) behaves completely
# differently depending on song tempo, which doesn't match how a human
# chart editor thinks (they group by beat subdivision, not milliseconds).
# These constants re-express the same tuned intent as a fraction of the
# song's own beat interval, falling back to today's absolute values when
# `beats` doesn't carry enough usable data to trust a tempo estimate.

_MIN_BEATS_FOR_TEMPO = 8  # same no-signal floor as MIN_EVENTS_FOR_GENERATION
_TEMPO_MIN_BEAT_INTERVAL_S = 0.15  # ~400bpm ceiling — guards against corrupt/duplicate beat data
_TEMPO_MAX_BEAT_INTERVAL_S = 2.5  # ~24bpm floor — same guard, other direction

_GROUP_WINDOW_BEAT_FRACTION = 0.25  # a sixteenth note: a run tighter than this reads as one cluster
_BEAT_ALIGN_TOLERANCE_FRACTION = 0.12  # reproduces the original 0.06s tolerance at ~120bpm
_FRET_JUMP_WINDOW_BEATS = 2.0  # reproduces the original 1.0s window at ~120bpm
_SUSTAIN_EASE_BEATS = 4.0  # a whole note's sustain reads as fully easy regardless of tempo


def _median_beat_interval(beat_times, *, min_beats=_MIN_BEATS_FOR_TEMPO):
    """Median seconds-per-beat from arr['beats'] — the song's own tactus
    grid, robust to a stray double-tap or missed click the way a mean
    wouldn't be. Returns None when there isn't enough (or sane-looking)
    beat data to trust a tempo estimate, so callers fall back to the
    pre-tempo-relative absolute constants and existing behavior on
    beat-less arrangements doesn't regress.
    """
    times = sorted({round(float(t), 6) for t in beat_times})
    if len(times) < min_beats:
        return None
    diffs = sorted(b - a for a, b in pairwise(times) if b > a)
    if len(diffs) < min_beats - 1:
        return None
    mid = len(diffs) // 2
    median = diffs[mid] if len(diffs) % 2 else (diffs[mid - 1] + diffs[mid]) / 2.0
    if not (_TEMPO_MIN_BEAT_INTERVAL_S <= median <= _TEMPO_MAX_BEAT_INTERVAL_S):
        return None
    return median


@dataclass(frozen=True)
class _TempoParams:
    """The tempo-relative thresholds above, resolved once per arrangement
    and threaded through the fretted scoring/refinement pipeline as a
    single object instead of four separate keyword arguments — the fields
    default to the pre-tempo-relative absolute constants, so a bare
    `_TempoParams()` reproduces the no-tempo-signal fallback exactly.
    """
    time_window_ms: float = 150.0
    beat_tolerance: float = 0.06
    fret_jump_window_seconds: float = FRET_JUMP_WINDOW_SECONDS
    sustain_ease_norm_seconds: float = 2.0
    beat_interval: float | None = None
    # Graded metrical-strength grid (#103/B2) -- (time, strength) pairs from
    # the arrangement's own beats[] (see _beat_grid), plus a times-only view
    # for bisect lookups. Both default to () so a bare _TempoParams() or a
    # from_beats() call with no `beats` argument reproduces the pre-B2
    # on/off _is_beat_aligned behavior exactly (see _beat_value) -- existing
    # callers/tests that never pass real beats are unaffected.
    beat_grid: tuple = ()
    grid_times: tuple = ()

    @classmethod
    def from_beats(cls, beat_times, beats=()):
        """Derive tempo-relative thresholds from a song's own beat grid,
        or fall back to the absolute defaults above when there's no
        trustworthy tempo signal (see _median_beat_interval). `beats` is
        the arrangement's raw beats[] wire list (time + measure); passing
        it also builds the graded metrical-strength grid (see _beat_grid)
        used by _beat_value/_syncopation_score. Omitting it (or passing
        only beat_times, as pre-B2 callers do) leaves beat_grid empty."""
        beat_interval = _median_beat_interval(beat_times)
        grid = tuple(_beat_grid(beats)) if beats else ()
        grid_times = tuple(gt for gt, _ in grid)
        if not beat_interval:
            return cls(beat_grid=grid, grid_times=grid_times)
        return cls(
            time_window_ms=beat_interval * 1000 * _GROUP_WINDOW_BEAT_FRACTION,
            beat_tolerance=beat_interval * _BEAT_ALIGN_TOLERANCE_FRACTION,
            fret_jump_window_seconds=beat_interval * _FRET_JUMP_WINDOW_BEATS,
            sustain_ease_norm_seconds=beat_interval * _SUSTAIN_EASE_BEATS,
            beat_interval=beat_interval,
            beat_grid=grid,
            grid_times=grid_times,
        )


# ── Scoring heuristic ────────────────────────────────────────────────────────

def _fret_score(fret):
    if fret <= 0:
        return 0.0
    return min(1.0, fret / 22.0)


def _fret_span(notes):
    """Raw fret span across `notes`: max - min fret among fretted notes.
    Open strings (f<=0) don't count — an unfretted string contributes no
    hand-position/reach demand. 0 when fewer than two fretted notes are
    present. Shared by _span_score (a fixed group's difficulty
    contribution) and _pick_partial_voicing (a hypothetical span if a
    candidate note were added to a growing voicing) so both agree on
    exactly what "span" means as either evolves."""
    frets = [n.get("f", 0) for n in notes if n.get("f", 0) > 0]
    if len(frets) < 2:
        return 0
    return max(frets) - min(frets)


def _span_score(notes):
    return min(1.0, _fret_span(notes) / 6.0)


def _posture_score(notes):
    """Extra low-position stretch cost, separate from absolute fret span.

    A wide shape near the nut needs a larger physical reach than the same
    fret span high on the neck. Three frets or fewer are treated as ordinary
    reach; the bonus fades out by fret 9. These are conservative heuristic
    scales, not measured player-specific hand geometry.
    """
    frets = [int(n.get("f", 0)) for n in notes if int(n.get("f", 0)) > 0]
    if len(frets) < 2:
        return 0.0
    stretch = min(1.0, max(0, max(frets) - min(frets) - 3) / 4.0)
    low_position = min(1.0, max(0.0, (9 - min(frets)) / 8.0))
    return stretch * low_position


def _string_span_score(notes, n_strings):
    """String-index spread within a group, normalized 0..1 — playing
    strings 1 and 6 together (a wide stretch/skip) is harder than playing
    adjacent strings 1 and 2, even though both "touch 2 strings." Mirrors
    _span_score's fret-spread normalization, just on the string axis
    instead of the fret axis (an orthogonal kind of "how far apart")."""
    strings = [n.get("s", 0) for n in notes]
    if len(strings) < 2:
        return 0.0
    return min(1.0, (max(strings) - min(strings)) / max(n_strings - 1, 1))


def _tech_score(n):
    # Weights track how much extra motor precision/independent coordination
    # a technique demands over plain picking/fretting. hm/hp were previously
    # scored identically (one shared +0.15) despite pinch harmonics needing
    # a precisely timed picking-hand thumb-touch immediately after the
    # strike — markedly less forgiving than a natural harmonic's fixed-node
    # touch — so they're split. plk/slp (bass pop/slap) were previously
    # unscored entirely: a slap-bass note read as no harder than a plain
    # picked note. Slap (a percussive thumb strike, a distinct right-hand
    # technique paradigm) is weighted above pop, mirroring how it's
    # generally regarded as the harder half of the "slap and pop" pairing.
    # pm/mt/vb were previously present in _TECH_GATE_FRAC (gated/stripped
    # correctly once a tier was assigned) but absent here — they contributed
    # nothing to the score that decides which tier a note lands in to begin
    # with. fhm (fret-hand mute — often paired with slap/pop for percussive
    # muted "ghost notes") was in neither: unscored AND ungated, so it
    # survived at every difficulty tier regardless of how hard the passage
    # was. Weights below are modest for pm/mt (consistent picking-hand/
    # fretting-hand pressure, but not independently demanding) and higher
    # for vb (a controlled, sustained oscillation — closer in kind to
    # tremolo) and fhm (deliberate hand-relaxation control while still
    # tracking rhythm precisely).
    #
    # bt (bend intent) and bnv (bend curve) were previously invisible to
    # scoring entirely — every bend read as the same difficulty regardless
    # of shape, even though a pre-bend (bt=2/3) means bending to the target
    # pitch BEFORE picking, with no real-time auditory feedback to correct
    # against (materially harder than hearing the pitch rise as you bend),
    # and a round-trip (bt=4) demands bidirectional control within one
    # note's sustain. Release (bt=1) is not penalized: it's a controlled
    # descent from an already-established pitch, not meaningfully harder
    # than a plain bend-up. A bnv curve with more than the trivial two
    # points a plain bn ramp already implies signals deliberate mid-bend
    # shaping (e.g. a wobble), which needs more precise control to execute.
    score = 0.0
    if n.get("bn"):
        score += 0.4
        bt = n.get("bt", 0)
        if bt in (2, 3):
            score += 0.15
        if bt in (3, 4):
            score += 0.10
        bnv = n.get("bnv")
        if bnv and len(bnv) > 2:
            score += 0.10
    if n.get("ho") or n.get("po"):
        score += 0.25
    if n.get("tp"):
        score += 0.5
    if n.get("sl", -1) >= 0 or n.get("slu", -1) >= 0:
        score += 0.2
    if n.get("tr"):
        score += 0.3
    if n.get("hm"):
        score += 0.15
    if n.get("hp"):
        score += 0.35
    if n.get("plk"):
        score += 0.30
    if n.get("slp"):
        score += 0.45
    if n.get("pm"):
        score += 0.15
    if n.get("mt"):
        score += 0.15
    if n.get("vb"):
        score += 0.25
    if n.get("fhm"):
        score += 0.20
    return min(1.0, score)


# Category names mirror _tech_score's own groupings, so a note's set of
# active categories is exactly the set of terms that contributed to its
# _tech_score. ho/po (hammer-on/pull-off) share one category, as do
# sl/slu (slide/slide-up) -- _tech_score itself scores each of those pairs
# identically and doesn't distinguish them, so treating them as one
# category avoids reporting a coordination hit that _tech_score doesn't
# actually recognize as two different techniques.
# Single wire-flag -> category name. ho/po and sl/slu aren't here: each
# pair maps to one category from either of two flags (an "or", not a
# lookup on one key), so they stay as explicit checks below.
_TECHNIQUE_FLAG_CATEGORIES = {
    "bn": "bend", "tp": "tap", "tr": "trem", "hm": "harm_nat",
    "hp": "harm_pinch", "plk": "pluck", "slp": "slap", "pm": "palm_mute",
    "mt": "string_mute", "vb": "vibrato", "fhm": "fret_mute",
}


def _technique_categories(n):
    """The set of distinct technique categories active on note `n` (see
    _tech_score) -- used by _technique_coordination_bonus (#72/B4) to score
    coordination demand separately from _tech_score's own max-single-note
    difficulty. Bend intent/curve (bt/bnv) refine the 'bend' category's
    _tech_score weight but don't add a category of their own -- they can't
    occur without `bn`, so they'd never contribute a category _tech_score
    doesn't already count."""
    cats = {cat for flag, cat in _TECHNIQUE_FLAG_CATEGORIES.items() if n.get(flag)}
    if n.get("ho") or n.get("po"):
        cats.add("hopo")
    if n.get("sl", -1) >= 0 or n.get("slu", -1) >= 0:
        cats.add("slide")
    return cats


# Per extra simultaneous technique category beyond the hardest one already
# counted by _tech_score's max, and per switch to a different technique set
# than the immediately preceding group. Heuristic weights (not measured
# against real players), capped low relative to _tech_score's own 0-1 range
# so a single very hard technique (_tech_score's max term) still dominates
# over coordination alone -- coordination compounds an existing demand, it
# doesn't replace judging which demand is hardest.
_COORD_PER_EXTRA_CATEGORY = 0.08
_COORD_SWITCH_BONUS = 0.06
_COORD_MAX_BONUS = 0.20


def _technique_coordination_bonus(group_categories, prev_categories):
    """Coordination-demand bonus on top of _tech_score's max-only term
    (#72/B4): planning and linking movements is harder than any one of them
    alone (motor-sequence literature; see #103's B4 entry), so a chord
    mixing a bend, a palm mute and a slide should score above a lone bend,
    and a passage that keeps switching technique between neighbouring
    groups should score above one that repeats the same technique.

    `group_categories` is this group's union of _technique_categories(n)
    across its notes (simultaneous demand); `prev_categories` is the same
    for the immediately preceding TECHNIQUE-BEARING group (sequential
    demand) -- not necessarily the physically adjacent group -- or an empty
    set if there is none yet. The caller (_score_groups) only updates its
    carried `prev_categories` when a group actually uses a technique, so a
    plain-picked or defensively-empty group in between doesn't count as
    "the group before": [bend] -> [plain] -> [palm_mute] still registers as
    a switch (the player changed technique since the last time one was
    active), while comparing against literally-adjacent-only would miss
    that switch whenever anything plain sits between the two technique
    passages. Returns 0.0 whenever a group uses at most one technique and
    doesn't change it from the last technique-bearing group -- the common
    case -- so single-technique passages are unaffected."""
    if not group_categories:
        return 0.0
    simultaneous = max(0, len(group_categories) - 1) * _COORD_PER_EXTRA_CATEGORY
    switched = (
        _COORD_SWITCH_BONUS
        if prev_categories and group_categories != prev_categories
        else 0.0
    )
    return min(_COORD_MAX_BONUS, simultaneous + switched)


def _cluster_covered_by_hand_shape(cluster, hand_shapes):
    """True when an authored `HandShape` window (wire keys `start_time`/
    `end_time`) covers every note's onset in `cluster` — the chart's own
    author linked these onsets into one playable shape (block chord or
    arpeggio; the `arp` flag doesn't change the grouping decision, only
    real chord-editing tools would draw that distinction), not a time
    window this generator invented after the fact. This is the strongest
    of the three evidence signals `_classify_cluster` checks (issue #73)
    because it comes straight from the source chart rather than being
    inferred from onset proximity."""
    if not hand_shapes:
        return False
    times = [float(n.get("t", 0)) for n in cluster]
    lo, hi = min(times), max(times)
    for hs in hand_shapes:
        start = float(hs.get("start_time", 0))
        end = float(hs.get("end_time", 0))
        if start <= lo and hi < end:
            return True
    return False


def _cluster_notes_overlap(cluster):
    """True when any two notes in the cluster ring simultaneously — one
    note's sustain window hasn't ended before the next note's onset. A
    broken chord's constituent notes are typically left to ring into each
    other; a fast melodic run's notes typically aren't (each cuts off
    before the next begins), so overlap is real evidence the notes were
    meant to sound together rather than an artifact of this generator's
    own time-window clustering."""
    ordered = sorted(cluster, key=lambda n: float(n.get("t", 0)))
    for i in range(len(ordered) - 1):
        end_i = float(ordered[i].get("t", 0)) + float(ordered[i].get("sus", 0))
        if end_i > float(ordered[i + 1].get("t", 0)) + 1e-9:
            return True
    return False


def _cluster_matches_chord_shape(cluster, chord_templates):
    """True when the cluster's per-string frets are an exact subset of an
    authored `ChordTemplate`'s fingering (wire key `frets`, indexed by
    string — see `_notes_for_level`'s chord-reduction docstring for the
    same indexing convention) AND that subset covers a meaningful share of
    the template's own fretted/used strings — not just any coincidental
    subset. Without the share requirement, a 2-note cluster that happens
    to land on two strings of a large template (e.g. a passing interval
    that coincidentally matches 2 of a 6-string open chord's 5 used
    strings) would read as chord-identity evidence, which is exactly the
    false-positive class issue #73 set out to eliminate -- caught in
    review on PR #100 (pullfrog).

    "Meaningful share" here is: the cluster covers at least 3 of the
    template's own strings, or at least half of them (rounded up) —
    whichever is the lower bar. A cluster that fully matches a small
    template (e.g. a 2-string power-chord shape) still counts even though
    it's only 2 notes, since 2-of-2 is the whole shape, not a coincidental
    fragment of a larger one.

    Returns the matched `ChordTemplate` dict itself (truthy) rather than a
    bare `True` -- #103/B7 uses the template's own `name` (e.g. "Am7") to
    parse a real harmonic root for chord/arpeggio reduction; callers that
    only need the boolean (the original contract) can keep using it in a
    truthiness check unchanged."""
    if not chord_templates:
        return None
    by_string = {}
    for n in cluster:
        by_string[n.get("s", 0)] = n.get("f", 0)
    if len(by_string) < 2:
        return None
    for ct in chord_templates:
        frets = ct.get("frets") or []
        if not all(0 <= s < len(frets) and frets[s] == f for s, f in by_string.items()):
            continue
        template_used = sum(1 for fr in frets if fr >= 0)
        if template_used < 2:
            continue
        min_share = min(3, math.ceil(template_used / 2))
        if len(by_string) >= min_share:
            return ct
    return None


def _classify_cluster(cluster, *, hand_shapes=None, chord_templates=None):
    """Classify a time/fret-proximity cluster of different-string notes as
    `"arpeggio"` (a genuine implicit broken chord, eligible for the
    lowest-string (bass-note) anchor reduction `_notes_for_level` applies at
    the bottom tier) or `"run"` (an unsubstantiated melodic sequence,
    preserved across the ladder instead of collapsed toward one presumed
    anchor note).

    Before issue #73, ANY different-string notes landing inside the
    grouping time/fret window became an "arpeggio" with no further
    evidence — a fast cross-string scale run read identically to a genuine
    broken chord, and the bottom tier would reduce either one down to a
    single note. This requires at least one of three independent evidence
    signals — an authored hand-shape linking the notes (`_cluster_
    covered_by_hand_shape`), overlapping sustain windows (`_cluster_
    notes_overlap`), or a fret pattern matching a known chord template
    (`_cluster_matches_chord_shape`) — before treating a cluster as
    chord-like. Absent all three, the notes are a melodic sequence, not an
    arpeggio, and are classified `"run"` so `_notes_for_level` preserves
    their shape instead of picking a "root".
    """
    if len(cluster) < 2:
        return "note"
    if _cluster_covered_by_hand_shape(cluster, hand_shapes):
        return "arpeggio"
    if _cluster_notes_overlap(cluster):
        return "arpeggio"
    if _cluster_matches_chord_shape(cluster, chord_templates):
        return "arpeggio"
    return "run"


def _group_notes(notes, chords, *, time_window_ms=150, fret_span_max=4,
                  hand_shapes=None, chord_templates=None):
    """Group flat wire notes/chords into atomic difficulty-scoring units.

    Simplified relative to a full chart editor's grouping (no link_next
    chain) — explicit chords, then time-proximity clusters of otherwise-solo
    notes, then leftover individual notes. A multi-note cluster is only ever
    labeled `"arpeggio"` when `_classify_cluster` finds real evidence for it
    (issue #73); otherwise it's labeled `"run"` — a fast scale or other
    melodic sequence that time/fret proximity alone doesn't prove is a
    broken chord. `hand_shapes`/`chord_templates` (the arrangement's
    authored `handshapes`/`templates` wire lists, optional) feed that
    evidence check; omitting them just means the authored-linkage and
    chord-identity signals aren't available and classification falls back
    to the overlap check alone.
    """
    groups = []
    for ch in chords:
        groups.append({
            "type": "chord", "notes": list(ch.get("notes", []) or []), "chord": ch,
            "time": float(ch.get("t", 0)), "cost": 0.0, "value": 0.0,
            "retention_score": 0.0, "level": 0,
        })

    solo = sorted((dict(n) for n in notes), key=lambda n: float(n.get("t", 0)))
    used = set()
    for i, n in enumerate(solo):
        if i in used:
            continue
        cluster = [n]
        cluster_strings = {n.get("s", 0)}
        frets = [n.get("f", 0)] if n.get("f", 0) > 0 else []
        for j in range(i + 1, len(solo)):
            if j in used:
                continue
            m = solo[j]
            dt_ms = (float(m.get("t", 0)) - float(n.get("t", 0))) * 1000
            if dt_ms > time_window_ms:
                break
            if m.get("s", 0) in cluster_strings:
                continue
            m_fret = m.get("f", 0)
            all_frets = frets + ([m_fret] if m_fret > 0 else [])
            if all_frets and (max(all_frets) - min(all_frets)) > fret_span_max:
                continue
            cluster.append(m)
            cluster_strings.add(m.get("s", 0))
            if m_fret > 0:
                frets.append(m_fret)
            used.add(j)
        used.add(i)
        cluster_type = _classify_cluster(
            cluster, hand_shapes=hand_shapes, chord_templates=chord_templates,
        )
        # #103/B7: when the cluster's shape matched a named ChordTemplate
        # (the same evidence _classify_cluster already required to call it
        # an "arpeggio"), remember the template's name so the bottom-tier
        # reduction in _notes_for_level can parse a real harmonic root
        # instead of always falling back to the lowest-string heuristic.
        chord_template_name = None
        if cluster_type == "arpeggio":
            matched = _cluster_matches_chord_shape(cluster, chord_templates)
            if matched:
                chord_template_name = matched.get("name")
        groups.append({
            "type": cluster_type,
            "notes": cluster, "chord": None,
            "chord_template_name": chord_template_name,
            "time": float(cluster[0].get("t", 0)), "cost": 0.0, "value": 0.0,
            "retention_score": 0.0, "level": 0,
        })

    groups.sort(key=lambda g: g["time"])
    return groups


def _group_anchor_note(group, *, prefer_fretted=True):
    """Return the fretted group's lowest-string-index / position anchor.

    Picks `min(s)` among the group's notes — the note on the lowest string
    index. feedpak's wire `s` (and `ChordTemplate.frets`/`fingers`) is
    indexed low-string-first (feedpak-v1.md §6.2/§6.6, `song.py`'s
    `_TUNING_BASE_MIDI`, gp2rs's `_gp_string_to_rs`), so this is the
    lowest-*pitched* (bass-most) note. In standard open and barre shapes
    that is usually the chord's root, which is why reductions keep it —
    but it is a positional heuristic, not a proven harmonic root: an
    inversion's or slash chord's bass is not its root, and without an
    authored chord identity (a matching `ChordTemplate` via the chord
    event's wire `id`, or —
    for a solo cluster — the evidence `_classify_cluster` checks, issue
    #73) there's no way to tell. (Before PR #101 this took `max(s)`, the
    treble-most note.)

    `prefer_fretted` (default True — used by the fret-jump scoring and
    lower-tier bridging below) picks a fretted note (f > 0) over an open
    one when the group has both: an open string needs no hand position at
    all, so letting it win as the anchor hides where the hand actually is,
    producing bogus fret-jump distances. `_notes_for_level`'s bottom-tier
    arpeggio note selection passes `prefer_fretted=False` — there the goal
    is this same lowest-string-index anchor regardless of fretted state,
    since an open string at that index is a valid (indeed easier)
    simplification for the bottom tier, not a hand-position signal.
    """
    notes = group.get("notes", []) or []
    if prefer_fretted:
        fretted = [n for n in notes if n.get("f", 0) > 0]
        notes = fretted or notes
    return min(notes, key=lambda n: n.get("s", 0), default=None)


# #103/B7: a chord TEMPLATE's authored `name` (e.g. "Am7", "G/B") names a
# real harmonic root, which is strictly better evidence than
# `_group_anchor_note`'s lowest-string-index guess (see its docstring: "not
# a proven harmonic root" for an inversion or slash chord). Only the root
# letter before any "/" is parsed -- a slash chord's bass note is a
# separate, unparsed concept and isn't needed here (the reduction picks the
# chord's ROOT, not its bass).
_NOTE_NAME_TO_PITCH_CLASS = {
    "C": 0, "B#": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4,
    "FB": 4, "E#": 5, "F": 5, "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8,
    "A": 9, "A#": 10, "BB": 10, "B": 11, "CB": 11,
}
_CHORD_ROOT_NAME_RE = re.compile(r"^\s*([A-Ga-g])([#b]?)")


def _parse_chord_root_pitch_class(name):
    """Extract the harmonic root's pitch class (0-11) from a chord
    template's authored `name`. Returns None when `name` is missing, not a
    string, or doesn't start with a recognizable note letter -- callers
    fall back to the lowest-string heuristic in that case (#103/B7)."""
    if not name or not isinstance(name, str):
        return None
    m = _CHORD_ROOT_NAME_RE.match(name)
    if not m:
        return None
    key = (m.group(1) + m.group(2)).upper()
    return _NOTE_NAME_TO_PITCH_CLASS.get(key)


def _find_note_by_pitch_class(notes, root_pc, tuning, n_strings, is_bass):
    """Among `notes`, return the lowest-string-index note whose approximate
    pitch class (see `_approx_pitch`) matches `root_pc`, or None when no
    note matches -- the caller falls back to the lowest-string heuristic in
    that case (#103/B7)."""
    if root_pc is None:
        return None
    candidates = [
        n for n in notes
        if _approx_pitch(n, tuning, n_strings, is_bass) % 12 == root_pc
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda n: n.get("s", 0))


def _is_beat_aligned(t, beat_times, tolerance=0.06):
    return any(abs(float(t) - beat) <= tolerance for beat in beat_times)


# Graded metrical strength (#103/B2, Palmer & Krumhansl 1990): a bar
# downbeat is the strongest position, the mid-bar strong beat (beat 3 of a
# 4-beat bar) next, other beats weaker still, then eighth- and
# sixteenth-note subdivisions, then off the grid entirely. The literature
# supports this ORDER, not these specific numbers -- heuristic weights,
# not measured player thresholds.
_STRENGTH_DOWNBEAT = 1.0
_STRENGTH_STRONG_BEAT = 0.75
_STRENGTH_OTHER_BEAT = 0.5
_STRENGTH_EIGHTH = 0.25
_STRENGTH_SIXTEENTH = 0.1
_STRENGTH_OFF_GRID = 0.0

# How close (as a fraction of one beat interval) an onset must land to an
# eighth (0.5) or sixteenth (0.25/0.75) subdivision point to count as that
# subdivision rather than off-grid.
_SUBDIVISION_TOLERANCE_FRACTION = 0.12


def _beat_grid(beats):
    """Graded (time, strength) pairs for every entry in the arrangement's
    own beats[] wire list, ranked by metrical position (#103/B2) -- derived
    purely from beat spacing and the existing `measure` flag (feedpak-spec
    Beat.measure: >=0 is a downbeat, -1 is not), no new pack data needed.

    A downbeat is strength 1.0; the beat exactly halfway through a 4-beat
    bar (beat 3) is next; every other beat entry (including any bar whose
    beat count isn't 4, since we can't tell where a "beat 3" would fall in
    an irregular or undetected meter) is a flat, weaker "other beat" --
    guessing a strong-beat position that may not exist in that meter would
    be worse than not grading it. Beats before the first downbeat (a
    pickup) are graded as "other beat" too, since there's no downbeat yet
    to anchor a position count to.

    Returns [] when `beats` has no entries or no downbeats at all -- the
    "no usable downbeat grid" case callers (_beat_value, _syncopation_score)
    fall back from.
    """
    entries = sorted(
        (float(b.get("time", 0)), int(b.get("measure", -1)))
        for b in beats if isinstance(b, dict)
    )
    if not entries:
        return []
    downbeat_positions = [i for i, (_, m) in enumerate(entries) if m >= 0]
    if not downbeat_positions:
        return []
    grid = []
    for seg_i, start in enumerate(downbeat_positions):
        end = downbeat_positions[seg_i + 1] if seg_i + 1 < len(downbeat_positions) else len(entries)
        beats_in_measure = end - start
        for offset in range(beats_in_measure):
            t = entries[start + offset][0]
            if offset == 0:
                strength = _STRENGTH_DOWNBEAT
            elif beats_in_measure == 4 and offset == 2:
                strength = _STRENGTH_STRONG_BEAT
            else:
                strength = _STRENGTH_OTHER_BEAT
            grid.append((t, strength))
    if downbeat_positions[0] > 0:
        for idx in range(downbeat_positions[0]):
            grid.append((entries[idx][0], _STRENGTH_OTHER_BEAT))
    grid.sort()
    return grid


def _beat_strength(t, tempo):
    """Graded metrical strength of onset time `t` against `tempo.beat_grid`
    (see _beat_grid): an exact beat match returns that beat's graded
    strength; otherwise `t` is tested against eighth/sixteenth subdivision
    points of the surrounding beat interval; otherwise off-grid (0.0).
    Callers must already know `tempo.beat_grid` is non-empty and
    `tempo.beat_interval` is truthy (see _beat_value) -- this returns
    _STRENGTH_OFF_GRID rather than raising if called without them anyway.
    """
    grid, grid_times = tempo.beat_grid, tempo.grid_times
    if not grid or not tempo.beat_interval:
        return _STRENGTH_OFF_GRID
    tol = tempo.beat_tolerance
    i = bisect.bisect_left(grid_times, t)
    for j in (i - 1, i):
        if 0 <= j < len(grid_times) and abs(grid_times[j] - t) <= tol:
            return grid[j][1]
    left_idx = max(0, min(i - 1, len(grid_times) - 1))
    left_t = grid_times[left_idx]
    right_idx = left_idx + 1
    # Local interval between the two actual surrounding grid beats, not the
    # arrangement-wide median -- a tempo change would otherwise misclassify
    # subdivisions in the deviating passage (caught in PR #123 review).
    # Only the trailing edge (t past the last known beat) has no "next"
    # beat to measure from, so it falls back to the median as a reasonable
    # extrapolation.
    local_interval = (
        grid_times[right_idx] - left_t if right_idx < len(grid_times)
        else tempo.beat_interval
    )
    # A tempo change and a MISSING beat (a dropped entry in beats[], a
    # click-track glitch) are indistinguishable from the grid alone -- a
    # missing beat doubles (or more) the local cell width, spanning two
    # true intervals, which would then mis-locate every subdivision in that
    # cell rather than just failing to grade one (caught in PR #123
    # review). Bound the local interval to a small multiple of the
    # arrangement-wide median: within bound, trust it as a real tempo
    # change (as intended above); an outsized cell degrades to off-grid
    # (a no-grade) rather than a confidently wrong grade.
    if (
        not local_interval or local_interval <= 0
        or local_interval > 2.0 * tempo.beat_interval
    ):
        return _STRENGTH_OFF_GRID
    frac = ((float(t) - left_t) / local_interval) % 1.0
    if abs(frac - 0.5) <= _SUBDIVISION_TOLERANCE_FRACTION:
        return _STRENGTH_EIGHTH
    if min(abs(frac - 0.25), abs(frac - 0.75)) <= _SUBDIVISION_TOLERANCE_FRACTION:
        return _STRENGTH_SIXTEENTH
    return _STRENGTH_OFF_GRID


def _beat_value(t, beat_times, tempo):
    """Beat-alignment retention value: graded metrical strength (see
    _beat_strength) when a downbeat grid is available, else the legacy
    on/off _is_beat_aligned check. The empty-grid path reproduces pre-B2
    output exactly (#104's fallback acceptance criterion) -- every existing
    caller/fixture that never threads real beats[] data through
    _TempoParams.from_beats keeps its old 0.0/1.0 value."""
    if tempo.beat_grid and tempo.beat_interval:
        return _beat_strength(t, tempo)
    return float(_is_beat_aligned(t, beat_times, tolerance=tempo.beat_tolerance))


def _syncopation_score(gi, times_sorted, beat_times, tempo):
    """Syncopation of group `gi`'s onset.

    With a usable downbeat grid (see _beat_grid), this is a
    Longuet-Higgins & Lee (1984) style measure: a note on a metrically weak
    position is syncopated when a metrically STRONGER position between it
    and the next onset falls silent -- e.g. a note struck on the "and" of
    beat 2 and held through beat 3 is more syncopated than the same
    off-beat note followed immediately by a note ON beat 3, because beat 3
    stays silent in the first case. Magnitude is the strength gap between
    this group's own metrical position and the strongest silent position
    skipped before the next onset (or one beat interval past this onset,
    for the last group). 0.0 when nothing stronger is skipped (including a
    note that already sits on the strongest available position).

    Without a usable grid, falls back to the legacy nearest-beat-distance
    measure (a fraction of a half-beat; landing between two beats maxes
    out at 1.0) -- reproduces pre-B2 output exactly for fixtures without
    downbeat data.

    Returns 0.0 (safe no-op) when there's no beat grid or no trustworthy
    tempo at all — this is a refinement layered onto the existing
    note-count density signal, not a replacement, so absent tempo data
    must not silently zero out density scoring altogether.
    """
    t = float(times_sorted[gi])
    if tempo.beat_grid and tempo.beat_interval:
        own_strength = _beat_strength(t, tempo)
        next_onset = (
            float(times_sorted[gi + 1]) if gi + 1 < len(times_sorted)
            else t + tempo.beat_interval
        )
        grid, grid_times = tempo.beat_grid, tempo.grid_times
        start = bisect.bisect_right(grid_times, t + tempo.beat_tolerance)
        strongest_silent = 0.0
        for idx in range(start, len(grid)):
            gt, strength = grid[idx]
            if gt >= next_onset - tempo.beat_tolerance:
                break
            if strength > own_strength:
                strongest_silent = max(strongest_silent, strength)
        return max(0.0, strongest_silent - own_strength)
    if not beat_times or not tempo.beat_interval:
        return 0.0
    nearest = min(abs(t - b) for b in beat_times)
    return min(1.0, nearest / (tempo.beat_interval / 2.0))


# Syncopation blended into the density sub-score at this weight — enough to
# separate a straight run of eighth notes from an equally-dense syncopated
# off-beat pattern (independently harder to read per basic rhythm
# pedagogy) without letting off-grid-ness alone dominate over actual note
# count, which stays the primary density signal.
_SYNCOPATION_DENSITY_WEIGHT = 0.30

# Phrase-boundary retention nudge (#103/B3) applied to a phrase's first and
# last group, same direction/mechanism as the 0.12 beat-alignment discount
# above but smaller: GTTM phrase-grouping evidence is moderate ("listeners
# split music into phrases"), not the strong beat-position evidence B2 has,
# and that keeping boundary notes specifically helps LEARNING is inferred,
# not tested. Heuristic weight, not measured against real players.
_PHRASE_BOUNDARY_RETENTION_BONUS = 0.10

# Spread (how far apart the touched strings are) weighs slightly more than
# raw string count in the hand-shape sub-score — a wide stretch across few
# strings is characteristically harder than a full barre across many
# adjacent ones (basic fretting-hand ergonomics), but count still matters
# (more independent digits needed), so it isn't dropped entirely.
_STRING_SPREAD_BLEND = 0.6

# String-jump bonus, parallel to the existing fret-jump bonus below: a
# skip across 4+ strings between consecutive lower-tier anchors is a hand-
# shape change severe enough to nudge difficulty up, independent of how
# far the fret position itself moved.
_STRING_JUMP_THRESHOLD = 3
_STRING_JUMP_COEF = 0.03
_STRING_JUMP_MAX_BONUS = 0.08  # capped below the movement bonus (0.10) — a secondary signal

# Fitts's index of difficulty uses a two-fret target width as a conservative
# approximation of usable hand-position tolerance. The reference 8-fret
# shift maps one full time-pressure unit to a conservative 0.10 bonus;
# unlike the old >5-fret step, shorter shifts still have a smaller cost.
_SHIFT_TARGET_WIDTH_FRETS = 2.0
_SHIFT_REFERENCE_FRETS = 8.0
_SHIFT_MAX_BONUS = 0.10


def _fitts_shift_bonus(distance, available_seconds, tempo):
    """Bounded hand-shift cost; a longer interval lowers the same move.

    ``tempo.fret_jump_window_seconds`` is a tempo-relative pressure scale,
    no longer a hard cutoff. This is a model of relative difficulty, not an
    estimate of actual human movement time.
    """
    if distance <= 0:
        return 0.0
    index = math.log2(distance / _SHIFT_TARGET_WIDTH_FRETS + 1.0)
    reference = math.log2(_SHIFT_REFERENCE_FRETS / _SHIFT_TARGET_WIDTH_FRETS + 1.0)
    time_scale = max(float(tempo.fret_jump_window_seconds), 0.001)
    pressure = time_scale / (time_scale + max(float(available_seconds), 0.0))
    return min(_SHIFT_MAX_BONUS, _SHIFT_MAX_BONUS * index / reference * pressure)

# How many beats wide the sequential-density window is on EACH side of a
# group's own onset (issue #71). Sized in beats, not seconds, so the same
# rhythmic pattern -- straight eighth notes, say -- scores the same
# density at 80 BPM and at 160 BPM, instead of the absolute-time-per-note
# gap between them producing different scores for musically equivalent
# content.
_DENSITY_WINDOW_BEATS = 2.0
# Onsets-per-window that saturates the density score at 1.0. Chosen so a
# genuinely dense passage (an onset every ~half beat within the window)
# still reaches the top of the 0..1 range.
_DENSITY_SATURATION_ONSETS = 8.0
# Absolute-time fallback window (seconds, each side), used only when no
# trustworthy beat grid exists (_TempoParams.beat_interval is None) and
# there is therefore no tempo to be relative to.
_DENSITY_WINDOW_SECONDS_FALLBACK = 1.0


def _sequential_density(times_sorted, gi, tempo):
    """Tempo-relative sequential event density around group index `gi`.

    Counts DISTINCT ONSETS (groups) within a time window, not each nearby
    group's constituent note count — a single wide chord shouldn't inflate
    density on its own, since simultaneous polyphony is already scored
    separately (fretting/string_shape here, `poly` in the keys path) and
    must not double up with sequential density (#71). `times_sorted` must
    be sorted ascending (grouping already produces groups in time order).
    """
    half_window = (
        _DENSITY_WINDOW_BEATS * tempo.beat_interval
        if tempo.beat_interval else _DENSITY_WINDOW_SECONDS_FALLBACK
    )
    t = times_sorted[gi]
    lo = bisect.bisect_left(times_sorted, t - half_window)
    hi = bisect.bisect_right(times_sorted, t + half_window)
    return min(1.0, (hi - lo) / _DENSITY_SATURATION_ONSETS)


# #103/B5 (Dowling, 1978): beginners remember a melody's rising-and-falling
# shape before its exact intervals, so thinning a single-note line can erase
# that shape even when none of its individual notes are otherwise "hard" by
# the cost model below. This nudges each local high or low note in a
# single-note passage to survive thinning a little longer, the same weight
# a downbeat gets from `value` (see _beat_value). Chord/cluster groups
# (`len(notes) != 1`) never participate — chord-heavy passages are
# unaffected by construction, not by a special case.
_MELODY_TURNING_POINT_RETENTION_BONUS = 0.12

# Approximate semitone offsets for a standard tuning, low string to high,
# keyed by string count. This ranks pitch DIRECTION (rising vs falling) for
# melody-shape retention -- not an exact pitch. It ignores capo and treats
# every string as standard-interval-spaced, which is wrong for drop/altered
# tunings and any non-standard interval between two particular strings, but
# a wrong interval size still preserves note-to-note direction almost
# always (a fret difference big enough to flip apparent direction across a
# wrongly-sized interval is the rare case), which is all a turning point
# needs. The arrangement's own per-string `tuning` offsets (already
# available at every call site) are added on top where given.
#
# Only the 5-string row is instrument-dependent, matching feedBack core's
# own `base_open_string_midis(string_count, is_bass)` contract (lib/song.py):
# a 5-string BASS is all perfect fourths (B-E-A-D-G -> 0,5,10,15,20), while
# a 5-string NON-bass (a guitar voicing) borrows the low strings of the
# 6-string base instead -- so its top interval is 19, same as 6-string
# guitar's own major-third B-string, not 20. (4-string is instrument-
# independent: both the bass base and the borrowed 6-string prefix give
# the same (0,5,10,15) shape, since the major third only appears between
# strings 5 and 6.) Getting the 5-string case wrong isn't just imprecise
# the way a wrong interval size usually is (see above): a 1-semitone error
# here can turn a genuine turning point into an exact tie, which the
# strict `>`/`<` comparison below then rejects outright, rather than just
# misjudging its size.
_STANDARD_STRING_INTERVALS = {
    4: (0, 5, 10, 15),
    6: (0, 5, 10, 15, 19, 24),
    7: (-5, 0, 5, 10, 15, 19, 24),
    8: (-10, -5, 0, 5, 10, 15, 19, 24),
}
_STANDARD_STRING_INTERVALS_5_BASS = (0, 5, 10, 15, 20)
_STANDARD_STRING_INTERVALS_5_GUITAR = (0, 5, 10, 15, 19)


def _string_intervals(n_strings, is_bass):
    if n_strings == 5:
        return _STANDARD_STRING_INTERVALS_5_BASS if is_bass else _STANDARD_STRING_INTERVALS_5_GUITAR
    return _STANDARD_STRING_INTERVALS.get(n_strings, _STANDARD_STRING_INTERVALS[6])


def _approx_pitch(note, tuning, n_strings, is_bass):
    s = int(note.get("s", 0))
    f = int(note.get("f", 0))
    intervals = _string_intervals(n_strings, is_bass)
    base = intervals[s] if 0 <= s < len(intervals) else s * 5
    offset = int(tuning[s]) if 0 <= s < len(tuning) else 0
    return base + offset + f


def _melody_turning_points(groups, tuning, n_strings, tempo, is_bass=False):
    """Return the set of group indices among single-note groups
    (`len(notes) == 1`) that are a strict local high or low among the
    OTHER single-note groups in the arrangement -- chords/clusters are
    skipped when looking for neighbors, since they aren't part of the
    single-note melodic line. A repeated pitch (equal to a neighbor) is
    not a turning point: the contour hasn't changed direction there.

    A candidate neighbor more than `tempo.fret_jump_window_seconds` away
    (the same "long enough that this isn't one continuous passage"
    threshold `_score_groups`'s fret-jump/string-jump bonuses already use)
    doesn't count -- otherwise two single notes either side of an
    intervening chord section, or either side of an unrelated authored
    phrase, could sit next to each other in `singles` and look like a
    contour turn that was never actually played that way."""
    singles = [i for i, g in enumerate(groups) if len(g["notes"]) == 1]
    pitches = {i: _approx_pitch(groups[i]["notes"][0], tuning, n_strings, is_bass) for i in singles}
    times = {i: float(groups[i]["time"]) for i in singles}
    max_gap = tempo.fret_jump_window_seconds
    turning = set()
    for k in range(1, len(singles) - 1):
        i, prev_i, next_i = singles[k], singles[k - 1], singles[k + 1]
        if times[i] - times[prev_i] > max_gap or times[next_i] - times[i] > max_gap:
            continue
        p, prev_p, next_p = pitches[i], pitches[prev_i], pitches[next_i]
        if (p > prev_p and p > next_p) or (p < prev_p and p < next_p):
            turning.add(i)
    return turning


# #103/B7 (Krumhansl & Kessler, 1982): a section's tonal key predicts which
# of its notes a listener hears as "structural" (tonic > chord tones > other
# scale tones > chromatic passing tones) versus ornamental -- keeping the
# structural ones longest, on top of (not instead of) the existing
# mechanical/metrical/melodic-shape signals, should make thinned tiers sound
# less broken. This is the classic Krumhansl-Schmuckler key-finding
# algorithm: correlate a duration-weighted pitch-class histogram against
# the 24 rotations of the major/minor key profiles below.
_KS_MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
_KS_MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)

# Guard (#103/B7's own acceptance criterion): a section whose best-fit key
# correlation falls below this is disabled from the key-stability
# weighting entirely, rather than confidently ranking notes against a key
# estimate the data doesn't actually support. Measured against synthetic
# uniform-weight pitch-class-set histograms, this threshold only rejects
# NEAR-UNIFORM/atonal content -- whole-tone (~0.07) and fully chromatic
# (0.0) correlate far below it. Ordinary diatonic, modal, and even blues
# content (corr ~0.59-0.76 depending on mode) clears it easily, so despite
# this guard's name it is NOT a "blues/modal detector" -- those are
# expected to pass and get the weighting like any other tonal material.
# What it mainly catches in practice is a section with too little content
# to estimate a key from at all (very short/sparse phrases can still
# report a misleadingly high correlation for an essentially arbitrary
# tonic -- a real limitation of a single correlation threshold with no
# minimum-support term, left as a known gap rather than in scope here).
_KEY_FIT_MIN_CORRELATION = 0.55

# Deliberately smaller than _MELODY_TURNING_POINT_RETENTION_BONUS (0.12) --
# #103/B7's guard requires the key-stability weight to sit BELOW metrical
# strength (_beat_value's 0.12 coefficient), since beat position is a much
# stronger, better-evidenced retention signal (#103/B2) than a heuristic
# key estimate is.
_KEY_STABILITY_RETENTION_BONUS = 0.08

_MAJOR_TRIAD_PITCH_CLASSES = {0, 4, 7}
_MINOR_TRIAD_PITCH_CLASSES = {0, 3, 7}
_MAJOR_SCALE_PITCH_CLASSES = {0, 2, 4, 5, 7, 9, 11}
_MINOR_SCALE_PITCH_CLASSES = {0, 2, 3, 5, 7, 8, 10}
# tonic(3) > chord-tone-in-triad(2) > other-scale-tone(1) > chromatic(0);
# +0.5 when the note is also part of a chord currently sounding (a chord
# tone ranks above a passing note of the same category -- #103/B7).
_KEY_STABILITY_RANK_MAX = 3.5


def _pitch_class_histogram(groups, tuning, n_strings, is_bass, *, is_keys=False):
    """Duration-weighted pitch-class histogram (12 bins) over every note in
    `groups`, using `_approx_pitch`'s direction-preserving approximation
    (already tuning/instrument-aware) mod 12. A note with no `sus` (a
    struck, undampened single hit) still contributes a nominal weight
    rather than zero -- an unweighted note shouldn't vanish from the
    profile just because its wire data doesn't carry a duration.

    #179: when `is_keys` is set, pitch is read directly off the note's
    REAL MIDI value (`_note_midi_keys`) instead of `_approx_pitch`'s
    string/fret+tuning approximation -- keys notes carry absolute pitch
    (midi = string*24 + fret) with no fretboard to approximate from, so
    the estimate is simpler, not unavailable (the reason this weighting
    was skipped for keys in the first place)."""
    hist = [0.0] * 12
    pitch_fn = (lambda n: _note_midi_keys(n)) if is_keys else (
        lambda n: _approx_pitch(n, tuning, n_strings, is_bass)
    )
    for g in groups:
        for n in g.get("notes", []) or []:
            pc = pitch_fn(n) % 12
            hist[pc] += float(n.get("sus", 0)) or 0.25
    return hist


def _pearson_correlation(a, b):
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    numerator = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    denom_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    denom_b = math.sqrt(sum((x - mean_b) ** 2 for x in b))
    if denom_a == 0.0 or denom_b == 0.0:
        return 0.0
    return numerator / (denom_a * denom_b)


def _estimate_key(groups, tuning, n_strings, is_bass, *, is_keys=False):
    """Krumhansl-Schmuckler key estimate for one section's `groups`.

    Returns `(tonic_pitch_class, is_major, correlation)` for the
    best-fitting of the 24 major/minor rotations, or None when the section
    has no notes at all (an empty phrase). `correlation` is the raw Pearson
    r against that rotation -- callers gate on `_KEY_FIT_MIN_CORRELATION`
    before trusting the estimate (#103/B7's "disable for a poor-fit
    section" guard).

    #179: when `is_keys` is set, the histogram is built from the notes'
    real MIDI pitch (`_note_midi_keys`) rather than `_approx_pitch`'s
    string/fret approximation -- keys notes carry absolute pitch, so the
    estimate is simpler, not unavailable."""
    hist = _pitch_class_histogram(groups, tuning, n_strings, is_bass, is_keys=is_keys)
    if sum(hist) <= 0:
        return None
    best = None
    for is_major, profile in ((True, _KS_MAJOR_PROFILE), (False, _KS_MINOR_PROFILE)):
        for tonic in range(12):
            rotated = [profile[(pc - tonic) % 12] for pc in range(12)]
            corr = _pearson_correlation(hist, rotated)
            if best is None or corr > best[2]:
                best = (tonic, is_major, corr)
    return best


def _pitch_class_stability_rank(pc, tonic_pc, is_major, chord_pcs=None):
    """Categorical stability rank (see _KEY_STABILITY_RANK_MAX) of pitch
    class `pc` within the estimated key `(tonic_pc, is_major)`: tonic >
    triad tone > other scale tone > chromatic, with a bonus when `pc` is
    also part of a chord currently sounding (`chord_pcs`) -- a chord tone
    ranks above a passing tone of the same category (#103/B7)."""
    rel = (pc - tonic_pc) % 12
    triad = _MAJOR_TRIAD_PITCH_CLASSES if is_major else _MINOR_TRIAD_PITCH_CLASSES
    scale = _MAJOR_SCALE_PITCH_CLASSES if is_major else _MINOR_SCALE_PITCH_CLASSES
    if rel == 0:
        rank = 3.0
    elif rel in triad:
        rank = 2.0
    elif rel in scale:
        rank = 1.0
    else:
        rank = 0.0
    if chord_pcs and pc in chord_pcs:
        rank += 0.5
    return rank


def _chord_pitch_class_windows(chords, tuning, n_strings, is_bass, *, is_keys=False):
    """Precompute each explicit chord event's [start, end) sounding window
    and pitch-class set, sorted by start time -- the "a chord is currently
    sounding" signal `_group_key_stability_bonus` needs to tell a chord
    tone apart from a passing tone for a NON-chord group (a single-note or
    arpeggio/run group whose notes happen to fall under a sustained chord).
    `end` falls back to a nominal 0.05s window when a chord's constituent
    notes carry no `sus` at all, so a zero-duration/undampened chord event
    still covers its own onset instant.

    #179: when `is_keys` is set, pitch is read off the note's real MIDI
    value (`_note_midi_keys`) instead of `_approx_pitch`'s string/fret
    approximation -- keys chords carry absolute pitch, so the window's
    pitch-class set is exact rather than an approximation."""
    pitch_fn = (lambda n: _note_midi_keys(n)) if is_keys else (
        lambda n: _approx_pitch(n, tuning, n_strings, is_bass)
    )
    windows = []
    for c in chords:
        c_notes = c.get("notes", []) or []
        if not c_notes:
            continue
        start = float(c.get("t", 0))
        max_sus = max((float(n.get("sus", 0)) for n in c_notes), default=0.0)
        end = start + max(max_sus, 0.05)
        pcs = {pitch_fn(n) % 12 for n in c_notes}
        windows.append((start, end, pcs))
    windows.sort(key=lambda w: w[0])
    return windows


def _chord_pcs_at_time(chord_windows, t):
    """Pitch classes of whichever precomputed chord window (see
    `_chord_pitch_class_windows`) covers time `t`, or None when no chord is
    sounding there. Chord counts per arrangement are small relative to a
    song's note count, so a linear scan is fine -- no bisect needed."""
    for start, end, pcs in chord_windows:
        if start <= t < end:
            return pcs
        if start > t:
            break
    return None


def _group_key_stability_bonus(g, tonic_pc, is_major, tuning, n_strings, is_bass,
                                chord_windows=None, *, is_keys=False, retention_bonus=None):
    """Retention-score discount (0..`retention_bonus`) for
    `g`'s most tonally-stable note -- mirrors the melody-turning-point
    bonus's shape (a flat subtraction gated by an arrangement-level
    signal), just keyed on harmonic stability instead of melodic contour.

    A `"chord"`-type group's own constituent notes are, by construction,
    each other's "chord currently sounding" context (every note in an
    explicit chord event is a chord tone of that same chord) -- so no
    external lookup is needed there. Any OTHER group (single note, run,
    arpeggio) has no such built-in context: without `chord_windows`, it
    only ever ranks on the plain tonic/triad/scale/chromatic ladder, with
    no chord-tone bonus, since there's nothing to check it against. Pass
    `chord_windows` (see `_chord_pitch_class_windows`) so those groups can
    pick up the chord-tone bonus when they land under a sustained chord —
    this is what actually lets a chord tone rank above a passing tone
    played alongside it, rather than the bonus only ever comparing a
    chord's notes against themselves (caught in PR #125 review).

    #179: when `is_keys` is set, pitch is read off the note's real MIDI
    value (`_note_midi_keys`) instead of `_approx_pitch`'s string/fret
    approximation -- keys notes carry absolute pitch, so the ranking is
    simpler, not unavailable. `retention_bonus` defaults to the fretted
    `_KEY_STABILITY_RETENTION_BONUS` (0.08) when not given; the keys
    path passes its own smaller `_KEYS_KEY_STABILITY_RETENTION_BONUS`
    (0.018), since a keys group's `cost` scale is far narrower than the
    fretted one's and the fretted bonus would overwhelm it."""
    notes = g.get("notes", []) or []
    if not notes:
        return 0.0
    is_chord = g.get("type") == "chord"
    pitch_fn = (lambda n: _note_midi_keys(n)) if is_keys else (
        lambda n: _approx_pitch(n, tuning, n_strings, is_bass)
    )
    if is_chord and len(notes) > 1:
        chord_pcs = {pitch_fn(n) % 12 for n in notes}
    elif chord_windows:
        chord_pcs = _chord_pcs_at_time(chord_windows, float(g.get("time", 0)))
    else:
        chord_pcs = None
    best_rank = max(
        _pitch_class_stability_rank(
            pitch_fn(n) % 12, tonic_pc, is_major,
            chord_pcs=chord_pcs,
        )
        for n in notes
    )
    if retention_bonus is None:
        retention_bonus = _KEY_STABILITY_RETENTION_BONUS
    return (best_rank / _KEY_STABILITY_RANK_MAX) * retention_bonus


def _score_groups(groups, n_strings, beat_times=(), *, tempo=None, tuning=(), is_bass=False):
    tempo = tempo or _TempoParams()
    times_sorted = [float(g["time"]) for g in groups]
    turning_points = _melody_turning_points(groups, tuning, n_strings, tempo, is_bass)
    prev_categories = set()
    for gi, g in enumerate(groups):
        ns = g["notes"]
        if not ns:
            g["cost"] = 0.0
            g["value"] = 0.0
            g["retention_score"] = 0.0
            continue
        avg_fret = sum(n.get("f", 0) for n in ns) / len(ns)
        count_ratio = min(1.0, (len(ns) - 1) / max(n_strings - 1, 1))
        spread_ratio = _string_span_score(ns, n_strings)
        string_shape = _STRING_SPREAD_BLEND * spread_ratio + (1.0 - _STRING_SPREAD_BLEND) * count_ratio
        fretting = min(1.0,
            0.4 * _fret_score(avg_fret)
            + 0.35 * _span_score(ns)
            + 0.25 * string_shape
            + 0.15 * _posture_score(ns)
        )
        group_categories = set().union(*(_technique_categories(n) for n in ns))
        # Deliberately NOT re-clamped to 1.0 here: _tech_score already clamps
        # each note to [0,1] on its own (_tech_score, this file), so a
        # single note stacking techniques (e.g. tap + a round-trip bend)
        # routinely saturates max(_tech_score) at exactly 1.0 -- clamping
        # `technique` again would silently swallow the coordination bonus in
        # exactly the peak-demand regime #72/B4 exists to score (a real bug
        # caught in PR #120 review: a chord mixing a palm mute into an
        # already-saturated tapped-bend scored identically to the tapped-bend
        # alone). `cost` (which this feeds) is already documented as
        # deliberately unclamped below; only `retention_score`, the value
        # that actually drives tiering, gets re-clamped to [0,1] there.
        technique = (
            max(_tech_score(n) for n in ns)
            + _technique_coordination_bonus(group_categories, prev_categories)
        )
        if group_categories:
            prev_categories = group_categories
        raw_density = _sequential_density(times_sorted, gi, tempo)
        syncopation = _syncopation_score(gi, times_sorted, beat_times, tempo)
        density = min(1.0, (1.0 - _SYNCOPATION_DENSITY_WEIGHT) * raw_density
                      + _SYNCOPATION_DENSITY_WEIGHT * syncopation)
        max_sus = max(float(n.get("sus", 0)) for n in ns)
        sustain_ease = min(1.0, max_sus / tempo.sustain_ease_norm_seconds)
        base_cost = (
            0.35 * fretting + 0.30 * technique + 0.20 * density + 0.15 * (1.0 - sustain_ease)
        )
        # Cost is purely mechanical. Retention value is tracked separately so
        # later policies can change what is worth preserving without rewriting
        # the intrinsic difficulty model. Graded metrical strength (#103/B2)
        # when a downbeat grid is available, else the legacy on/off check --
        # see _beat_value.
        value = _beat_value(g["time"], beat_times, tempo)
        cost = base_cost
        # Keep the legacy operation order byte-for-byte: base score, beat
        # discount, jump bonuses, final clamp. `retention_score -= 0.12 *
        # value` generalizes the pre-B2 `if value: retention_score -= 0.12`
        # (value was strictly 0.0/1.0 then) to a graded value without
        # changing the binary case's result: 0.12*1.0 == 0.12, 0.12*0.0 == 0.0.
        retention_score = base_cost - 0.12 * value
        if gi in turning_points:
            retention_score -= _MELODY_TURNING_POINT_RETENTION_BONUS
        if gi:
            prev = _group_anchor_note(groups[gi - 1])
            cur = _group_anchor_note(g)
            if prev and cur:
                available = float(g["time"]) - float(groups[gi - 1]["time"])
                fret_jump = abs(int(cur.get("f", 0)) - int(prev.get("f", 0)))
                fret_jump_bonus = _fitts_shift_bonus(fret_jump, available, tempo)
                cost += fret_jump_bonus
                retention_score += fret_jump_bonus
                if available <= tempo.fret_jump_window_seconds:
                    string_jump = abs(int(cur.get("s", 0)) - int(prev.get("s", 0)))
                    string_jump_bonus = min(
                        _STRING_JUMP_MAX_BONUS,
                        max(0, string_jump - _STRING_JUMP_THRESHOLD) * _STRING_JUMP_COEF,
                    )
                    cost += string_jump_bonus
                    retention_score += string_jump_bonus
        # `cost` is deliberately left unclamped (it can exceed 1.0) and does
        # not affect ranking yet — only `retention_score` feeds tiering.
        g["cost"] = cost
        g["value"] = value
        g["retention_score"] = max(0.0, min(1.0, retention_score))


# Retention-curve exponent: how sparse the bottom of a ladder is relative to
# a flat percentile split. Tuned against a wide sample of authored ladders
# (varied genres, both hand-tuned and tool-generated) — real ladders keep
# roughly 10-20% of a phrase's content at the bottom tier and ramp up
# convexly, not the ~1/n_levels flat share a plain percentile split gives.
# Raising rank-fraction to this power before indexing into the sorted score
# list pushes the low-tier cutoffs down without changing the top tier.
_RETENTION_CURVE_EXPONENT = 1.35


# ── Tier assignment ──────────────────────────────────────────────────────────
#
# Every phrase is laid out on ONE arrangement-wide tier scale (0..n_tiers-1),
# so a given mastery-slider position means the same difficulty everywhere in
# the song. The previous scheme ranked groups by percentile *within each
# phrase* and gave each phrase its own depth (from score spread), which made
# the slider phrase-relative twice over: the bottom rung of an easy verse was
# thinned exactly as hard as the bottom rung of the solo, and a passage that
# was hard all the way through (low spread) got the SHORTEST ladder.
#
# A group's tier is the lower of two independent answers:
#   * global — the same retention curve as before, but with its thresholds
#     taken over every group in the arrangement. An easy group enters early
#     no matter which phrase it's in, so an easy phrase is complete at a low
#     tier and simply stops changing above it.
#   * floor — the retention curve applied within the phrase by rank. It
#     guarantees a hard phrase still has a playable skeleton at the bottom
#     tier instead of going silent, and it is what gives a uniformly hard
#     phrase a full-depth ladder.
# Taking the minimum keeps tiers nested (each tier adds groups, never drops
# them) because both inputs are monotone in the tier.


# Upper end of the fixed score scale the global cutoffs are capped by: tier k
# admits nothing scoring above (k+1)/n_tiers * this. Without the cap the
# cutoffs are pure arrangement quantiles, so in a song that is mostly one hard
# passage that passage IS the median and lands in the low tiers almost whole.
# 0.6 is a starting calibration from synthetic passages (open-position quarter
# notes and whole-note chords score < 0.15, an eighth-note low-position riff
# 0.1-0.4, sixteenth-note tapping/legato runs 0.4-0.65), not a validated
# scale; at 4 tiers it puts the cutoffs at 0.15 / 0.30 / 0.45.
_ABSOLUTE_SCORE_SPAN = 0.6


def _tier_thresholds(scores, n_tiers, curve_exponent=_RETENTION_CURVE_EXPONENT):
    """Arrangement-wide score cutoffs for tiers 0..n_tiers-2 from `scores`
    (any order): the retention-curve quantile of the arrangement's scores,
    capped by the fixed scale above. A score strictly above cutoff k is not
    admitted at tier k by this route (the per-phrase floor may still admit
    it — see _assign_tiers)."""
    scores_sorted = sorted(scores)
    total = len(scores_sorted)
    if not total:
        return []
    return [
        min(
            scores_sorted[min(int(((i + 1) / n_tiers) ** curve_exponent * total), total - 1)],
            (i + 1) / n_tiers * _ABSOLUTE_SCORE_SPAN,
        )
        for i in range(n_tiers - 1)
    ]


def _spread_key(index):
    """Van der Corput (base-2 radical inverse) value of `index` — an ordering
    of 0, 1, 2, … that visits positions evenly across the range (0, 0.5,
    0.25, 0.75, …). Used to break retention-score ties in the per-phrase floor so a
    run of equally hard notes is thinned evenly across the phrase rather than
    keeping only its first few notes."""
    result, denom = 0.0, 1.0
    while index:
        denom *= 2.0
        result += (index & 1) / denom
        index >>= 1
    return result


def _assign_tiers(groups, n_tiers, global_thresholds, beat_times=(), *, tempo=None,
                  curve_exponent=_RETENTION_CURVE_EXPONENT):
    """Set g["level"] for one phrase's groups on the arrangement-wide tier
    scale (see the section comment above)."""
    tempo = tempo or _TempoParams()
    if not groups:
        return
    top = n_tiers - 1
    total = len(groups)
    in_time_order = sorted(range(total), key=lambda i: groups[i]["time"])
    position = {gi: pos for pos, gi in enumerate(in_time_order)}
    # Ties in retention score go to the metrically strongest groups first
    # (#103/B2: graded when a downbeat grid is available, else the legacy
    # on/off check -- see _beat_value): a thinned tier keeps its rhythmic
    # landmarks up front (the same bias represented by value applies), then
    # _spread_key spreads the rest. Negated so a stronger beat (higher
    # value) sorts earlier, same direction as the old 0-before-1 ordering.
    ranked = sorted(range(total), key=lambda i: (
        groups[i]["retention_score"],
        -_beat_value(groups[i]["time"], beat_times, tempo),
        _spread_key(position[i]),
    ))
    floor_counts = [
        max(1, math.ceil(((k + 1) / n_tiers) ** curve_exponent * total)) for k in range(top)
    ]
    for rank, gi in enumerate(ranked):
        floor_level = next((k for k, count in enumerate(floor_counts) if rank < count), top)
        global_level = sum(1 for t in global_thresholds if groups[gi]["retention_score"] > t)
        groups[gi]["level"] = min(floor_level, global_level, top)


def _best_bridge_candidate(groups_sorted, group_times, left, right, level, beat_times, original_jump, *,
                            original_string_jump=0, tempo=None):
    tempo = tempo or _TempoParams()
    left_anchor = _group_anchor_note(left)
    right_anchor = _group_anchor_note(right)
    left_fret = int(left_anchor.get("f", 0))
    right_fret = int(right_anchor.get("f", 0))
    left_string = int(left_anchor.get("s", 0))
    right_string = int(right_anchor.get("s", 0))
    lo = bisect.bisect_right(group_times, left["time"])
    hi = bisect.bisect_left(group_times, right["time"])
    candidates = []
    for candidate in groups_sorted[lo:hi]:
        if candidate["level"] <= level:
            continue
        anchor = _group_anchor_note(candidate)
        if not anchor:
            continue
        fret = int(anchor.get("f", 0))
        string = int(anchor.get("s", 0))
        worst_jump = max(abs(fret - left_fret), abs(right_fret - fret))
        worst_string_jump = max(abs(string - left_string), abs(right_string - string))
        if worst_jump < original_jump or worst_string_jump < original_string_jump:
            # Graded when a downbeat grid is available (#103/B2), else the
            # legacy 0/1 penalty -- see _beat_value. 1.0 - value keeps the
            # same direction (a stronger beat is a smaller penalty) and is
            # numerically identical to the old 0/1 in the binary case.
            beat_penalty = 1.0 - _beat_value(candidate["time"], beat_times, tempo)
            candidates.append((
                worst_jump, worst_string_jump, beat_penalty,
                candidate["retention_score"], candidate["time"], candidate,
            ))
    return min(candidates, key=lambda item: item[:5])[5] if candidates else None


# A 4+-string skip between consecutive lower-tier anchors is a hand-shape
# change severe enough to warrant hunting for an intermediate authored
# note to bridge — mirrors the fret-jump continuity check (max_jump=7)
# but on the orthogonal string axis. Deliberately kept as an independent
# OR-trigger alongside the fret-distance check, not fused into one metric:
# a chord-shape change that jumps strings will usually also move frets, so
# the existing fret-distance-based ranking in _best_bridge_candidate
# already does something sensible once bridging is triggered, without
# redefining what the already-tuned max_jump=7 threshold means.
_MAX_STRING_JUMP_FOR_CONTINUITY = 3


def _promote_bridge_candidate(kept, groups_sorted, group_times, beat_times, level, max_jump, *,
                              tempo=None, max_string_jump=_MAX_STRING_JUMP_FOR_CONTINUITY,
                              phrase_boundaries=(), bar_boundaries=()):
    tempo = tempo or _TempoParams()
    phrase_boundaries = sorted(phrase_boundaries)
    bar_boundaries = sorted(bar_boundaries)

    def priority(pair):
        left, right = pair
        t0, t1 = float(left["time"]), float(right["time"])
        if bisect.bisect_right(phrase_boundaries, t0) < bisect.bisect_right(phrase_boundaries, t1):
            return (0, t0)
        if bisect.bisect_right(bar_boundaries, t0) < bisect.bisect_right(bar_boundaries, t1):
            return (1, t0)
        return (2, t0)

    # Chunk joins are harder to recover after thinning than an equally large
    # shift inside a chunk. Prioritize phrase joins, then bar joins, then
    # ordinary within-bar jumps; preserve chronological order within each.
    for left, right in sorted(pairwise(kept), key=priority):
        if float(right["time"]) - float(left["time"]) > tempo.fret_jump_window_seconds:
            continue
        left_anchor = _group_anchor_note(left)
        right_anchor = _group_anchor_note(right)
        if not left_anchor or not right_anchor:
            continue
        left_fret = int(left_anchor.get("f", 0))
        right_fret = int(right_anchor.get("f", 0))
        original_jump = abs(right_fret - left_fret)
        string_jump = abs(int(right_anchor.get("s", 0)) - int(left_anchor.get("s", 0)))
        if original_jump <= max_jump and string_jump <= max_string_jump:
            continue
        candidate = _best_bridge_candidate(
            groups_sorted, group_times, left, right, level, beat_times, original_jump,
            original_string_jump=string_jump, tempo=tempo,
        )
        if candidate:
            candidate["level"] = level
            return True
    return False


def _refine_lower_tier_path(groups, beat_times, max_level, max_jump=7, *,
                             tempo=None,
                             max_string_jump=_MAX_STRING_JUMP_FOR_CONTINUITY,
                             phrase_boundaries=(), bar_boundaries=()):
    """Promote anchors needed for a playable, rhythmically grounded path.

    Promotions only add source groups to lower tiers, preserving nesting and
    providing a conservative fallback when percentile thinning omits every
    beat landmark or creates a large avoidable jump (fret position or
    string distance).
    """
    tempo = tempo or _TempoParams()
    if not groups or max_level <= 0:
        return
    beat_groups = [g for g in groups if _is_beat_aligned(g["time"], beat_times, tolerance=tempo.beat_tolerance)]
    groups_sorted = sorted(groups, key=lambda g: g["time"])
    group_times = [g["time"] for g in groups_sorted]
    for level in range(max_level):
        kept = [g for g in groups_sorted if g["level"] <= level]
        if beat_groups and not any(g in beat_groups for g in kept):
            min(beat_groups, key=lambda g: (g["retention_score"], g["time"]))["level"] = level
            kept = [g for g in groups_sorted if g["level"] <= level]

        while _promote_bridge_candidate(kept, groups_sorted, group_times, beat_times, level, max_jump,
                                        tempo=tempo, max_string_jump=max_string_jump,
                                        phrase_boundaries=phrase_boundaries,
                                        bar_boundaries=bar_boundaries):
            kept = [g for g in groups_sorted if g["level"] <= level]


# Fraction of the way up a phrase's own ladder (0..1) at which a technique
# is allowed to survive. Ordered coarsely from how corpus analysis (a wide
# sample of authored ladders, hand-tuned and tool-generated, across genres)
# showed these actually get introduced: sustain/legato-adjacent techniques
# (bends, vibrato) show up earliest, palm mutes/slides/pop in the middle,
# natural harmonics/fret-hand-mutes/hammer-on/pull-off chains later,
# tremolo/slap/tap/pinch-harmonic reserved for the hardest tier or two.
# Pinch harmonics gate later than natural harmonics (0.95 vs 0.75) since
# the thumb-touch timing they require is markedly less forgiving; on the
# bass side, slap gates later than pop (0.90 vs 0.80) for the same reason
# — slap's percussive thumb strike is the harder half of the "slap and
# pop" pairing. fhm (fret-hand mute) gates alongside natural harmonics —
# often paired with slap/pop for percussive muted "ghost notes," so it
# belongs in the same "moderately advanced, single coordinated hand-
# position" tier rather than the earlier pm/mt picking-hand mutes.
#
# bt/bnv gate the BEND'S SHAPE, layered above bn's own gate (0.50) rather
# than as independent techniques — they only mean anything in the context
# of an active bend, so both gates sit above 0.50 to guarantee a stripped
# bend (bn=0) never leaves a stale bt/bnv behind describing a bend that no
# longer exists. bt gates first (0.65: downgrades a hard bend variant —
# pre-bend, pre-bend-release, round-trip — to a plain bend-up before the
# bend itself is old enough to survive at full shape), bnv later (0.80:
# an explicitly authored curve is the most precise bend representation, so
# it's the last bend refinement to appear).
_TECH_GATE_FRAC = {
    "bn": 0.50,
    "pm": 0.55, "mt": 0.55,
    "bt": 0.65,
    "vb": 0.70,
    "hm": 0.75, "fhm": 0.75,
    "ho": 0.80, "po": 0.80,
    "plk": 0.80, "bnv": 0.80,
    "sl": 0.85, "slu": 0.85,
    "slp": 0.90,
    "tr": 0.90,
    "hp": 0.95,
    "tp": 0.95,
}


# Pitch-preserving simplification: removing a technique must not change the
# pitch the note is STRUCK at, which is both what the player hears against the
# recording and what note_detect checks at the onset.
_MAX_FRET = 24  # feedpak-v1 §6.2: "f" 0 = open, 24 = max
# Bend intents whose onset is already at the bent pitch: release (a held bend
# let down), pre-bend, pre-bend-and-release (feedpak-v1 §6.2.1).
_BEND_STRUCK_AT_PEAK = frozenset({1, 2, 3})
# A bend within this many semitones of a whole number can be replaced by a
# fretted note at that pitch; anything else (a quarter-tone "blues curl") has
# no fretted equivalent.
_BEND_SEMITONE_TOLERANCE = 0.25
# Frets where a natural harmonic sounds the same pitch as the fretted note
# (the 2nd/3rd/4th partials' nodes at 12, 19 and 24). At every other node
# (5, 7, 4, 9, …) the harmonic sounds well above the fretted pitch, so
# stripping `hm` would turn it into a different note.
_HARMONIC_PITCH_SAFE_FRETS = frozenset({12, 19, 24})


def _fretted_bend_peak(note):
    """Fret on the same string that sounds this note's bend peak, or None
    when no fretted note can (non-whole-semitone bend, or past fret 24)."""
    bn = float(note.get("bn", 0) or 0)
    semis = round(bn)
    if semis < 1 or abs(bn - semis) > _BEND_SEMITONE_TOLERANCE:
        return None
    target = int(note.get("f", 0)) + semis
    return target if target <= _MAX_FRET else None


def _prune_bend(out, diff_percent):
    """Apply the bn/bt gates to `out` (mutated) without changing the pitch
    the note is struck at.

    - A bend struck unbent (bend-up, round-trip) just loses the bend: its
      onset pitch is the plain fret either way.
    - A bend struck at its peak (release, pre-bend, pre-bend-and-release) is
      replaced by a fretted note at the peak pitch. Previously it became a
      plain fret (bn gate) or a bend-UP (bt gate), both of which strike the
      note a whole step or so flat of the recording.
    - A struck-at-peak bend with no fretted equivalent is left as authored
      (bn, bt AND its bnv curve): keeping a technique on a low tier is better
      than a wrong pitch.

    Returns True only in that last case, so the caller also skips the bnv
    gate for it.
    """
    if not out.get("bn"):
        return False
    bt = out.get("bt", 0)
    strip = diff_percent < _TECH_GATE_FRAC["bn"]
    # Only the genuinely harder intents (pre-bend, pre-bend-release,
    # round-trip) are affected by bt's own gate; release (1) isn't
    # meaningfully harder than a plain bend and stays until bn's gate.
    downgrade = not strip and diff_percent < _TECH_GATE_FRAC["bt"] and bt in (2, 3, 4)
    if not (strip or downgrade):
        return False
    if bt in _BEND_STRUCK_AT_PEAK:
        peak_fret = _fretted_bend_peak(out)
        if peak_fret is None:
            return True
        out["f"] = peak_fret
        out["bn"] = 0
        out["bt"] = 0
        out.pop("bnv", None)
    elif strip:
        # A removed bend must not leave stale bend-shape metadata behind.
        out["bn"] = 0
        out["bt"] = 0
        out.pop("bnv", None)
    else:
        out["bt"] = 0  # round-trip -> plain bend-up: same onset pitch
    return False


def _prune_techniques(note, diff_percent):
    """Strip technique flags a phrase hasn't "earned" yet at the shared
    arrangement-wide tier scale represented by `diff_percent`, so a low tier
    reads as a simplified-but-intentional version of the part rather than
    a random note subset that happens to keep whatever techniques its
    underlying notes had. Removal is pitch-preserving — see _prune_bend
    and _HARMONIC_PITCH_SAFE_FRETS."""
    out = dict(note)
    bend_kept_as_authored = _prune_bend(out, diff_percent)
    for key, gate in _TECH_GATE_FRAC.items():
        if key in ("bn", "bt") or diff_percent >= gate:
            continue
        if key == "bnv" and bend_kept_as_authored:
            continue
        if key in ("sl", "slu"):
            out[key] = -1
        elif key == "hm" and int(out.get("f", 0)) not in _HARMONIC_PITCH_SAFE_FRETS:
            continue
        else:
            out.pop(key, None)
    return out


def _pick_partial_voicing(ranked, n):
    """Pick `n` notes from a chord's notes for a reduced voicing. `ranked`
    is ordered by ascending string index (see _notes_for_level's comment
    on that convention) UNLESS #103/B7's root-parsing found a real
    harmonic root elsewhere in the chord, in which case `ranked[0]` is
    that root note instead — not necessarily the lowest string. Either
    way, `ranked[0]` is always kept, then this greedily adds whichever
    remaining note keeps the voicing's own fret span (_fret_span)
    smallest. An open string (f=0) contributes nothing to the span, so
    it's always a free, no-stretch add.

    Deliberately diverges from the keys path's outer-voice selection
    (_notes_for_level_keys picks by pitch extremes, since a piano hand
    isn't fret-constrained): a fretted "partial" voicing that's still a
    hard stretch defeats the point of simplifying it, so selection here
    optimizes for playability over preserving the chord's full pitch range.
    """
    if n >= len(ranked):
        return list(ranked)
    kept = [ranked[0]]
    remaining = list(ranked[1:])
    while len(kept) < n and remaining:
        def _span_if_added(cand, kept=kept):
            return _fret_span(kept + [cand])
        best = min(remaining, key=_span_if_added)
        kept.append(best)
        remaining.remove(best)
    return kept


# Chords reduce to their root alone on any tier whose slider band ends in the
# bottom quarter. This was `diff_percent < 0.20`, which no tier could satisfy
# at the default 4 tiers (the bottom tier's diff_percent is 0.25), so the
# documented root-only rung silently never happened; `<= 0.25` makes it the
# bottom tier at 4 tiers and the bottom one or two tiers at 5-8.
_CHORD_ROOT_ONLY_MAX_FRAC = 0.25
# A 4+-note chord's middle voice appears only in roughly the top half of a
# phrase's own ladder, keeping early tiers sparse the same way
# _RETENTION_CURVE_EXPONENT and _TECH_GATE_FRAC already bias the bottom of
# a ladder toward simplicity — a mid-tier chord widens root+1 -> root+2
# instead of jumping straight from a partial voicing to the full chord.
_CHORD_MID_VOICING_FRAC = 0.55


def _prune_note_for_level(note, diff_percent):
    """`_prune_techniques`, plus: clear a slide-contingent `ln` (link-next)
    when the slide it announced didn't survive this tier's own gating.

    gp2rs sets `ln` in exactly two cases (see gp2rs.py): alongside a pitched
    slide (to suppress the target note's gem so the slide visually connects
    into it), or alongside `letRing` with no slide at all. `_prune_techniques`
    already gates `sl`/`slu` independently at 0.85 -- without this, a note
    below that gate keeps `ln=True` with `sl`/`slu` both reverted to -1,
    which would suppress the next note's gem for a slide that no longer
    exists in this tier. Only touches slide-linked notes: a letRing-only
    `ln` (no `sl`/`slu` to begin with) has no technique to strip and is left
    for `_clear_orphaned_link_next` to validate against target survival.
    """
    pruned = _prune_techniques(note, diff_percent)
    if pruned.get("ln") and (note.get("sl", -1) >= 0 or note.get("slu", -1) >= 0):
        if pruned.get("sl", -1) < 0 and pruned.get("slu", -1) < 0:
            pruned.pop("ln", None)
    return pruned


def _global_link_next_survivors(groups_all):
    """Object ids of every note in `groups_all` that has a genuine next
    note on the same string SOMEWHERE later in the full arrangement --
    not just within one phrase window.

    A phrase-scoped check alone can't tell "arpeggio truncation actually
    dropped this note's target" apart from "the target simply lives in the
    NEXT phrase" -- the top tier never prunes or truncates notes, so its
    `out_notes` entries are the exact same objects as here (id()-comparable),
    while every reduced-tier note is always a fresh `dict()` copy from
    `_prune_techniques`/`_prune_note_for_level` and therefore never
    collides with these ids. Passing this set into every tier's
    `_clear_orphaned_link_next` call is safe by construction: it can only
    ever exempt an actual top-tier note.
    """
    by_string = {}
    for g in groups_all:
        for n in g.get("notes", []) or []:
            by_string.setdefault(n.get("s"), []).append(n)
    survivors = set()
    for group in by_string.values():
        group.sort(key=lambda n: float(n.get("t", 0)))
        for i in range(len(group) - 1):
            survivors.add(id(group[i]))
    return survivors


def _clear_orphaned_link_next(notes, keep_ids=None):
    """Clear `ln` on any note whose linked target -- the next note on the
    same string -- didn't survive this tier's reduction (arpeggio
    truncation via `keep_n`, or a whole later group excluded because its
    own `level` is above this tier). Must run once the tier's full note
    list is assembled, since the target can sit in a different group than
    the linking note. Mutates `notes` in place.

    `keep_ids` (see _global_link_next_survivors) exempts a note whose real
    target simply lives in the next phrase rather than having been pruned
    away -- only ever matches an untouched top-tier note (see that
    function's docstring for why).
    """
    keep_ids = keep_ids or ()
    by_string = {}
    for n in notes:
        by_string.setdefault(n.get("s"), []).append(n)
    for group in by_string.values():
        group.sort(key=lambda n: float(n.get("t", 0)))
        for i, n in enumerate(group):
            if n.get("ln") and i + 1 >= len(group) and id(n) not in keep_ids:
                n.pop("ln", None)


def _evenly_sample(ns, keep_n):
    """Pick `keep_n` items from time-ordered `ns`, spaced evenly across the
    full sequence and keeping their original order — used to thin a
    melodic "run" group (issue #73) so a lower tier still traces the run's
    shape (its first and last notes, plus evenly spaced interior ones)
    instead of always keeping a fixed prefix, which would bias every tier
    toward the run's opening notes and never reach its later ones."""
    n = len(ns)
    if keep_n >= n:
        return list(ns)
    if keep_n <= 1:
        return [ns[0]]
    step = (n - 1) / (keep_n - 1)
    indices = sorted({round(i * step) for i in range(keep_n)})
    if len(indices) < keep_n:
        remaining = [i for i in range(n) if i not in indices]
        indices = sorted(set(indices) | set(remaining[: keep_n - len(indices)]))
    return [ns[i] for i in indices]


# #103/B10 (opt-in, off by default): for a chord/rhythm-led phrase, the
# generic per-group thinning above still governs voicing/technique
# reduction inside each surviving chord group -- what's genuinely new here
# is dropping a REPEATED occurrence of the same identified chord entirely
# at the bottom tier ("chord landmarks"), which the per-group model never
# does (every group always survives in some reduced form). Only a chord
# whose ChordTemplate the chart positively identifies (a real `id` naming a
# real template with a name) is ever a drop candidate -- "Chordr must
# identify a voicing's parent chord before grading it: don't confuse
# unnamed partials with new harmony" (issue #121). Scope: this lands the
# landmark stage only (the bottom tier's group SELECTION); the issue's
# fuller staged progression (separate strum-onset/voicing/technique
# stages) is left to the existing continuous diff_percent curve
# (_CHORD_ROOT_ONLY_MAX_FRAC/_CHORD_MID_VOICING_FRAC, _TECH_GATE_FRAC),
# which already grades voicing width and technique presence by tier --
# see the PR description for why a separate staged curve isn't needed on
# top of that.
def _resolvable_chord_identity(g, chord_templates):
    """The parent chord's authored `name` for chord GROUP `g`, or None when
    the chord has no template reference, an out-of-range `id`, or the
    template carries no name. An unidentified/unnamed voicing is never a
    candidate for `_staged_chord_drop_ids` to collapse."""
    if g.get("type") != "chord" or not chord_templates:
        return None
    ch = g.get("chord")
    if not ch:
        return None
    try:
        chord_id = int(ch.get("id", -1))
    except (TypeError, ValueError):
        return None
    if not (0 <= chord_id < len(chord_templates)):
        return None
    name = chord_templates[chord_id].get("name")
    return name if isinstance(name, str) and name else None


def _chord_group_max_sustain(g):
    notes = g.get("notes", []) or []
    return max((float(n.get("sus", 0)) for n in notes), default=0.0)


def _staged_chord_drop_ids(phrase_groups, chord_templates):
    """#103/B10: `id()`s of identified-chord groups in `phrase_groups` that
    the opt-in staged-chords bottom tier should drop entirely -- every
    BOTTOM-TIER (`level == 0`) occurrence of a distinct identified chord
    EXCEPT its longest-sustained ("landmark") occurrence among the bottom
    tier's own occurrences, and except the phrase's own final chord group
    (a likely resolution, which "must never be dropped for being short" --
    issue #121's explicit rule, since a resolution is often struck briefly
    right at a phrase's end). Groups with no resolvable identity (see
    `_resolvable_chord_identity`) are never in the returned set.

    Scoped to `level == 0` groups only (not every occurrence in the whole
    phrase) -- caller only ever consumes this set when building the level-0
    tier (see `generate_phrases_for_arrangement`). Picking a landmark from
    every occurrence in the phrase, including ones `_assign_tiers` placed
    on a HIGHER tier, could drop the only level-0 occurrence of an identity
    with nothing to replace it there (that occurrence's own landmark
    wouldn't show until its own, later tier) -- emptying that identity out
    of the bottom tier entirely, the opposite of this stage's purpose
    (caught in PR #127 review, with a 6-in-2592 synthetic-fixture repro).
    Restricting the scan to groups already competing for a level-0 slot
    makes an empty bottom tier for an identity structurally unreachable
    -- PROVIDED the landmark/resolution role only ever goes to a group
    that actually contributes notes: a `notes: []` chord group (reachable
    from a GP import whose chord id is out of range or whose template is
    fully muted, per `lib/song.py`'s importer) scores a `_chord_group_
    max_sustain` of `0.0`, which can win a landmark tie (`>` favors the
    earliest occurrence) or the resolution slot while emitting nothing in
    `_notes_for_level` -- silently holding an identity's "kept" spot while
    contributing nothing, which is indistinguishable from the identity
    being empty (caught in PR #127 review, round 2). Excluded from
    candidacy below; dropping such a group is a no-op regardless, so
    excluding it from candidacy costs nothing."""
    bottom_tier_groups = [g for g in phrase_groups if g.get("level") == 0]
    note_bearing_candidates = [g for g in bottom_tier_groups if g.get("notes")]
    best_by_identity = {}
    for g in note_bearing_candidates:
        identity = _resolvable_chord_identity(g, chord_templates)
        if identity is None:
            continue
        sus = _chord_group_max_sustain(g)
        current = best_by_identity.get(identity)
        if current is None or sus > current[1]:
            best_by_identity[identity] = (g, sus)
    keep_ids = {id(g) for g, _sus in best_by_identity.values()}
    last_chord = None
    for g in reversed(note_bearing_candidates):
        if g.get("type") == "chord":
            last_chord = g
            break
    if last_chord is not None and _resolvable_chord_identity(last_chord, chord_templates) is not None:
        keep_ids.add(id(last_chord))
    return {
        id(g) for g in bottom_tier_groups
        if _resolvable_chord_identity(g, chord_templates) is not None and id(g) not in keep_ids
    }


def _notes_for_level(groups, level, max_level, *, link_next_keep_ids=None,
                      chord_templates=None, tuning=(), n_strings=6, is_bass=False,
                      chord_stage_drop_ids=frozenset()):
    """Return (notes, chords) wire lists at/below `level`.

    Below the top tier, chords are reduced by voicing and flattened to plain
    notes (no chord_id references — avoids stale chord-template indices on
    a level that never goes through chord reconstruction); the max-difficulty
    tier keeps chords intact and byte-identical to the source.

    `link_next_keep_ids` (see _global_link_next_survivors) is threaded
    straight through to _clear_orphaned_link_next.

    `chord_templates`/`tuning`/`n_strings`/`is_bass` (#103/B7, all optional)
    let both the chord and arpeggio reduction branches below try a REAL
    harmonic root — parsed from the matched ChordTemplate's authored `name`
    (see `_parse_chord_root_pitch_class`) — before falling back to the
    lowest-string-index heuristic they always used. Omitting `chord_templates`
    reproduces the old lowest-string-only behavior exactly.

    `chord_stage_drop_ids` (#103/B10, opt-in, default empty -- a no-op) is a
    set of `id()`s of GROUPS to skip entirely at this level, regardless of
    type -- the opt-in "staged chords" bottom-tier landmark stage computes
    this once per phrase (see `_staged_chord_drop_ids`) to drop a repeated,
    already-identified chord occurrence altogether rather than merely
    thinning its voicing. Empty by default, so every existing caller is
    unaffected.
    """
    diff_percent = (level + 1) / (max_level + 1) if max_level >= 0 else 1.0
    out_notes = []
    out_chords = []
    for g in groups:
        if g["level"] > level:
            continue
        if chord_stage_drop_ids and id(g) in chord_stage_drop_ids:
            continue
        if g["type"] == "chord" and g["chord"] is not None:
            if level >= max_level:
                out_chords.append(g["chord"])
                continue
            ch = g["chord"]
            ch_time = float(ch.get("t", 0))
            ch_notes = list(ch.get("notes", []) or [])
            if len(ch_notes) > 1:
                # String-index convention follows feedpak's own wire format
                # (feedpak-v1.md §6.2/§6.6, mirrored in song.py's
                # _TUNING_BASE_MIDI): index 0 = lowest-pitched string, so the
                # LOWEST index among a chord's notes is its bass-most note by
                # position. That is usually the root in standard open/barre
                # shapes, so reductions build on it — a positional heuristic,
                # not a proven root (an inversion's or slash chord's bass is
                # not its root; the chord's authored identity isn't threaded
                # through here, issue #73).
                ranked = sorted(ch_notes, key=lambda n: n.get("s", 0))
                # #103/B7: prefer a REAL harmonic root, parsed from the
                # matched ChordTemplate's authored name, over the lowest-
                # string guess above -- only when the chart actually
                # references a template and that template's name parses to
                # a recognizable root pitch class present among this
                # chord's own notes. Falls back to `ranked` (the lowest-
                # string heuristic) when unparseable/unmatched.
                #
                # Wire key is `id` (song.py's chord_to_wire/chord_from_wire:
                # {"t", "id", "hd", "notes"}) -- `chord_id` is the Chord
                # dataclass's own attribute name and, separately, the wire
                # key for a HandShape's chord reference (hand_shape_to_wire).
                # Reading `ch.get("chord_id")` here would always be None on
                # a real pack; mirror song.py's own `int(d.get("id", 0))`
                # reader rather than a strict isinstance check, so a
                # hand-edited pack's `"id": "0"` or `0.0` still resolves the
                # way core itself would.
                root_pc = None
                if chord_templates:
                    try:
                        chord_id = int(ch.get("id", 0))
                    except (TypeError, ValueError):
                        chord_id = 0
                    if 0 <= chord_id < len(chord_templates):
                        root_pc = _parse_chord_root_pitch_class(
                            chord_templates[chord_id].get("name"),
                        )
                root_note = _find_note_by_pitch_class(
                    ch_notes, root_pc, tuning, n_strings, is_bass,
                )
                if root_note is not None:
                    ranked = [root_note] + sorted(
                        (n for n in ch_notes if n is not root_note),
                        key=lambda n: n.get("s", 0),
                    )
                # Bass-note-only very early, then a partial voicing that
                # grows by one note at a mid-ladder threshold -- the same
                # general shape the keys path's per-tier budget aims for
                # (root/outer alone, a real middle voicing, full voicing),
                # though the two are no longer the same step function
                # (#103/B8 made the keys path a proportional budget; this
                # fretted branch is still a two-threshold step) — a 4+-note
                # chord still gets a real middle rung instead of jumping
                # straight from 2 notes to the full voicing.
                if diff_percent <= _CHORD_ROOT_ONLY_MAX_FRAC:
                    ch_notes = [ranked[0]]
                elif diff_percent < _CHORD_MID_VOICING_FRAC or len(ranked) <= 3:
                    if len(ranked) > 2:
                        ch_notes = _pick_partial_voicing(ranked, 2)
                else:
                    ch_notes = _pick_partial_voicing(ranked, 3)
            for cn in ch_notes:
                merged = _prune_techniques(cn, diff_percent)
                merged["t"] = ch_time
                merged.pop("ln", None)
                out_notes.append(merged)
        elif g["type"] == "arpeggio" and level < max_level:
            ns = g["notes"]
            # #103/B7: same real-root-over-lowest-string preference as the
            # explicit-chord branch above, using the ChordTemplate name
            # _group_notes recorded when this cluster matched one.
            arp_root_pc = _parse_chord_root_pitch_class(g.get("chord_template_name"))
            arp_root = _find_note_by_pitch_class(ns, arp_root_pc, tuning, n_strings, is_bass)
            if level == 0:
                # Highest-string-index, not hand-position: an open string
                # at that index is a valid, easier bottom-tier
                # simplification, so don't skew toward a fretted note here
                # the way the jump-scoring anchor does.
                anchor = arp_root or _group_anchor_note(g, prefer_fretted=False) or ns[0]
                out_notes.append(_prune_note_for_level(anchor, diff_percent))
            else:
                # Always include the bottom tier's root, then the earliest
                # remaining notes, so each tier is a superset of the one
                # below (taking just ns[:keep_n] could drop the root the
                # bottom tier kept).
                keep_n = max(1, (len(ns) * (level + 1)) // max_level)
                root = arp_root or _group_anchor_note(g, prefer_fretted=False) or ns[0]
                kept = [root] + [n for n in ns if n is not root][:keep_n - 1]
                kept.sort(key=lambda n: float(n.get("t", 0)))
                out_notes.extend(_prune_note_for_level(n, diff_percent) for n in kept)
        elif g["type"] == "run" and level < max_level:
            # No arpeggio evidence for this cluster (issue #73) — it's an
            # unsubstantiated melodic sequence (e.g. a fast cross-string
            # scale run), not a proven broken chord, so it must not be
            # collapsed toward one presumed anchor note even at the bottom
            # tier the way a real arpeggio is above. Thin proportionally to
            # the level (same ratio as the arpeggio branch) and sample
            # evenly across the run so the surviving notes still trace its
            # melodic contour instead of always favoring the run's opening
            # notes. NOTE: for a short run (fewer than roughly 2x
            # max_level notes) this ratio still rounds down to a single
            # surviving note at the bottom tier -- "traces the contour"
            # only becomes visible once a run is long enough for keep_n>1;
            # it's still strictly better than the old behavior (which
            # collapsed to one note regardless of length), just not a
            # contour for every run.
            ns = g["notes"]
            keep_n = max(1, (len(ns) * (level + 1)) // max_level)
            kept = _evenly_sample(ns, keep_n)
            out_notes.extend(_prune_note_for_level(n, diff_percent) for n in kept)
        else:
            if level < max_level:
                out_notes.extend(_prune_note_for_level(n, diff_percent) for n in g["notes"])
            else:
                out_notes.extend(g["notes"])
    out_notes.sort(key=lambda n: float(n.get("t", 0)))
    out_chords.sort(key=lambda c: float(c.get("t", 0)))
    _clear_orphaned_link_next(out_notes, keep_ids=link_next_keep_ids)
    return out_notes, out_chords


def _notes_for_anchors(notes, chords):
    """Flatten standalone notes and chord constituents (each stamped with
    its chord's own onset time) into one note-shaped list for
    `_generate_anchors`, which only reads `t`/`f`. Fret anchors need every
    fretted event in a tier, not just the ones that happen to already be
    standalone notes -- without this, a top tier made entirely of intact
    chords (`lvl_chords`; chords aren't flattened into `lvl_notes` at the
    top tier -- see `_notes_for_level`) produced zero anchors, since
    `_generate_anchors` only ever saw the (empty) `lvl_notes` list.
    """
    out = list(notes)
    for c in chords:
        t = c.get("t", 0)
        for cn in c.get("notes", []) or []:
            out.append({"t": t, "f": cn.get("f", 0)})
    return out


def _generate_anchors(notes, beat_times, *, default_width=4, phrase_start=None, phrase_end=None):
    """`notes` is already scoped to one phrase, but `beat_times` is the
    arrangement's full song-global beat grid -- a beat straddling the
    phrase's own start (bt < phrase_start <= note.t < bt_end) would
    otherwise emit an anchor timestamped at `bt`, before the phrase it
    belongs to even starts (issue #69). When phrase bounds are given,
    beats whose window doesn't overlap [phrase_start, phrase_end) are
    skipped entirely, and any anchor time is clamped to phrase_start so it
    can never precede its own phrase interval.
    """
    if not notes:
        return []
    anchors = []
    prev_fret = prev_width = None
    for i, bt in enumerate(beat_times):
        bt_end = beat_times[i + 1] if i + 1 < len(beat_times) else bt + 2.0
        if phrase_end is not None and bt >= phrase_end:
            continue
        if phrase_start is not None and bt_end <= phrase_start:
            continue
        window = [n for n in notes if bt <= float(n.get("t", 0)) < bt_end and n.get("f", 0) >= 1]
        if not window:
            continue
        frets = [n.get("f", 0) for n in window]
        min_fret = max(1, min(frets))
        max_fret = max(frets)
        width = max(default_width, max_fret - min_fret + 3)
        anchor_time = bt if phrase_start is None else max(bt, phrase_start)
        if min_fret != prev_fret or width != prev_width:
            anchors.append({"time": round(anchor_time, 3), "fret": min_fret, "width": width})
            prev_fret, prev_width = min_fret, width
    return anchors


# ── Keys/piano scoring — separate from the fretted path above ───────────────
#
# Keys arrangements have no fretboard: `s`/`f` encode absolute pitch as
# `midi = string*24 + fret` (see CLAUDE.md's note-detect section), so
# fret-complexity scoring is meaningless here. Difficulty instead comes from
# polyphony, hand span, density, and sustain ease — the same shape feedBack's
# own editor plugin CHANGELOG describes for its keys difficulty support
# ("Scoring is pitch-based ... instead of the guitar fret/string heuristics").

def _note_midi_keys(n):
    return int(n.get("s", 0)) * 24 + int(n.get("f", 0))


# #103/B8: the fretted path's beat-value coefficient (0.12) and
# _MELODY_TURNING_POINT_RETENTION_BONUS (0.12) were calibrated against the
# fretted `cost` model's typical range (0.3-0.6 for a mid-difficulty group,
# dominated by 0.35*fretting + 0.30*technique) -- reusing them verbatim on
# keys' `cost` model is wrong: a single-note keys melody can only move
# `cost` through density/speed/sustain (poly and span_score are 0 for a
# single note), giving a typical spread of roughly 0.10 across an entire
# melodic passage (measured in PR #126 review: min 0.125 / max 0.225,
# stdev 0.0168 on a representative fixture). At the fretted coefficients,
# one beat discount alone (0.12) is already larger than that whole spread,
# and beat+turning together (0.24) can flatten a uniformly-costed keys
# passage's bottom tier to hold over half its notes purely from metrical
# position -- a side effect of borrowing the fretted scale, not an
# intentional design choice. Scaled down to roughly the same RELATIVE
# influence the fretted coefficients have on the fretted cost range
# (0.12 / ~0.45 typical ≈ 27%; 0.10 spread * 27% ≈ 0.025) rather than the
# same absolute number.
_KEYS_BEAT_VALUE_COEF = 0.025
_KEYS_MELODY_TURNING_BONUS = 0.025


# A single hand spans roughly an octave (a 9th at a stretch). An interval this
# wide INSIDE one simultaneous onset can't be one hand's chord voicing -- it is
# a left-hand bass/accompaniment note sounding with a right-hand melody/chord.
_KEYS_HAND_SPLIT_SEMITONES = 10
# The other half of the same judgement (#180): how wide ONE hand's own
# material can be. A hand reaches about an octave, a 9th at a stretch (14
# semitones) -- the figure the split comment above names. `_split_keys_hands`
# uses it to recognise a GENUINE seam: a candidate split is credible only when
# each part fits inside this width, because a hand's own reach is always less
# than the gap separating it from the other hand. An interleaved onset, whose
# only wide gaps fall inside one hand's reach, has no such seam.
_KEYS_HAND_SPAN_SEMITONES = 14
# Retention discount for the melody (skyline) voice, in the same units as
# _KEYS_BEAT_VALUE_COEF: sized to beat the cost spread between an equally
# placed accompaniment filler and a melody note, so the bottom tier carries the
# tune rather than whichever notes happen to be mechanically cheapest.
_KEYS_MELODY_LINE_BONUS = 0.08

# #177: hand-position shift on the keys path -- the keys counterpart of the
# fretted path's `_fitts_shift_bonus` (#103/B4 / issue #19), which the keys
# path had no equivalent of, so a passage leaping two octaves every beat cost
# exactly the same as a stepwise one.
# Target width: a hand shifts for free within about a fourth. That is the
# "no cost" width the log2 index is measured from, mirroring the fretted
# path's conservative two-fret target width -- both are the tolerance of a
# held hand position, not a threshold at which movement begins costing.
_KEYS_LEAP_TARGET_WIDTH_SEMITONES = 5.0
# Reference distance: a twelfth (an octave plus a fifth), roughly the widest
# leap a hand plays comfortably in one go. At or beyond this the term
# saturates, so a passage's worst jump cannot dominate its cost.
_KEYS_LEAP_REFERENCE_SEMITONES = 19.0
# Cap: about half the fretted `_SHIFT_MAX_BONUS` (0.10), for the same reason
# `_KEYS_BEAT_VALUE_COEF` is 0.025 rather than the fretted 0.12 -- the keys
# `cost` scale has a far narrower range than the fretted one (a melodic
# single-note keys passage spread ~0.10 across an entire phrase, measured in
# PR #126 review), so 0.10 here would be as large as the whole signal it
# nudges. Still deliberately TWICE `_KEYS_BEAT_VALUE_COEF`: metrical position
# is the weaker of the two signals for a beginner actually playing the note
# (they can see the barline, they cannot see the span their hand must cross),
# so a two-octave jump should outweigh the beat discount rather than be
# outweighed by it. Must stay under the fretted beat coefficient 0.12 -- see
# the term-vs-metrical-weight guard in the tests.
_KEYS_LEAP_MAX_BONUS = 0.05

# #179: keys key-stability weighting. The fretted path's
# `_KEY_STABILITY_RETENTION_BONUS` (0.08) is sized against the fretted
# `cost` model's typical range (0.3-0.6 for a mid-difficulty group, dominated
# by 0.35*fretting + 0.30*technique). A keys group's `cost` lives on a far
# narrower scale: a single-note keys melody can only move `cost` through
# density/speed/sustain (poly and span_score are 0 for a single note),
# giving a typical spread of roughly 0.10 across an entire melodic passage
# (measured in PR #126 review: min 0.125 / max 0.225, stdev 0.0168 on a
# representative fixture). Reusing the fretted 0.08 verbatim would be a
# discount larger than the whole mechanical signal it's meant to nudge, so
# the keys path gets its own coefficient, scaled to the same RELATIVE
# influence the fretted bonus has on the fretted cost range (0.08 / ~0.45
# typical ≈ 18%; 0.10 spread * 18% ≈ 0.018) rather than the same absolute
# number. Deliberately BELOW `_KEYS_BEAT_VALUE_COEF` (0.025): beat position
# is a stronger, better-evidenced retention signal (#103/B2) than a
# heuristic key estimate is, same guard the fretted bonus obeys.
_KEYS_KEY_STABILITY_RETENTION_BONUS = 0.018

# #178: black-key / awkward-fingering cost on the keys path. Keys cost
# ignored which keys are played, so a passage dense in accidentals cost
# exactly what its transposition to C major did -- for a beginner the
# thumb-on-black-key shapes and shorter black-key levers are genuinely
# harder than white-key material at the same density (beginner methods
# teach white-key material first; that this ordering is the best TEACHING
# order is inferred from pedagogical precedent, not tested -- evidence
# 🔴 weak per #103's convention, design judgment rather than a cited
# finding, same tier as #103/B10 and C7).
# The set is the five raised keys (C# D# F# G# A#, pitch classes 1/3/6/8/10);
# the term charges the group's black-key SHARE (black notes / total notes),
# so a single black melody note pays the full cap while a 4-voice chord
# with one black key pays a quarter of it -- awkwardness scales with how
# much of what the hand is holding is black. No separate TRANSITION term:
# movement between onsets is already priced by the #177 leap term, and a
# white->black step at the same pitch distance would otherwise be charged
# twice for one move.
# Cap 0.02: deliberately BELOW `_KEYS_BEAT_VALUE_COEF` (0.025), the same
# guard the #179 key-stability weight obeys -- metrical position is a
# stronger, better-evidenced retention signal (#103/B2) than a pitch-class
# heuristic is -- and well under the fretted metrical 0.12 ceiling the
# #177 leap guard pins against. Roughly 20% of the keys cost spread
# (~0.10), so a fully-black passage scores at most 0.02/group above its
# all-white transposition: a nudge, not a reordering.
# Interaction with #179's key-stability weighting (checked, not assumed):
# the two terms compound in the same direction on chromatic material but
# model different things -- this one is instrument-mechanical (the key
# under the finger), that one is cognitive (tonal expectation). A black
# key that IS stable in the estimated key (Bb in F major) still pays this
# mechanical cost while earning that stability discount; neither term
# disables the other.
_KEYS_BLACK_KEY_PITCH_CLASSES = frozenset({1, 3, 6, 8, 10})
_KEYS_BLACK_KEY_MAX_BONUS = 0.02

# #181: minimum musical floor for the keys bottom tier. The
# proportional floor in _assign_tiers guarantees tier 0 a share of
# the phrase's GROUPS -- ceil(((1/n_tiers)^1.35) * total), about 15%
# of them -- but a keys group is one onset, often a whole chord, so
# on a dense chordal passage that share materializes as only 1-2
# NOTES against 20+ at the top tier: a learner at the easiest
# setting has almost nothing to play (measured on a 2-bar phrase of
# quarter-note 4-voice chords: tier 0 held 2-3 notes while the top
# tier held 32). The floor has two parts, applied per phrase AFTER
# _assign_tiers (the arrangement-wide thresholds are untouched -- see
# the interaction notes in _keys_tier0_floor's docstring):
#   1. a strong-beat skeleton: one group at tier 0 covering every
#      grid position graded at least _STRENGTH_STRONG_BEAT (a
#      downbeat, or the mid-bar strong beat of a 4-beat measure),
#      so the easiest tier always carries the phrase's metrical
#      landmarks;
#   2. a note-density backstop: tier 0 materializes at least the
#      bottom tier's equal share of the phrase's notes (1/n_levels,
#      capped at what a full demotion of the phrase could emit at
#      tier 0 -- a voicing whose outer voices are an octave apart
#      collapses to one note at tier 0 however many voices it has),
#      topping up with the cheapest remaining groups when the
#      skeleton alone -- thin in 3/4 or 6/8, or absent entirely when
#      no graded grid exists -- leaves it sparser.
# Both parts only DEMOTE groups into tier 0, so the top tier's
# group set (every group) is unchanged and every lower tier only
# grows; the anti-collapse guard inside _keys_tier0_floor keeps the
# floor from making ANY tier byte-identical to its neighbour (which
# _collapse_identical_levels would silently merge, costing the
# ladder a tier -- #181's second acceptance criterion).


def _split_keys_hands(ns):
    """Split one simultaneous onset into (lower, upper) hand parts, or
    (ns, []) when it reads as a single hand.

    The seam must be plausible as a two-hand boundary (#180). The long-
    standing rule was simply the widest internal interval once it reached
    `_KEYS_HAND_SPLIT_SEMITONES`, but when several intervals tie -- the
    crossed/interleaved case -- the widest is not unique and the FIRST one
    can fall INSIDE a hand rather than between the hands: `[36, 48, 60, 72]`
    (left hand 36+48+60, right hand 72) has three equal 12-semitone gaps,
    and splitting at the first pairs 60 with the right hand instead of 36+48.
    Candidate seams are therefore ranked by (widest gap, then the most
    balanced split -- the one whose larger part spans least), so ties resolve
    to the seam that leaves each hand most compact.

    A seam is only rejected when NO qualifying gap leaves both parts within
    `_KEYS_HAND_SPAN_SEMITONES`; in that case the widest gap is used anyway,
    the long-standing behaviour, so a genuine two-hand split is never dropped
    for being wide (a hand may span up to a 9th)."""
    if len(ns) < 2:
        return list(ns), []
    ranked = sorted(ns, key=_note_midi_keys)
    gaps = [
        (_note_midi_keys(ranked[i + 1]) - _note_midi_keys(ranked[i]), i)
        for i in range(len(ranked) - 1)
    ]
    if not gaps or max(gap for gap, _ in gaps) < _KEYS_HAND_SPLIT_SEMITONES:
        return ranked, []

    def _span(part):
        return _note_midi_keys(part[-1]) - _note_midi_keys(part[0])

    def _parts(at):
        return ranked[:at + 1], ranked[at + 1:]

    tight = []
    for gap, at in gaps:
        if gap < _KEYS_HAND_SPLIT_SEMITONES:
            continue
        lower, upper = _parts(at)
        widest_part = max(_span(lower), _span(upper))
        if widest_part <= _KEYS_HAND_SPAN_SEMITONES:
            tight.append((gap, widest_part, at))
    if tight:
        _, _, at = min(tight, key=lambda c: (-c[0], c[1], c[2]))
        return _parts(at)
    gap, at = max(gaps)
    return _parts(at)




def _keys_leap_bonus(distance, available_seconds, tempo):
    """Keys counterpart of `_fitts_shift_bonus` (#177): a bounded per-hand
    hand-position shift cost, where a longer interval lowers the same move.

    Same bounded-log + time-pressure shape as the fretted term, re-derived on
    a pitch scale rather than a fret scale, and reusing
    `tempo.fret_jump_window_seconds` as its tempo-relative pressure scale (the
    fretted path's window, not a new tempo constant): "how much time the
    player has to move the hand" is one question with one answer per path.
    Like `_fitts_shift_bonus` this is a model of relative difficulty, not an
    estimate of actual human movement time.

    DIFFERENCE FROM THE FRETTED TERM, AND IT IS A REAL ONE -- but note WHERE
    it lives: `_keys_leap_bonus` itself is smooth in `available_seconds`, like
    `_fitts_shift_bonus`. The cutoff is added entirely CALLER-SIDE, by the
    `available > tempo.fret_jump_window_seconds` branch in `_score_groups_keys`.
    `_fitts_shift_bonus`'s docstring says of its own window that it is "a
    tempo-relative pressure scale, no longer a hard cutoff", and the keys caller
    reintroduces one: it zeroes the term outright just past that window, which
    puts a discontinuity in `cost` at the window edge (measured worst case
    0.0279 over a grid of windows and distances, 1.12x the whole
    `_KEYS_BEAT_VALUE_COEF` of 0.025, out of a 1 ms change in the inter-onset
    gap). That cutoff is a DELIBERATE KEYS-ONLY ADDITION, kept because the
    issue text points at this window and because
    `_melody_turning_points_keys` bounds the same way: past the window this is
    no longer one continuous passage, and a movement charge that ignored the
    gap entirely would let one distant pair of notes dominate a phrase. It is
    kept despite the cliff, not by accident of sharing the constant.
    """
    if distance <= 0:
        return 0.0
    index = math.log2(distance / _KEYS_LEAP_TARGET_WIDTH_SEMITONES + 1.0)
    reference = math.log2(
        _KEYS_LEAP_REFERENCE_SEMITONES / _KEYS_LEAP_TARGET_WIDTH_SEMITONES + 1.0
    )
    time_scale = max(float(tempo.fret_jump_window_seconds), 0.001)
    pressure = time_scale / (time_scale + max(float(available_seconds), 0.0))
    return min(
        _KEYS_LEAP_MAX_BONUS,
        _KEYS_LEAP_MAX_BONUS * index / reference * pressure,
    )


def _keys_black_key_bonus(ns):
    """#178's black-key / awkward-fingering cost for one group's notes, as
    a value in [0, `_KEYS_BLACK_KEY_MAX_BONUS`]: the group's black-key
    SHARE (black notes / total notes) scaled to the cap.

    Share, not count: a single black melody note pays the full cap (every
    key the hand plays is black), while a 4-voice chord with one black key
    pays a quarter of it. Charged on the MIDIS AS WRITTEN -- including the
    full voicing of a melody-flagged group, even though `_score_groups_keys`
    prices poly/span on the melody note alone above: the hand still shapes
    the whole chord, and this term models the fingering, not the voice
    budget. Mechanical like the #177 leap term, so -- exactly like it -- it
    raises `cost` and `retention_score` together and must NOT touch the
    cost/retention separation the beat-value discount established."""
    if not ns:
        return 0.0
    black = sum(
        1 for n in ns
        if _note_midi_keys(n) % 12 in _KEYS_BLACK_KEY_PITCH_CLASSES
    )
    return _KEYS_BLACK_KEY_MAX_BONUS * black / len(ns)


def _group_notes_keys(notes, chords, *, onset_window_ms=30):
    """Group keys notes into atomic units. No fretboard, so grouping is
    purely temporal: explicit chords stay chords, remaining notes sharing an
    onset (within `onset_window_ms`) become a block-chord cluster.

    A cluster spanning both hands (bass + melody sounding together) is split
    into one group per hand -- scoring it as a single huge chord made the
    accompaniment's cheap filler outrank the tune at the bottom tier. The
    topmost group of each onset is flagged `melody` (skyline) when it sits in
    the arrangement's upper register; see `_score_groups_keys`."""
    raw = []
    for ch in chords:
        raw.append((float(ch.get("t", 0)), list(ch.get("notes", []) or []), ch))

    note_list = sorted((dict(n) for n in notes), key=lambda n: float(n.get("t", 0)))
    total = len(note_list)
    i = 0
    while i < total:
        base_t = float(note_list[i].get("t", 0))
        cluster = [note_list[i]]
        j = i + 1
        while j < total and (float(note_list[j].get("t", 0)) - base_t) * 1000 <= onset_window_ms:
            cluster.append(note_list[j])
            j += 1
        raw.append((base_t, cluster, None))
        i = j

    groups = []
    for t, ns, chord in raw:
        parts = [p for p in _split_keys_hands(ns) if p]
        for pi, part in enumerate(parts):
            groups.append({
                "type": "chord" if (chord is not None or len(part) > 1) else "note",
                "notes": part, "chord": chord, "time": t,
                "cost": 0.0, "value": 0.0, "retention_score": 0.0, "level": 0,
                # Only the upper part of a two-hand onset can be the melody;
                # an unsplit onset is judged against the register below.
                "melody": len(parts) > 1 and pi == len(parts) - 1,
                # Provenance: this group is one hand's half of a split onset.
                # An authored chord and a separate single note that merely
                # share a timestamp are NOT split groups.
                "hand_split": len(parts) > 1,
                # Which hand plays this group (#177). A split onset's halves
                # are exactly the (lower, upper) `_split_keys_hands`
                # returned, and an unsplit onset is one hand by definition of
                # the 10-semitone threshold -- that one is assigned by register
                # in the skyline loop below, using the same judgement that
                # picks the melody.
                "hand": None if len(parts) < 2 else ("lower", "upper")[pi],
            })

    # An unsplit onset is melody only if it sits in the upper register of the
    # arrangement -- otherwise a left-hand-only filler would claim the bonus.
    # The register is read off the SKYLINE (the highest note at each onset),
    # not off every group: a split onset's lower half would otherwise pull the
    # median down into the accompaniment range.
    skyline = {}
    for g in groups:
        if g["notes"]:
            top = max(_note_midi_keys(n) for n in g["notes"])
            skyline[g["time"]] = max(top, skyline.get(g["time"], top))
    tops = sorted(skyline.values())
    median_top = tops[len(tops) // 2] if tops else 0
    for g in groups:
        if not g["notes"] or g["hand_split"]:
            continue
        in_upper_register = max(_note_midi_keys(n) for n in g["notes"]) >= median_top
        g["melody"] = in_upper_register
        # Hand identity comes off the SAME register judgement, computed in the
        # same loop, rather than a second pass: deciding which voice owns a
        # register is one judgement, and for an UNSPLIT onset the hand is the
        # one that owns the register it sits in. Reusing `median_top` here (not
        # a separate mean or span test) is what keeps the two assignments
        # consistent by construction -- the leap term in `_score_groups_keys`
        # then reads `hand` as meaning exactly what `melody` meant. It is a
        # register GUESS, not an observed fingering: on a single melodic line
        # that straddles the median the label alternates with the pitch, which
        # is why `_score_groups_keys` falls back to the preceding group when a
        # group has no same-hand predecessor.
        g["hand"] = "upper" if in_upper_register else "lower"

    groups.sort(key=lambda g: g["time"])
    return groups


def _melody_turning_points_keys(groups, tempo):
    """Keys counterpart of `_melody_turning_points` (#103/B8, applying B5 to
    the keys path). Keys notes carry a REAL MIDI pitch (`_note_midi_keys`),
    not an approximation from string/fret + tuning, so this needs none of
    the fretted version's tuning-approximation machinery -- otherwise
    identical: a strict local high/low among single-note groups, gated by
    the same `tempo.fret_jump_window_seconds` "not one continuous passage"
    neighbor-gap bound."""
    # A group created by a hand split is one half of a two-hand onset; its
    # neighbour at dt == 0 is the other hand, so comparing them says nothing
    # about melodic shape. Excluding only split-created groups (not every
    # group that happens to share a timestamp) matches the pre-split
    # behaviour, where such an onset was one multi-note group and never a
    # candidate, while an authored single note over an authored chord still is.
    singles = [
        i for i, g in enumerate(groups)
        if len(g["notes"]) == 1 and not g.get("hand_split")
    ]
    pitches = {i: _note_midi_keys(groups[i]["notes"][0]) for i in singles}
    times = {i: float(groups[i]["time"]) for i in singles}
    max_gap = tempo.fret_jump_window_seconds
    turning = set()
    for k in range(1, len(singles) - 1):
        i, prev_i, next_i = singles[k], singles[k - 1], singles[k + 1]
        if times[i] - times[prev_i] > max_gap or times[next_i] - times[i] > max_gap:
            continue
        p, prev_p, next_p = pitches[i], pitches[prev_i], pitches[next_i]
        if (p > prev_p and p > next_p) or (p < prev_p and p < next_p):
            turning.add(i)
    return turning


def _keys_leap_charges(groups, tempo):
    """#177's per-hand hand-position shift for every group, as a list parallel to
    `groups` (#177).

    Split out of `_score_groups_keys` so the scoring loop there holds only the
    cost model itself: this pass is a self-contained predecessor walk over the
    whole list, and running it as its own linear pass is what lets the scoring
    loop stay single-pass too. Each group is anchored at the MEAN of its MIDI
    pitches (`_note_midi_keys`, so a single-note group anchors at that note):
    the fretted path's anchor is a fret-position PROXY, whereas a keys note
    carries exact pitch, and a hand's position is best read as the centre of
    the pitches it is holding rather than its top or bottom note -- for a
    multi-note voicing the extremes belong to whichever voice reaches furthest,
    not to the hand as a whole.

    A group's predecessor is the previous group of the SAME hand in list order
    (groups arrive time-sorted), never simply the previous group: the two
    hand-parts of a split onset are different hands playing at the same
    instant, so neither is the other's "previous position" -- scoring them
    against each other would charge the pair a phantom leap on both halves of
    every single split onset. Keying the lookup by `hand` (rather than by
    parity, or by "the group before this one") also makes the result
    independent of the order those parts were emitted in, the same
    emission-order independence `speed` in `_score_groups_keys` relies on.
    The key carries the hand split's provenance as well, since `hand` names
    two different things for the two sources: the lower/upper HALF of a split
    onset and the lower/upper REGISTER of an unsplit one.

    The `hand` tag is a REGISTER judgement, not an observed fingering: for an
    unsplit onset it is exactly the `melody` test (see `_group_notes_keys`). On
    a SINGLE melodic line whose contour straddles the skyline median that label
    alternates lower/upper with every pitch, so no group ever has a same-hand
    predecessor and the whole term would silently evaluate to zero -- 41
    semitones of dive free, which is the exact failure #177 exists to remove.
    So a group with no usable same-hand predecessor falls back to the nearest
    earlier group of the same kind: strictly conservative (a real hand must get
    from the last thing it played to this one), and monotone in real travel.

    The fallback is gated OFF for a group CREATED by a hand split
    (`hand_split`): such a group's `hand` comes from `_split_keys_hands`, so
    having no same-hand predecessor genuinely means "this hand has not played
    yet", and the group before it is the OTHER half of its own onset -- at the
    same timestamp, a zero gap, and a full hand span away. The upper part of
    the FIRST two-hand onset would otherwise be charged that entire span for a
    move it never makes. For the same reason the fallback never INHERITS from a
    split-created group either.

    Groups with no notes are skipped as predecessors too: a hand's position is
    read off notes it is playing, so an empty group is not a position. A group
    built outside `_group_notes_keys` (a unit-test fixture) has no `hand` at
    all; `g.get("hand")` reads that as one hand, which is the conservative
    reading and matches an unsplit single-hand passage.
    """
    leap_by_index = []
    anchors = []
    prev_by_hand = {}
    for gi, g in enumerate(groups):
        ns = g["notes"]
        anchor = (sum(_note_midi_keys(n) for n in ns) / len(ns)) if ns else None
        is_split = bool(g.get("hand_split"))
        key = (g.get("hand"), is_split)
        prev_i = prev_by_hand.get(key)
        # A same-hand predecessor sitting OUTSIDE the movement window is not a
        # usable predecessor -- past the window this is no longer one continuous
        # passage, so the hand had time to reposition. It used to be kept anyway
        # (`prev_by_hand` is refreshed whatever the charge comes out as), which
        # let a stale distant group win every lookup for its hand forever: a line
        # could pay for a two-semitone step and waive a 43-semitone dive, because
        # the dive's nearest same-hand note was a bar back while the step right
        # before it was well inside the window. Dropping it here hands the
        # decision to the fallback below, so the nearest group actually inside
        # the window gets the charge.
        if prev_i is not None and not (
            0.0
            < float(g["time"]) - float(groups[prev_i]["time"])
            <= tempo.fret_jump_window_seconds
        ):
            prev_i = None
        if prev_i is None and anchor is not None and not is_split:
            # Fallback predecessor: the nearest earlier group that actually
            # holds notes AND was not itself created by a hand split. Both skips
            # are needed: a notes-less group holds no position to inherit (same
            # reason it is not recorded as a predecessor above), and a
            # split-created group belongs to the OTHER namespace of `hand`, so
            # inheriting from one is the `upper`/`upper` collision that pairing
            # the chain key on `(hand, hand_split)` exists to prevent. Ordinary
            # input reaches this: an authored left-hand chord that splits, then a
            # right-hand melody note 0.5 s later, was charged a full-hand-span
            # leap off the chord's own upper half even though that hand never
            # moved.
            j = gi - 1
            while j >= 0 and (anchors[j] is None or groups[j].get("hand_split")):
                j -= 1
            if j >= 0:
                cand_available = float(g["time"]) - float(groups[j]["time"])
                if 0.0 < cand_available <= tempo.fret_jump_window_seconds:
                    prev_i = j
                # A candidate past the window is not chained to either; prev_i
                # stays None and the group is charged nothing.
        charged = False
        if anchor is not None and prev_i is not None and anchors[prev_i] is not None:
            available = float(g["time"]) - float(groups[prev_i]["time"])
            if 0.0 < available <= tempo.fret_jump_window_seconds:
                leap_by_index.append(
                    _keys_leap_bonus(abs(anchor - anchors[prev_i]), available, tempo)
                )
                charged = True
        if not charged:
            leap_by_index.append(0.0)
        anchors.append(anchor)
        if anchor is not None:
            prev_by_hand[key] = gi
    return leap_by_index


def _score_groups_keys(groups, beat_times=(), *, tempo=None):
    tempo = tempo or _TempoParams()
    times_sorted = [float(g["time"]) for g in groups]
    onset_times = sorted(set(times_sorted))
    onset_index = {t: i for i, t in enumerate(onset_times)}
    # #103/B8: apply B2 (graded beat strength, via _beat_value) and B5
    # (melody-turning-point retention) to the keys path -- previously only
    # the fretted path (_score_groups) had either term, so a keys chart's
    # ladder ignored metrical position and melodic shape entirely.
    turning_points = _melody_turning_points_keys(groups, tempo)
    # #177: the per-hand hand-position shift, precomputed in one linear pass so
    # the scoring loop below never rescans for it.
    leap_by_index = _keys_leap_charges(groups, tempo)
    for gi, g in enumerate(groups):
        ns = g["notes"]
        if not ns:
            g["cost"] = 0.0
            g["value"] = 0.0
            g["retention_score"] = 0.0
            continue
        midis = [_note_midi_keys(n) for n in ns]
        if g.get("melody") and len(midis) > 1:
            # The bottom tier plays a melody voicing as its outer voices, so
            # what decides where the group first appears is the cost of the
            # tune itself, not of the full right-hand chord. Scoring the whole
            # voicing let a 3-note right hand (poly + span) outrank the
            # left-hand filler by far more than the melody bonus, and tier 0
            # then held no melody at all.
            midis = [max(midis)]

        poly = min(1.0, (len(midis) - 1) / 4.0)  # 1 note=0, 5+ at once=1
        span = (max(midis) - min(midis)) if len(midis) > 1 else 0
        span_score = min(1.0, span / 12.0)  # an octave reach = 1.0

        # Distinct onsets in a tempo-relative time window, not each nearby
        # group's own note count -- same reasoning as the fretted path's
        # _sequential_density (#71): a wide block chord shouldn't inflate
        # density on its own, since polyphony is already `poly` above. The
        # two hand-parts of a split onset are ONE onset, so density is read
        # off the distinct onset times.
        oi = onset_index[times_sorted[gi]]
        density = _sequential_density(onset_times, oi, tempo)

        # Speed is the interval to the NEXT DISTINCT onset, so it doesn't
        # depend on the order the hand-parts of a split onset were emitted in
        # (the first would otherwise see dt == 0 and score zero speed).
        speed = 0.0
        if oi + 1 < len(onset_times):
            dt = onset_times[oi + 1] - float(g["time"])
            if dt > 0:
                speed = min(1.0, max(0.0, (0.25 - dt) / 0.25))

        max_sus = max(float(n.get("sus", 0)) for n in ns)
        sustain_ease = min(1.0, max_sus / 2.0)

        cost = (
            0.30 * poly + 0.25 * span_score + 0.20 * density
            + 0.15 * speed + 0.10 * (1.0 - sustain_ease)
        )
        # Same operation order as the fretted path's _score_groups: base
        # cost, then the beat-value discount and melody-turning bonus, then
        # the jump bonuses, then the final clamp. `value` feeds
        # `_assign_tiers`'s tie-break exactly as it does on the fretted path.
        value = _beat_value(g["time"], beat_times, tempo)
        retention_score = cost - _KEYS_BEAT_VALUE_COEF * value
        if gi in turning_points:
            retention_score -= _KEYS_MELODY_TURNING_BONUS
        if g.get("melody") and retention_score > 0.0:
            # Capped at half of what is left so a cheap, slow melody note
            # (cost ~0.05) keeps a distinct score instead of clamping to 0.0
            # alongside every other one; r - min(b, r/2) is strictly
            # increasing in r, so ordering among melody groups is preserved.
            retention_score -= min(_KEYS_MELODY_LINE_BONUS, retention_score / 2.0)
        # #177: the leap term is mechanical, so -- exactly like the fretted
        # path's fret_jump_bonus -- it raises `cost` and `retention_score`
        # together. It must NOT touch the cost/retention separation the
        # beat-value discount established: `cost` stays intrinsic mechanical
        # difficulty, and the only thing `retention_score` does on top of it is
        # subtract the metrical discount. #178's black-key term is mechanical
        # in the same sense and joins it here, for the same reason: what the
        # hand holds (this term) and where it moves (the leap) are both
        # intrinsic to playing the notes, not reasons to keep or drop them.
        # `cost` is left unclamped here to
        # MIRROR `_score_groups`'s convention, NOT because keys `cost` stays
        # under 1.0 -- it does not. The base formula's weights sum to exactly
        # 1.00 (0.30 + 0.25 + 0.20 + 0.15 + 0.10) and its terms can all
        # saturate at once (5+ notes sounding, a 12-semitone reach,
        # `_sequential_density` at its own 1.0 cap, dt -> 0, max_sus >= 2.0),
        # so the analytic base ceiling is 1.00 and the leap bonus on top of
        # it takes `cost` to 1.05. It stays unclamped to mirror
        # `_score_groups`; the highest value measured over ~60k dense clusters
        # was 0.1750, so `min(1.0, ...)` changes nothing on any input seen so
        # far and adding it would be a behaviour change dressed as tidiness.
        # Only `retention_score` is clamped, as before.
        leap_bonus = leap_by_index[gi]
        black_bonus = _keys_black_key_bonus(ns)
        cost += leap_bonus + black_bonus
        retention_score += leap_bonus + black_bonus
        g["cost"] = cost
        g["value"] = value
        g["retention_score"] = max(0.0, min(1.0, retention_score))


def _collapse_octave_duplicates(ns, preferred=()):
    """Merge notes that are the same pitch class exactly one octave apart
    down to a single note — a doubled root/octave voicing plays the same to
    a beginner as the single note, so it's a free simplification on top of
    the voice-thinning above. Notes in `preferred` win an octave collision;
    this lets a harder reduced tier retain the representative already exposed
    by an easier tier instead of replacing it with its octave partner.
    `preferred` must contain the same note objects that appear in `ns`."""
    preferred_ids = {id(n) for n in preferred}
    ordered = [n for n in ns if id(n) in preferred_ids]
    ordered.extend(n for n in ns if id(n) not in preferred_ids)
    kept = []
    for n in ordered:
        midi_n = _note_midi_keys(n)
        if any(abs(_note_midi_keys(m) - midi_n) == 12 for m in kept):
            continue
        kept.append(n)
    kept_ids = {id(n) for n in kept}
    return [n for n in ns if id(n) in kept_ids]


def _keys_voice_priority(ranked):
    """Fixed voice-add order for a keys chord's notes, `ranked` ascending by
    MIDI pitch: outer voices (bass + melody) first, then inner voices added
    alternately from the outside in. #103/B8 reframes `_notes_for_level_keys`
    as truncating THIS one fixed order to a per-tier count -- since the
    order never changes across tiers, tier k+1's kept set is always a
    superset of tier k's (the #99 nesting property), and "outer voices kept
    at the lowest tier" holds by construction (they're always first)."""
    n = len(ranked)
    if n <= 2:
        return list(ranked)
    order = [ranked[0], ranked[-1]]
    lo, hi = 1, n - 2
    toggle = True
    while lo <= hi:
        if toggle:
            order.append(ranked[lo])
            lo += 1
        else:
            order.append(ranked[hi])
            hi -= 1
        toggle = not toggle
    return order


def _notes_for_level_keys(groups, level, max_level):
    """Thin keys chords by pitch, keeping outer voices first — melody
    (highest pitch) + bass (lowest) at the bottom tier, growing inward,
    mirroring simplified piano sheet-music arrangements. No 'chords' output:
    everything flattens to individual notes, same as the fretted path's
    reduced tiers.

    #103/B8: how many voices a tier keeps is now a per-tier BUDGET that
    rises with level -- `_keys_voice_priority`'s fixed add-order truncated
    to `keep_n(level)` notes -- rather than the previous fixed three-step
    (outer / outer+mid / everything) regardless of how many tiers the
    ladder actually has. This is a note-COUNT budget, not the mechanical-
    cost budget Nakamura & Yoshii (2018) frame piano reduction around
    (`g["cost"]`'s poly/span/density/speed/sustain terms aren't attributed
    per note, only per group) -- a real simplification of the cited
    approach, declared here rather than left implicit. Still gives every
    tier a genuinely graded voicing instead of jumping straight from 2
    notes to 3 to "everything" regardless of ladder depth.
    """
    out_notes = []
    for g in groups:
        if g["level"] > level:
            continue
        ns = list(g["notes"])
        g_time = float(g.get("time", 0))
        is_explicit_chord = g.get("chord") is not None
        if len(ns) > 1 and level < max_level:
            ranked = sorted(ns, key=_note_midi_keys)
            priority = _keys_voice_priority(ranked)
            outer = _collapse_octave_duplicates(priority[:2])
            if level == 0:
                keep_n = len(outer)
            elif len(priority) <= 3:
                # A 2-3 note chord has no real room for a graded budget
                # between "outer only" and "everything" -- matches the
                # pre-#103/B8 behavior exactly for this size.
                keep_n = len(priority)
            else:
                # Proportional budget, same shape as the fretted path's
                # arpeggio-reduction ratio (_notes_for_level): always at
                # least the outer voices, growing toward the full voicing
                # as level approaches max_level. Capped at len(priority) - 1
                # (PR #126 review): at level == max_level - 1 the raw
                # formula already evaluates to the complete voicing, which
                # belongs to the top tier (level >= max_level, handled
                # above) alone -- without the cap, a levels=3 request
                # reaches "everything" one tier early and the tier below
                # the top can come out byte-identical to it for voicings
                # the octave-collapse doesn't rescue.
                keep_n = max(
                    len(outer),
                    min(len(priority) - 1, round(len(priority) * (level + 1) / max_level)),
                )
            # #99/#103/B8 (PR #126 review, round 2): collapse octave
            # duplicates in VOICE-ADD order (priority[:keep_n]), not
            # pitch-sorted order -- `_collapse_octave_duplicates` is a
            # greedy left-to-right pass that keeps whichever note it sees
            # FIRST in a collision, so sorting by pitch before collapsing
            # makes collision priority pitch order instead of add order.
            # When a newly-added interior voice at a HIGHER tier is an
            # octave below one an easier tier already exposed, the sorted
            # collapse can keep the new low voice and drop the exposed
            # high one -- neither a superset nor a subset of the easier
            # tier, breaking the #99 nesting guarantee this whole function
            # exists to provide (measured: 11/2310 adjacent-tier pairs
            # violated nesting on a random voicing sweep; reproduced
            # end-to-end through generate_phrases_for_arrangement).
            # `priority[:keep_n]` is a prefix of one FIXED order, so
            # collapsing over it directly is prefix-stable by construction:
            # a newly added voice is either kept (a real superset) or
            # dropped as an octave duplicate of a voice already exposed at
            # every earlier tier -- never the other way around. Sort for
            # presentation only, after the collapse decides survivors.
            seen = set()
            deduped = []
            for n in priority[:keep_n]:
                if id(n) not in seen:
                    seen.add(id(n))
                    deduped.append(n)
            ns = deduped
            if len(ns) > 1:
                ns = _collapse_octave_duplicates(ns, preferred=outer)
            ns = sorted(ns, key=_note_midi_keys)
        for n in ns:
            merged = dict(n)
            if is_explicit_chord or merged.get("t") is None:
                merged["t"] = g_time
            out_notes.append(merged)
    out_notes.sort(key=lambda n: float(n.get("t", 0)))
    return out_notes, []  # keys never emits chord-shaped entries at reduced tiers


# ── Phrase windowing ─────────────────────────────────────────────────────────

# 8 measures ≈ two 4-bar antecedent/consequent phrases, a common phrase length
# in contemporary popular/rock music — a much closer approximation of a real
# phrase's grain than a flat 30-second chunk, without fragmenting into
# awkwardly short windows at slower tempos the way a 4-bar default would.
_FALLBACK_PHRASE_MEASURES = 8
# Enough consecutive downbeats to trust the grid is real, without requiring a
# full phrase's worth of data before this fallback is allowed to kick in.
_MIN_DOWNBEATS_FOR_MEASURE_FALLBACK = 4


def _measure_aligned_windows(beats, duration, *,
                              measures_per_phrase=_FALLBACK_PHRASE_MEASURES,
                              min_downbeats=_MIN_DOWNBEATS_FOR_MEASURE_FALLBACK):
    """Group the arrangement's own downbeats into phrase-length windows when
    neither section_times nor arr['sections'] gave real boundaries — a
    musically-aligned replacement for the blind 30-second chunking below.

    Counts downbeat OCCURRENCES in time order rather than trusting
    beats[].measure's numeric label to be globally monotonic (nothing in
    the feedpak spec guarantees a chart never restarts measure numbering
    at a structural repeat) — this only ever needs "every Nth downbeat" as
    a position, never the label's value.

    Returns None (caller falls back to the legacy 30s chunker) when there
    aren't enough usable downbeats: `beats` is empty, sparse, or every
    entry is `measure: -1` (not a downbeat, per feedpak-spec §6.8) — every
    other value, including 0, is a real downbeat (see the filter below).
    """
    downbeat_times = sorted(
        float(b.get("time", 0)) for b in beats
        # feedpak-spec's song_timeline.json prose describes 1-based downbeat
        # numbering ("`-1` = sub-beat"), but feedBack's own runtime treats
        # ANY non-negative measure as a downbeat — static/highway.js's
        # `isMeasure = beat.measure >= 0`, static/js/count-in.js, and
        # plugins/highway_3d/screen.js all key off `measure >= 0`, and a
        # song whose numbering happens to start at 0 must not have its
        # first downbeat silently dropped here. `>= 0` and `> 0` behave
        # identically on spec-conformant data (0 should never legitimately
        # appear in this field), so matching core's actual, ecosystem-wide
        # convention costs nothing and is the defensive choice.
        if isinstance(b, dict) and int(b.get("measure", -1)) >= 0
    )
    if len(downbeat_times) < min_downbeats:
        return None
    windows = []
    for i in range(0, len(downbeat_times), measures_per_phrase):
        t0 = downbeat_times[i]
        j = i + measures_per_phrase
        t1 = downbeat_times[j] if j < len(downbeat_times) else duration
        if t1 > t0:
            windows.append((t0, t1))
    if windows and windows[0][0] > 0.0:
        # A pickup/intro before the first downbeat belongs in the first
        # window, not silently dropped.
        windows[0] = (0.0, windows[0][1])
    return windows or None


def _valid_section_times(raw_times):
    """Coerce a raw list of section-boundary times into a finite,
    nonnegative, strictly increasing sequence with duplicates removed
    (issue #69). Both the canonical song-level `section_times` and an
    arrangement's own `sections` field are user/importer-supplied and have
    shown missing-as-zero, negative, non-finite (`inf`/`nan`), and
    duplicate values in the wild — any of those can turn a phrase window
    into a zero-length or reversed interval, or (for `nan`) silently break
    sort order entirely, since every comparison against `nan` is False.

    Non-finite/non-numeric entries are dropped outright (there's no sane
    boundary to recover from `nan`); negative times are clamped to 0.0 (a
    corrupt offset, not a meaningful "before the song" position); and
    duplicate times collapse to a single boundary so no window degenerates
    to zero length purely from the input carrying the same time twice.
    """
    cleaned = []
    for t in raw_times or []:
        try:
            f = float(t)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(f):
            continue
        cleaned.append(max(0.0, f))
    seen = set()
    out = []
    for f in sorted(cleaned):
        if f in seen:
            continue
        seen.add(f)
        out.append(f)
    return out


# Wire fields whose documented default (feedpak-v1.md §6.2.1) `_prune_techniques`
# writes EXPLICITLY when gating a technique out, rather than popping the key --
# `sl`/`slu` are always set to -1, and a fully-stripped bend always sets both
# `bn` and `bt` to 0 (see _prune_techniques). A raw, never-pruned top-tier note
# simply never carries these keys at all. Without normalizing this away, a
# pruned tier and an untouched tier describing the exact same playable notes
# compare as different dicts purely because one has `sl: -1` and the other
# doesn't -- which would make _collapse_identical_levels miss a real duplicate.
_NOTE_FIELD_DEFAULTS = {"sl": -1, "slu": -1, "bn": 0, "bt": 0}


def _canonical_note_for_compare(note):
    """`note` with any field sitting at its wire-format default dropped, so
    tier-duplicate comparison sees past _prune_techniques's explicit
    sentinel writes (see _NOTE_FIELD_DEFAULTS) to the actual playable
    content. A `False` boolean flag is also dropped: every boolean note
    field defaults to false (feedpak-v1 §6.2), and gating a technique pops
    its key, so a source note carrying `"ho": false` would otherwise differ
    from its own pruned copy and keep a duplicate tier alive."""
    return {
        k: v for k, v in note.items()
        if v is not False and (k not in _NOTE_FIELD_DEFAULTS or v != _NOTE_FIELD_DEFAULTS[k])
    }


def _phrase_mechanical_cost(phrase_groups):
    """Mean of `g["cost"]` (the purely mechanical difficulty from
    _score_groups/_score_groups_keys — see #72/B1) across every note group
    in a phrase, regardless of which tier(s) a group survives to.

    This is deliberately independent of `max_difficulty`/`levels`: those
    report how DEEP the ladder is (how many distinct tiers this phrase's
    content splits into), not how HARD the phrase's full content is to
    play. A short phrase with one very hard chord and a long, easy phrase
    can both collapse to a single level (max_difficulty 0) while having
    very different mechanical cost — this field is what lets a consumer
    tell those two apart. Unclamped, like `cost` itself (see _score_groups):
    a phrase dominated by several stacked jump/posture penalties can exceed
    1.0.
    """
    if not phrase_groups:
        return 0.0
    return sum(g["cost"] for g in phrase_groups) / len(phrase_groups)


def _collapse_identical_levels(levels_out):
    """Merge adjacent tiers whose generated content is identical (issue #70),
    keeping each surviving level's `difficulty` as the tier where its content
    FIRST appears — so the numbers can be sparse (e.g. 0, 1, 3).

    Every phrase is generated on the same arrangement-wide tier scale (see
    _assign_tiers), so an easy phrase is typically complete a few tiers
    below the top and every tier above that repeats it. Storing those
    repeats is pure noise, but renumbering the survivors 0..k (as this used
    to) would throw away the tier scale itself: the player needs to know
    that a phrase's second level starts at tier 3, not at tier 1. A reader
    maps the mastery slider onto `max_difficulty + 1` tiers and plays the
    last level whose `difficulty` is <= the current tier. (A reader that
    instead indexes levels by position still gets a valid, just
    phrase-relative, ladder.)

    Anchors/handshapes are derived purely from notes/chords, so comparing
    those two fields (through _canonical_note_for_compare, to see past
    prune-artifact sentinel keys) is sufficient to detect a true duplicate.

    Within a run of duplicates, the LATER (higher-difficulty) tier's dict is
    kept as the representative content rather than the first: pruning only
    ever adds sentinel keys (never removes real content — see
    _NOTE_FIELD_DEFAULTS), so the later tier in an equal-content run is
    always the same-or-cleaner version, up to and including the untouched
    top tier itself.
    """
    if not levels_out:
        return levels_out
    collapsed = [levels_out[0]]
    for lvl in levels_out[1:]:
        prev = collapsed[-1]
        same_notes = [_canonical_note_for_compare(n) for n in lvl["notes"]] == [
            _canonical_note_for_compare(n) for n in prev["notes"]
        ]
        if same_notes and lvl["chords"] == prev["chords"]:
            lvl["difficulty"] = prev["difficulty"]
            collapsed[-1] = lvl
            continue
        collapsed.append(lvl)
    return collapsed


def _keys_identical_tier_pairs(phrase_groups, top_tier):
    """Indices k such that this phrase's tiers k and k+1 materialize
    byte-identical note lists -- exactly the comparison
    `_collapse_identical_levels` makes (through
    `_canonical_note_for_compare`, so prune-artifact sentinel keys
    cannot fake a difference). A demotion into tier 0 grows every
    tier set `{g["level"] <= k}` it crosses, so ANY adjacent
    boundary can become a duplicate, not just the bottom one:
    demoting every group that sat at level k+1 equalizes the two
    tiers' group sets, and when their per-tier voice budgets then
    agree (always for two-voice groups, and for wider voicings
    whose extra voices are all octave duplicates of exposed outer
    voices) the materialized notes collide and
    `_collapse_identical_levels` would silently merge the pair,
    costing the ladder a tier."""
    materialized = [
        [_canonical_note_for_compare(n)
         for n in _notes_for_level_keys(phrase_groups, k, top_tier)[0]]
        for k in range(top_tier + 1)
    ]
    return frozenset(
        k for k in range(top_tier) if materialized[k] == materialized[k + 1]
    )


def _keys_tier0_note_count(group, top_tier):
    """How many notes `group` emits at tier 0, whatever level
    it currently holds. A group's tier-0 materialization
    depends only on its own notes -- `_notes_for_level_keys`
    reduces each group independently -- so a phrase's tier-0
    size is the sum of its groups' sizes, computable once per
    group instead of by rematerializing the whole phrase.
    `_notes_for_level_keys` skips a group whose level exceeds
    the requested tier, so the group is briefly held at
    level 0 for the measurement and its real level restored
    immediately after."""
    held = group["level"]
    group["level"] = 0
    try:
        return len(_notes_for_level_keys([group], 0, top_tier)[0])
    finally:
        group["level"] = held


def _keys_phrase_hands(phrase_groups, coverable):
    """#180: the phrase's two register bands as index lists ``[lower, upper]``,
    or None when it reads as one hand.

    The per-onset `hand_split` tag only exists where an onset was actually
    split, so a phrase whose hands play distinct, register-separated lines
    WITHOUT ever sounding together -- staggered, rather than simultaneous --
    has no split at all and would fall outside a `hand_split`-only per-hand
    floor. This bands the phrase's own pitches instead: the widest gap between
    consecutive distinct pitches, when it reaches `_KEYS_HAND_SPLIT_SEMITONES`,
    is the same two-register separation the onset split looks for; each group
    is placed on the side of that boundary its mean pitch falls. It is a
    register heuristic, deliberately conservative: no gap that wide means one
    band (None), and a band that ends up empty is not a second hand."""
    pitches = sorted({
        _note_midi_keys(n)
        for i in coverable for n in phrase_groups[i]["notes"]
    })
    if len(pitches) < 2:
        return None
    gap, at = max(
        (pitches[k + 1] - pitches[k], k) for k in range(len(pitches) - 1)
    )
    if gap < _KEYS_HAND_SPLIT_SEMITONES:
        return None
    boundary = (pitches[at] + pitches[at + 1]) / 2.0
    lower, upper = [], []
    for i in coverable:
        midis = [_note_midi_keys(n) for n in phrase_groups[i]["notes"]]
        (lower if sum(midis) / len(midis) < boundary else upper).append(i)
    if not lower or not upper:
        return None
    return [lower, upper]


def _keys_strong_positions(tempo, t0, t1):
    """The graded grid positions (#103/B2) in [t0, t1) graded at least
    `_STRENGTH_STRONG_BEAT` -- the strong beats the #181 skeleton covers."""
    if not tempo.beat_grid:
        return ()
    return tuple(sorted(
        float(t) for t, strength in tempo.beat_grid
        if strength >= _STRENGTH_STRONG_BEAT and t0 <= t < t1
    ))


def _keys_tier0_skeleton(positions, coverable, phrase_groups, original_levels,
                         beat_times, tempo, chosen):
    """#181 Part 1: one group per strong position -- the phrase group nearest
    it (unbounded, so a syncopated or subdivided onset still covers the beat
    it displaces; a position nobody plays near, a rest, keeps whatever group
    is genuinely closest); ties go to the cheaper group, then the earlier one,
    so the choice is deterministic. Returns (covers, demotions) and adds the
    demoted groups to `chosen`."""
    covers, demotions = [], []
    for pos in positions:
        cover = min(
            coverable,
            key=lambda i: (
                abs(float(phrase_groups[i]["time"]) - pos),
                phrase_groups[i]["retention_score"],
                -_beat_value(phrase_groups[i]["time"], beat_times, tempo),
                i,
            ),
        )
        covers.append(cover)
        if cover not in chosen and original_levels[cover] > 0:
            chosen.add(cover)
            demotions.append(cover)
    return covers, demotions


def _keys_bass_root_indices(phrase_groups, covers, onset_members, original_levels, chosen):
    """#180 Part 1b: the lowest-pitch group of each strong position's onset,
    as indices to demote (added to `chosen`). The skeleton keeps only the
    nearest group, usually the cheaper melody, so the downbeat root it sounds
    over was dropped even while present. Only groups sounding at that onset
    qualify, so a rest or a right-hand-only bar forces nothing into a sparse
    window."""
    out = []
    for cover in covers:
        onset = onset_members[float(phrase_groups[cover]["time"])]
        bass = min(
            onset,
            key=lambda i: (
                min(_note_midi_keys(n) for n in phrase_groups[i]["notes"]),
                i,
            ),
        )
        if bass not in chosen and original_levels[bass] > 0:
            chosen.add(bass)
            out.append(bass)
    return out


def _keys_hand_bands(phrase_groups, coverable):
    """#180 Part 1c's hand model: the `hand_split` groups' sides when the
    phrase has any (the unambiguous hand identity), otherwise the phrase-level
    register bands from `_keys_phrase_hands` so staggered hands are covered
    too. None when the phrase reads as one hand."""
    split_hands = {}
    for i in coverable:
        g = phrase_groups[i]
        if g.get("hand_split") and g.get("hand") in ("lower", "upper"):
            split_hands.setdefault(g["hand"], []).append(i)
    if len(split_hands) >= 2:
        return list(split_hands.values())
    return _keys_phrase_hands(phrase_groups, coverable)


def _keys_tier0_required(phrase_groups, covers, coverable, onset_members,
                         original_levels, chosen):
    """#180's two extra bottom-tier guarantees, as group indices to demote:
    the bass root under each strong position (`_keys_bass_root_indices`), then
    one group per hand (`_keys_hand_bands`). Adds them to `chosen` too, so the
    density backstop below counts them. The per-hand floor stops a dense hand
    from satisfying the skeleton and the note-share target alone and starving
    a sparse hand out of every reduced tier but the top."""
    required = _keys_bass_root_indices(
        phrase_groups, covers, onset_members, original_levels, chosen,
    )
    bands = _keys_hand_bands(phrase_groups, coverable)
    if bands:
        for members in bands:
            if any(int(phrase_groups[k]["level"]) == 0 or k in chosen for k in members):
                continue
            cheapest = min(
                members,
                key=lambda i: (
                    phrase_groups[i]["retention_score"],
                    float(phrase_groups[i]["time"]),
                    i,
                ),
            )
            if cheapest not in chosen and original_levels[cheapest] > 0:
                chosen.add(cheapest)
                required.append(cheapest)
    return required



def _keys_tier0_backstop(phrase_groups, coverable, chosen, original_levels,
                         beat_times, tempo, top_tier, n_levels):
    """#181's note-density backstop, as the group indices to demote.

    The target is the bottom tier's equal share of the phrase's notes
    (1/n_levels), counted the way a reader experiences it -- the
    MATERIALIZED tier-0 note count, after outer-voice reduction and the
    octave collapse -- so the guarantee is on what the learner actually
    plays, not on raw input note counts. The raw share is capped at what a
    full demotion of the phrase could emit at tier 0: a voicing whose outer
    voices are an octave apart collapses to one note at tier 0 however many
    voices it has, so on such a passage the raw share is unreachable even
    with every group at tier 0, and an uncapped target would only send the
    backstop chasing it to exhaustion.

    Each group's tier-0 size is independent of every group's level (see
    `_keys_tier0_note_count`), so the emitted tier-0 count is tracked
    incrementally -- the per-group sizes are computed once and each
    demotion adds its group's size -- instead of rematerializing and
    re-sorting the whole phrase after every demotion, which is quadratic in
    the phrase's group count. The caller applies the returned levels."""
    tier0_sizes = {
        i: _keys_tier0_note_count(phrase_groups[i], top_tier)
        for i in coverable
    }
    emitted = sum(
        tier0_sizes[i] for i in coverable
        if phrase_groups[i]["level"] == 0
    )
    target = min(
        math.ceil(
            sum(len(phrase_groups[i]["notes"]) for i in coverable) / n_levels
        ),
        sum(tier0_sizes.values()),
    )
    remaining = [
        i for i in sorted(
            coverable,
            key=lambda i: (
                phrase_groups[i]["retention_score"],
                -_beat_value(phrase_groups[i]["time"], beat_times, tempo),
                i,
            ),
        )
        if i not in chosen and original_levels[i] > 0
    ]
    demoted = []
    for i in remaining:
        if emitted >= target:
            break
        demoted.append(i)
        emitted += tier0_sizes[i]
    return demoted


def _keys_tier0_anticollapse(phrase_groups, demotions, protected,
                             original_levels, pre_floor_pairs, top_tier):
    """Break every identical adjacent-tier pair the floor newly created.

    A pair already identical BEFORE the floor is a pre-existing collapse
    (issue #70's territory, which `_collapse_identical_levels` exists to
    merge); a pair the floor made identical gets a demoted group of the level
    it lost back, one restoration per threatened boundary. The ladder without
    the collapsed tier wins (#181): the guard breaks every new pair, so the
    floor never costs a tier.

    A NON-required demotion is preferred, so a #180 required group (bass root,
    per-hand) is normally left at tier 0. Only when every group demoted from
    that level is required -- so nothing else can separate the boundary -- is a
    required group restored, at the documented cost of that phrase's bass-root
    or per-hand guarantee: keeping the full ladder is the higher-priority
    contract, and the alternative (leaving the pair identical) makes
    `_collapse_identical_levels` silently merge a tier, which pullfrog measured
    on the #180 fixtures (22 new identical pairs across Alberti/stride/crossed
    with the protection unconditional)."""
    for k in range(top_tier - 1, -1, -1):
        if k in pre_floor_pairs:
            continue
        if k not in _keys_identical_tier_pairs(phrase_groups, top_tier):
            continue
        fallback = None
        for i in reversed(demotions):
            if original_levels[i] != k + 1:
                continue
            if i not in protected:
                fallback = i
                break
            if fallback is None:
                fallback = i
        if fallback is not None:
            phrase_groups[fallback]["level"] = original_levels[fallback]
            demotions.remove(fallback)


def _keys_tier0_floor(phrase_groups, t0, t1, beat_times, tempo, n_levels):
    """#181: enforce the keys bottom-tier density floor on one phrase
    window (see the #181 design comment in the keys constants
    region for the two-part floor definition and the measured
    failure it fixes).

    INTERACTION WITH THE TIER THRESHOLDS AND _collapse_identical_levels
    (#181's second work item): this pass runs AFTER `_assign_tiers`,
    so it never sees or moves the arrangement-wide `global_thresholds`
    -- it only relabels `g["level"]` DOWNWARDS within this phrase.
    `_notes_for_level_keys` materializes tier k from the group set
    `{g["level"] <= k}`, and a demotion moves a group INTO every
    such set it crosses, never out of one, so tiers only grow and
    the top tier (all groups) is untouched. The boundary a demotion
    can cross is any adjacent pair: demoting EVERY group that sat at
    level k+1 equalizes tiers k and k+1, and when every group's
    level-k and level-(k+1) reductions then agree their materialized
    notes become byte-identical -- which `_collapse_identical_levels`
    would silently merge, costing the ladder a tier. The guard
    therefore makes sure the floor never CREATES a new identical
    pair -- a pair already identical BEFORE the floor is a
    pre-existing collapse, #70's own territory, and not the floor's
    to fix or worsen. A pair (k, k+1) can only become newly identical
    when every group that sat at level k+1 was demoted (one group at
    level k+1 alone separates the pair's group sets, since a group
    materializes at least one note at every tier), so the fix is
    surgical: restore the most recently demoted group with original
    level k+1 -- one per threatened boundary, every other demotion
    intact. Boundaries are walked top-down because restoring a group
    with original level L removes it from every tier set below L,
    which can only threaten boundaries below L -- each visited later
    in the walk.

    Strong positions come from `tempo.beat_grid` (the graded #103/B2
    grid), filtered to this window [t0, t1). Without a graded grid
    (no beats[], or none carrying a downbeat) there are no positions
    of known strength to guarantee -- every beat is equally (un)known
    -- so the skeleton is skipped and the note-density backstop is the
    whole floor. A strong position's cover is the phrase group nearest
    to it (unbounded, so a syncopated or subdivided onset still
    covers the beat it displaces; a position nobody plays near, a
    rest, keeps whatever group is genuinely closest); ties -- a pair
    of sixteenths straddling the beat -- go to the cheaper group, the
    one the ladder already prefers at tier 0, then to the earlier
    group, so the choice is deterministic.

    #180 adds two more demotions on top of the #181 skeleton, both
    before the density backstop so they count toward its target:
    a bass root (the lowest-pitch group of each strong position's
    onset) and a per-hand floor (one group per hand). See
    `_keys_tier0_required` for what each guarantees and its bounds,
    and `_keys_tier0_anticollapse` for how the anti-collapse guard
    protects them (preferring to restore a non-required demotion, so
    the ladder keeps its tier count when the two contracts collide).
    """
    if not phrase_groups:
        return
    top_tier = n_levels - 1
    original_levels = [int(g["level"]) for g in phrase_groups]
    # Only groups that hold notes can cover a position or contribute
    # to tier 0's density; an empty group (a unit-test fixture
    # shape) materializes nothing either way.
    coverable = [i for i, g in enumerate(phrase_groups) if g["notes"]]
    if not coverable:
        return
    # The identical-pair set the floor must not extend (see the
    # docstring): pairs already collapsing before any demotion.
    pre_floor_pairs = _keys_identical_tier_pairs(phrase_groups, top_tier)

    # Part 1: the strong-beat skeleton; Parts 1b/1c add the bass root and the
    # per-hand floor. The returned "required" indices are the #180 guarantees
    # the anti-collapse guard will not undo; the backstop (Part 2) then tops
    # tier 0 up to the note-share target.
    chosen = set()
    onset_members = {}
    for i in coverable:
        onset_members.setdefault(float(phrase_groups[i]["time"]), []).append(i)
    covers, demotions = _keys_tier0_skeleton(
        _keys_strong_positions(tempo, t0, t1), coverable, phrase_groups,
        original_levels, beat_times, tempo, chosen,
    )
    required = _keys_tier0_required(
        phrase_groups, covers, coverable, onset_members, original_levels, chosen,
    )
    demotions.extend(required)
    for i in demotions:
        phrase_groups[i]["level"] = 0

    # Part 2, the note-density backstop (#181): see `_keys_tier0_backstop`.
    demotions.extend(_keys_tier0_backstop(
        phrase_groups, coverable, chosen, original_levels, beat_times, tempo,
        top_tier, n_levels,
    ))
    for i in demotions:
        phrase_groups[i]["level"] = 0

    # Anti-collapse guard (see `_keys_tier0_anticollapse`): a pair the floor
    # made identical is broken by restoring a NON-required demotion; a #180
    # required group is never restored.
    _keys_tier0_anticollapse(
        phrase_groups, demotions, set(required), original_levels, pre_floor_pairs, top_tier,
    )


# ── Public entry point ───────────────────────────────────────────────────────
#
# The seam's only public door: everything above is private scoring math, and
# this is the one function callers outside this file reach for. It still takes
# and returns plain wire dicts/lists — no I/O happens here or below.

def generate_phrases_for_arrangement(arr, *, n_levels=4, section_times: list[float] | None = None,
                                      is_bass: bool | None = None, staged_chords: bool = False):
    """Build a phrase-level difficulty ladder for one arrangement's raw wire
    dict (as stored in a sloppak's arrangements/*.json).

    Read-only over `arr` — returns the `phrases` wire list to assign onto
    `arr["phrases"]`; every other key in `arr` is left untouched by the
    caller. Returns None when there isn't enough chart content to bother
    (an ambient/silent arrangement, or one already effectively empty), or
    when the arrangement's instrument isn't one this generator supports
    (currently: fretted guitar/bass and keys/piano — see _instrument_kind).

    Each returned phrase carries `max_difficulty` (how many tiers the
    ladder has — see _assign_tiers) and `difficulty_cost` (how mechanically
    hard the phrase's full, untiered content is — see
    _phrase_mechanical_cost). The two are independent: `max_difficulty`
    answers "how much does this phrase get thinned across the ladder",
    `difficulty_cost` answers "how hard is this phrase to play at all" (#72/B1).

    `is_bass`, when given, overrides the bass/non-bass name-sniff used to
    pick the melody-shape pitch approximation's 5-string interval row
    (#103/B5) -- pass the EFFECTIVE value (after any manifest entry
    override; see _generate_one) when the caller has one. `None` (the
    default) falls back to sniffing `arr`'s own type/name, matching
    lib/sloppak.py's arrangement_is_bass() but over only the embedded
    values -- correct for a caller with no manifest context.

    `staged_chords` (#103/B10, opt-in, default False) enables the chord-
    landmark bottom tier: a REPEATED occurrence of the same identified
    chord is dropped entirely at the bottom tier, keeping only its longest-
    sustained occurrence per phrase (plus the phrase's own final chord
    group, protected as a likely resolution). False reproduces this
    function's exact prior output -- existing callers/behavior are
    unaffected. See `_staged_chord_drop_ids`.
    """
    kind = _instrument_kind(arr.get("type", ""), arr.get("name", ""))
    if kind == "drums":
        return None  # shouldn't normally reach here — see setup()'s pre-check
    if kind == "unsupported":
        return None  # explicit allowlist miss (issue #66) — see _instrument_kind

    notes = arr.get("notes", []) or []
    chords = arr.get("chords", []) or []
    beats = arr.get("beats", []) or []
    sections = arr.get("sections", []) or []
    tuning = arr.get("tuning", [0] * 6) or [0] * 6
    n_strings = max(1, len(tuning))
    is_keys = (kind == "keys")
    # Authored evidence for implicit-arpeggio classification (issue #73) —
    # see _classify_cluster. Both are additive/optional wire keys; absent
    # on GP imports and pre-#73 sloppaks, which just means clustering falls
    # back to the sustain-overlap check alone.
    hand_shapes = arr.get("handshapes", []) or []
    chord_templates = arr.get("templates", []) or []

    total_events = len(notes) + sum(len(c.get("notes", []) or []) for c in chords)
    if total_events < MIN_EVENTS_FOR_GENERATION:
        return None

    duration = 0.0
    for n in notes:
        duration = max(duration, float(n.get("t", 0)) + float(n.get("sus", 0)))
    for c in chords:
        # A flat +0.1 ignored how long the chord's own constituent notes
        # actually ring — a final chord held for several seconds could get
        # windowed as if it ended almost immediately, truncating (or, in the
        # no-sections fallback, altogether dropping) its own sustain tail.
        c_notes = c.get("notes", []) or []
        max_sus = max((float(n.get("sus", 0)) for n in c_notes), default=0.0)
        duration = max(duration, float(c.get("t", 0)) + max(max_sus, 0.1))
    if duration <= 0.0:
        duration = 30.0

    # #103/B3: whether `windows` below are AUTHORED phrase/section
    # boundaries (from the caller's section_times, or the arrangement's own
    # sections) versus mechanically GENERATED ones (measure-aligned or
    # blind 30s chunks) -- only authored boundaries are real musical
    # phrases whose start/end deserve retention value; a generated window
    # edge is an arbitrary cut point, not a phrase (see the boundary-value
    # loop below).
    windows_are_authored = bool(section_times) or bool(sections)
    if section_times:
        # The caller supplies the song-global timeline that feedBack streams
        # through highway.getSections().  Keep an interval for every boundary,
        # including a section with no notes in this particular arrangement:
        # Section Map is song-level while note content is arrangement-level.
        secs = _valid_section_times(section_times)
        windows = []
        for i, t0 in enumerate(secs):
            # An arrangement can end before the song-level timeline because
            # it has a long rest/outro.  Still retain the final canonical
            # section for this arrangement; its empty phrase is what keeps the
            # one-to-one Section Map contract intact.
            t1 = secs[i + 1] if i + 1 < len(secs) else max(duration, t0 + 0.001)
            if t1 > t0:
                windows.append((t0, t1))
    elif sections:
        # Same validation as the canonical section_times path above (both
        # were missing-as-zero/negative/non-finite/duplicate hazards before
        # #69) -- this branch additionally used to skip the zero-length/
        # reversed-interval guard entirely, so a duplicate or malformed
        # section time here could silently emit a degenerate window.
        raw_times = [
            s.get("time", s.get("start_time", 0)) for s in sections if isinstance(s, dict)
        ]
        secs = _valid_section_times(raw_times)
        windows = []
        for i, t0 in enumerate(secs):
            t1 = secs[i + 1] if i + 1 < len(secs) else duration
            if t1 > t0:
                windows.append((t0, t1))
    elif (measure_windows := _measure_aligned_windows(beats, duration)) is not None:
        windows = measure_windows
    else:
        windows, t = [], 0.0
        while t < duration:
            windows.append((t, min(t + 30.0, duration)))
            t += 30.0
    if not windows:
        windows = [(0.0, duration)]

    beat_times = [float(b.get("time", 0)) for b in beats]
    tempo = _TempoParams.from_beats(beat_times, beats)

    link_next_keep_ids = None
    if is_keys:
        groups_all = _group_notes_keys(notes, chords)
        _score_groups_keys(groups_all, beat_times, tempo=tempo)
        # Keys arrangements have no tuning/string model to approximate pitch
        # from -- their notes carry REAL MIDI pitch (`_note_midi_keys`), so
        # the key estimate below reads pitch directly off the notes rather
        # than being skipped (the old comment's premise was wrong -- keys
        # notes carry absolute pitch; #179).
        effective_is_bass = False
    else:
        groups_all = _group_notes(
            notes, chords, time_window_ms=tempo.time_window_ms,
            hand_shapes=hand_shapes, chord_templates=chord_templates,
        )
        # is_bass, when the caller passed one (the EFFECTIVE value, after
        # any manifest entry override -- see _generate_one), overrides the
        # embedded-only fallback so the 5-string interval row (#103/B5,
        # see _string_intervals) matches what core would actually resolve.
        effective_is_bass = (
            is_bass if is_bass is not None
            else _is_bass_arrangement(arr.get("type", ""), arr.get("name", ""))
        )
        _score_groups(
            groups_all, n_strings, beat_times, tempo=tempo, tuning=tuning,
            is_bass=effective_is_bass,
        )
        # A phrase-local ln check alone can't tell "the target was pruned
        # away" apart from "the target is simply in the next phrase" --
        # compute cross-phrase survivorship once up front (issue #68
        # review follow-up) rather than per phrase/level.
        link_next_keep_ids = _global_link_next_survivors(groups_all)
    # #103/B7: per-SECTION key estimate (a song can modulate). Applied
    # PRE-threshold, over `windows` (the same section/phrase boundaries
    # used below), so the discount participates in `global_thresholds`
    # construction below exactly like the B2 beat-value and B5 melody-
    # turning-point terms already do inside _score_groups -- applying
    # it AFTER the tier scale is frozen would re-label a whole
    # phrase's groups against cutoffs that never saw the discount,
    # which can silently collapse a tier on ordinary tonal material
    # (caught in PR #125 review). Gated per-window on the correlation
    # guard (a poor tonal fit -- near-uniform/atonal pitch-class
    # content -- disables the weighting for that window rather than
    # ranking notes against an estimate the data doesn't support).
    # #179: the weighting now runs for keys too (reading pitch off the
    # notes' real MIDI values via `_note_midi_keys`), using the keys-
    # specific `_KEYS_KEY_STABILITY_RETENTION_BONUS` coefficient rather
    # than the fretted `_KEY_STABILITY_RETENTION_BONUS` -- a keys group's
    # `cost` scale is far narrower than the fretted one's, so the fretted
    # bonus would overwhelm it.
    chord_windows = _chord_pitch_class_windows(
        chords, tuning, n_strings, effective_is_bass, is_keys=is_keys,
    )
    key_retention_bonus = (
        _KEYS_KEY_STABILITY_RETENTION_BONUS if is_keys else _KEY_STABILITY_RETENTION_BONUS
    )
    for (win_t0, win_t1) in windows:
        window_groups = [g for g in groups_all if win_t0 <= g["time"] < win_t1]
        if not window_groups:
            continue
        key_est = _estimate_key(
            window_groups, tuning, n_strings, effective_is_bass, is_keys=is_keys,
        )
        if key_est is None or key_est[2] < _KEY_FIT_MIN_CORRELATION:
            continue
        tonic_pc, is_major, _corr = key_est
        for g in window_groups:
            bonus = _group_key_stability_bonus(
                g, tonic_pc, is_major, tuning, n_strings, effective_is_bass,
                chord_windows=chord_windows, is_keys=is_keys,
                retention_bonus=key_retention_bonus,
            )
            if bonus:
                g["retention_score"] = max(0.0, g["retention_score"] - bonus)

    top_tier = n_levels - 1
    global_thresholds = _tier_thresholds([g["retention_score"] for g in groups_all], n_levels)

    phrases_out = []
    for widx, (t0, t1) in enumerate(windows):
        phrase_groups = [g for g in groups_all if t0 <= g["time"] < t1]
        if not phrase_groups:
            # Preserve the canonical Section Map boundary in this arrangement.
            # An empty level is intentional: there is no chart content to
            # assign/filter here, but dropping the phrase would shift all later
            # phrases out of one-to-one alignment with the song timeline.
            phrases_out.append({
                "start_time": round(t0, 3),
                "end_time": round(t1, 3),
                "max_difficulty": 0,
                "difficulty_cost": 0.0,
                "levels": [{
                    "difficulty": 0, "notes": [], "chords": [],
                    "anchors": [], "handshapes": [],
                }],
            })
            continue
        # #103/B3: nudge the phrase's first/last group's retention_score
        # down (same direction/mechanism as the beat-alignment discount
        # above) so a thinned tier keeps the phrase's opening and closing
        # material when the rest of the tier allows it -- listeners split
        # music into phrases and remember boundaries (Lerdahl & Jackendoff
        # 1983; Knösche et al. 2005), but that keeping boundary notes
        # specifically helps LEARNING is inferred, not tested (moderate
        # evidence, see #105). Only for AUTHORED phrase boundaries
        # (windows_are_authored) -- a generated window's edges are
        # arbitrary cut points, not real phrase starts/ends -- EXCEPT the
        # very first window's start and the very LAST window's end, both of
        # which are always genuine boundaries (the song's own beginning and
        # end) regardless of how the windows in between were generated:
        # both generated-window builders (_measure_aligned_windows and the
        # blind 30s chunker) clamp their final window's end to `duration`,
        # so unlike an INTERNAL generated edge, the last window's end really
        # is the song's actual end (caught in PR #123 review).
        # Keys only: the boundary onset is every group sharing the first/last
        # time, since a two-hand onset is split into one group per hand and
        # giving the bonus to only one half would leave the other (often the
        # melody) without it. The fretted path keeps its single boundary group.
        if windows_are_authored or widx == 0:
            first_t = phrase_groups[0]["time"]
            for first in phrase_groups:
                if first["time"] != first_t or (not is_keys and first is not phrase_groups[0]):
                    break
                first["retention_score"] = max(
                    0.0, first["retention_score"] - _PHRASE_BOUNDARY_RETENTION_BONUS,
                )
        is_last_window = widx == len(windows) - 1
        if windows_are_authored or is_last_window:
            # #184: a window holding a single onset is by construction its
            # own opening AND closing material. The old guard
            # `phrase_groups[-1] is not phrase_groups[0]` skipped the ending
            # discount for such a window entirely, so a generated (non-authored)
            # last window holding one onset got NO boundary discount at all --
            # its retention stayed at the raw cost (measured: a keys final
            # window holding one two-hand onset kept retention 0.41, equal to
            # its raw cost). The last window's end is the song's real end, so
            # the closing material is meant to be kept when the rest of the
            # tier allows it, same as any other last window.
            last_t = phrase_groups[-1]["time"]
            if (windows_are_authored or widx == 0) and last_t == phrase_groups[0]["time"]:
                # Exactly the groups the first-onset block above discounted:
                # phrase_groups[0] on the fretted path, every group at first_t
                # on the keys path.
                first_onset_groups = (
                    frozenset(id(g) for g in phrase_groups if g["time"] == last_t)
                    if is_keys
                    else frozenset([id(phrase_groups[0])])
                )
            else:
                first_onset_groups = frozenset()
            # Discount EVERY group sharing the final onset, on both paths. The
            # fretted path used to break as soon as it left phrase_groups[-1],
            # so a final window holding a double stop (two groups at one onset)
            # discounted only the last of them and left the other closing
            # material at its raw retention score -- while the keys path, which
            # keys on time rather than identity, discounted both. A closing
            # onset is a closing onset regardless of how many voices share it
            # (measured: a fretted final window with fret-8 and fret-17 groups
            # at t=31.5 kept retention 0.0793 / 0.2237 raw and applied the
            # ending discount to 0.1237 only). Groups that already received the
            # first-onset discount in this pass are skipped so a single onset
            # that is its own opening and closing is discounted exactly once,
            # preserving the pre-existing behaviour for an authored window that
            # is both first and last (first-onset discount only, see issue
            # #184's open question 2).
            for last in reversed(phrase_groups):
                if last["time"] != last_t or id(last) in first_onset_groups:
                    break
                last["retention_score"] = max(
                    0.0, last["retention_score"] - _PHRASE_BOUNDARY_RETENTION_BONUS,
                )
        # Every phrase is built on the same n_levels-tier scale (see
        # _assign_tiers); how many DISTINCT levels a phrase ends up with
        # follows from how hard its content is, once
        # _collapse_identical_levels drops the tiers where it has stopped
        # changing — an easy riff is complete early, a hard passage differs
        # at every tier.
        _assign_tiers(phrase_groups, n_levels, global_thresholds, beat_times, tempo=tempo)
        # #181: the keys bottom-tier density floor. Fretted is
        # untouched -- its degenerate-sparsity failure mode is
        # keys-only (a keys group is one onset, often a whole
        # chord, so the proportional group-share floor can
        # materialize as 1-2 notes; see the _KEYS_TIER0 design
        # comment in the keys constants region).
        if is_keys:
            _keys_tier0_floor(phrase_groups, t0, t1, beat_times, tempo, n_levels)
        # #103/B10: computed once per phrase (not per level) since the
        # landmark stage only ever applies at level 0 -- see the loop below.
        chord_stage_drop_ids = (
            _staged_chord_drop_ids(phrase_groups, chord_templates)
            if staged_chords and not is_keys else frozenset()
        )
        # Per-phrase refinement (promoting beat/bridge anchors) breaks consistent
        # difficulty mapping: equal-retention groups can end up at different tiers
        # when one phrase's local playability needs trigger promotions that don't
        # occur in another phrase. Disabling it preserves the shared global tier
        # scale. _refine_lower_tier_path and its bridge helpers (above, in
        # this file's tiering region) are now unused in production pending the TODO below.
        # TODO: incorporate playability constraints into arrangement-wide
        # tier assignment (before generating phrase levels) instead of post-hoc.
        levels_out = []
        for lvl in range(n_levels):
            if is_keys:
                lvl_notes, lvl_chords = _notes_for_level_keys(phrase_groups, lvl, top_tier)
            else:
                lvl_notes, lvl_chords = _notes_for_level(
                    phrase_groups, lvl, top_tier, link_next_keep_ids=link_next_keep_ids,
                    chord_templates=chord_templates, tuning=tuning, n_strings=n_strings,
                    is_bass=effective_is_bass,
                    # #103/B10: the landmark stage is the BOTTOM tier only
                    # ("staged tiers on top of, not replacing, the existing
                    # progression" -- issue #121); every tier above it goes
                    # through the normal per-group voicing/technique curve
                    # with every occurrence present, same as staged_chords
                    # disabled.
                    chord_stage_drop_ids=chord_stage_drop_ids if lvl == 0 else frozenset(),
                )
            # Fret anchors and hand shapes are fretboard concepts the piano
            # renderer never consumes (mirrors feedBack's own editor plugin's
            # keys difficulty support) — leave them empty for keys.
            lvl_anchors = (
                [] if is_keys
                else _generate_anchors(
                    _notes_for_anchors(lvl_notes, lvl_chords), beat_times,
                    phrase_start=t0, phrase_end=t1,
                )
            )
            levels_out.append({
                "difficulty": lvl,
                "notes": lvl_notes,
                "chords": lvl_chords,
                "anchors": lvl_anchors,
                "handshapes": [],  # not generated in v1 — additive, safe to omit
            })
        levels_out = _collapse_identical_levels(levels_out)
        phrases_out.append({
            "start_time": round(t0, 3),
            "end_time": round(t1, 3),
            # The shared tier scale, so a reader maps the slider identically
            # onto every phrase. A phrase whose tiers all collapsed to one
            # level has no ladder at all, reported as 0 (the same convention
            # core uses for a single-level phrase).
            "max_difficulty": top_tier if len(levels_out) > 1 else 0,
            # Mechanical difficulty of the phrase's full content, independent
            # of ladder depth — see _phrase_mechanical_cost. Additive: absent
            # is not a valid state a reader needs to handle differently, but
            # older readers that don't know the key simply ignore it.
            "difficulty_cost": round(_phrase_mechanical_cost(phrase_groups), 4),
            "levels": levels_out,
        })
    return phrases_out if phrases_out else None



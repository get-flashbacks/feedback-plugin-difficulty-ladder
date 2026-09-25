"""Difficulty Ladder plugin — backend routes.

Generates a phrase-level difficulty ladder (Easy..Hard) for sloppak
arrangements that don't have one yet, using a note/chord-density heuristic —
so the frontend's live accuracy auto-adjust and glass HUD (screen.js) have
something to work with on songs that were never authored with phrase data
(GP imports, plain single-level sloppaks).

This is a fresh implementation against feedBack's own arrangement wire
format (lib/song.py — t/s/f/sus/... note keys, {t,id,hd,notes:[...]} chords),
operating directly on the raw JSON dicts as stored in a sloppak's
arrangements/*.json. It does not round-trip through the Note/Chord
dataclasses, so any key core doesn't model yet survives untouched, and it
does not port code from Slopsmith's differently-scoped editor plugin —
only the general "score groups, bucket into percentile tiers, thin lower
tiers" approach is reused as a heuristic design, reimplemented here against
feedBack's actual data.
"""

import bisect
import json
import math
import os
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictBool
import yaml

import sloppak
from dlc_paths import _resolve_dlc_path
from jsonc import parse_jsonc
from safepath import safe_join

PLUGIN_ID = "difficulty_ladder"

MIN_EVENTS_FOR_GENERATION = 8  # skip near-empty arrangements — nothing to grade
FRET_JUMP_WINDOW_SECONDS = 1.0  # longer rests give the player time to reposition
MAX_PROCESSING_SECONDS = 120  # hard cap per /generate-library call to bound CPU/DoS risk

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

    @classmethod
    def from_beats(cls, beat_times):
        """Derive tempo-relative thresholds from a song's own beat grid,
        or fall back to the absolute defaults above when there's no
        trustworthy tempo signal (see _median_beat_interval)."""
        beat_interval = _median_beat_interval(beat_times)
        if not beat_interval:
            return cls()
        return cls(
            time_window_ms=beat_interval * 1000 * _GROUP_WINDOW_BEAT_FRACTION,
            beat_tolerance=beat_interval * _BEAT_ALIGN_TOLERANCE_FRACTION,
            fret_jump_window_seconds=beat_interval * _FRET_JUMP_WINDOW_BEATS,
            sustain_ease_norm_seconds=beat_interval * _SUSTAIN_EASE_BEATS,
            beat_interval=beat_interval,
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
    fragment of a larger one."""
    if not chord_templates:
        return False
    by_string = {}
    for n in cluster:
        by_string[n.get("s", 0)] = n.get("f", 0)
    if len(by_string) < 2:
        return False
    for ct in chord_templates:
        frets = ct.get("frets") or []
        if not all(0 <= s < len(frets) and frets[s] == f for s, f in by_string.items()):
            continue
        template_used = sum(1 for fr in frets if fr >= 0)
        if template_used < 2:
            continue
        min_share = min(3, math.ceil(template_used / 2))
        if len(by_string) >= min_share:
            return True
    return False


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
        groups.append({
            "type": _classify_cluster(
                cluster, hand_shapes=hand_shapes, chord_templates=chord_templates,
            ),
            "notes": cluster, "chord": None,
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
    authored chord identity (a matching `ChordTemplate`/`chord_id`, or —
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


def _is_beat_aligned(t, beat_times, tolerance=0.06):
    return any(abs(float(t) - beat) <= tolerance for beat in beat_times)


def _syncopation_score(t, beat_times, beat_interval):
    """How far a group's onset lands from the nearest beat, as a fraction
    of a half-beat (landing exactly between two beats — the "and" of an
    off-beat eighth, the hardest possible offset to internalize — maxes
    this out at 1.0; landing on the beat is 0.0).

    Returns 0.0 (safe no-op) when there's no beat grid or no trustworthy
    tempo — this is a refinement layered onto the existing note-count
    density signal, not a replacement, so absent tempo data must not
    silently zero out density scoring altogether.
    """
    if not beat_times or not beat_interval:
        return 0.0
    nearest = min(abs(float(t) - b) for b in beat_times)
    return min(1.0, nearest / (beat_interval / 2.0))


# Syncopation blended into the density sub-score at this weight — enough to
# separate a straight run of eighth notes from an equally-dense syncopated
# off-beat pattern (independently harder to read per basic rhythm
# pedagogy) without letting off-grid-ness alone dominate over actual note
# count, which stays the primary density signal.
_SYNCOPATION_DENSITY_WEIGHT = 0.30

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


def _score_groups(groups, n_strings, beat_times=(), *, tempo=None):
    tempo = tempo or _TempoParams()
    times_sorted = [float(g["time"]) for g in groups]
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
        # each note to [0,1] on its own (routes.py's _tech_score), so a
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
        syncopation = _syncopation_score(g["time"], beat_times, tempo.beat_interval)
        density = min(1.0, (1.0 - _SYNCOPATION_DENSITY_WEIGHT) * raw_density
                      + _SYNCOPATION_DENSITY_WEIGHT * syncopation)
        max_sus = max(float(n.get("sus", 0)) for n in ns)
        sustain_ease = min(1.0, max_sus / tempo.sustain_ease_norm_seconds)
        base_cost = (
            0.35 * fretting + 0.30 * technique + 0.20 * density + 0.15 * (1.0 - sustain_ease)
        )
        # Cost is purely mechanical. Retention value is tracked separately so
        # later policies can change what is worth preserving without rewriting
        # the intrinsic difficulty model.
        value = float(
            _is_beat_aligned(g["time"], beat_times, tolerance=tempo.beat_tolerance)
        )
        cost = base_cost
        # Keep the legacy operation order byte-for-byte: base score, beat
        # discount, jump bonuses, final clamp. Computing this as
        # `cost - 0.12 * value` after adding the jumps is algebraically equal
        # but can round to a different float.
        retention_score = base_cost
        if value:
            retention_score -= 0.12
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
    # Ties in retention score go to beat-aligned groups first: a thinned tier
    # keeps its rhythmic landmarks up front (the same bias represented by
    # value applies), then _spread_key spreads the rest.
    ranked = sorted(range(total), key=lambda i: (
        groups[i]["retention_score"],
        0 if _is_beat_aligned(groups[i]["time"], beat_times, tolerance=tempo.beat_tolerance) else 1,
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
            beat_penalty = 0 if _is_beat_aligned(candidate["time"], beat_times, tolerance=tempo.beat_tolerance) else 1
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
    """Pick `n` notes from a chord's notes (already sorted by ascending
    string index — see _notes_for_level's comment on that convention)
    for a reduced voicing. Always keeps the lowest-string-index (bass)
    note (ranked[0]), then greedily adds whichever remaining note keeps the
    voicing's own fret span (_fret_span) smallest. An open string (f=0)
    contributes nothing to the span, so it's always a free, no-stretch add.

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


def _notes_for_level(groups, level, max_level, *, link_next_keep_ids=None):
    """Return (notes, chords) wire lists at/below `level`.

    Below the top tier, chords are reduced by voicing and flattened to plain
    notes (no chord_id references — avoids stale chord-template indices on
    a level that never goes through chord reconstruction); the max-difficulty
    tier keeps chords intact and byte-identical to the source.

    `link_next_keep_ids` (see _global_link_next_survivors) is threaded
    straight through to _clear_orphaned_link_next.
    """
    diff_percent = (level + 1) / (max_level + 1) if max_level >= 0 else 1.0
    out_notes = []
    out_chords = []
    for g in groups:
        if g["level"] > level:
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
                # Bass-note-only very early, then a partial voicing that
                # grows by one note at a mid-ladder threshold, mirroring the
                # keys path's outer-voices -> +middle -> full progression —
                # authored ladders widen chords quickly (this is a
                # bottom-tier-only thing) but a 4+-note chord still gets a
                # real middle rung instead of jumping straight from 2 notes
                # to the full voicing.
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
            if level == 0:
                # Highest-string-index, not hand-position: an open string
                # at that index is a valid, easier bottom-tier
                # simplification, so don't skew toward a fretted note here
                # the way the jump-scoring anchor does.
                anchor = _group_anchor_note(g, prefer_fretted=False) or ns[0]
                out_notes.append(_prune_note_for_level(anchor, diff_percent))
            else:
                # Always include the bottom tier's root, then the earliest
                # remaining notes, so each tier is a superset of the one
                # below (taking just ns[:keep_n] could drop the root the
                # bottom tier kept).
                keep_n = max(1, (len(ns) * (level + 1)) // max_level)
                root = _group_anchor_note(g, prefer_fretted=False) or ns[0]
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


def _group_notes_keys(notes, chords, *, onset_window_ms=30):
    """Group keys notes into atomic units. No fretboard, so grouping is
    purely temporal: explicit chords stay chords, remaining notes sharing an
    onset (within `onset_window_ms`) become a block-chord cluster."""
    groups = []
    for ch in chords:
        groups.append({
            "type": "chord", "notes": list(ch.get("notes", []) or []), "chord": ch,
            "time": float(ch.get("t", 0)), "cost": 0.0, "value": 0.0,
            "retention_score": 0.0, "level": 0,
        })

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
        groups.append({
            "type": "chord" if len(cluster) > 1 else "note",
            "notes": cluster, "chord": None,
            "time": base_t, "cost": 0.0, "value": 0.0,
            "retention_score": 0.0, "level": 0,
        })
        i = j

    groups.sort(key=lambda g: g["time"])
    return groups


def _score_groups_keys(groups, *, tempo=None):
    tempo = tempo or _TempoParams()
    total = len(groups)
    times_sorted = [float(g["time"]) for g in groups]
    for gi, g in enumerate(groups):
        ns = g["notes"]
        if not ns:
            g["cost"] = 0.0
            g["value"] = 0.0
            g["retention_score"] = 0.0
            continue
        midis = [_note_midi_keys(n) for n in ns]

        poly = min(1.0, (len(ns) - 1) / 4.0)  # 1 note=0, 5+ at once=1
        span = (max(midis) - min(midis)) if len(midis) > 1 else 0
        span_score = min(1.0, span / 12.0)  # an octave reach = 1.0

        # Distinct onsets in a tempo-relative time window, not each nearby
        # group's own note count -- same reasoning as the fretted path's
        # _sequential_density (#71): a wide block chord shouldn't inflate
        # density on its own, since polyphony is already `poly` above.
        density = _sequential_density(times_sorted, gi, tempo)

        speed = 0.0
        if gi + 1 < total:
            dt = float(groups[gi + 1]["time"]) - float(g["time"])
            if dt > 0:
                speed = min(1.0, max(0.0, (0.25 - dt) / 0.25))

        max_sus = max(float(n.get("sus", 0)) for n in ns)
        sustain_ease = min(1.0, max_sus / 2.0)

        cost = (
            0.30 * poly + 0.25 * span_score + 0.20 * density
            + 0.15 * speed + 0.10 * (1.0 - sustain_ease)
        )
        g["cost"] = cost
        g["value"] = 0.0
        g["retention_score"] = cost


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


def _notes_for_level_keys(groups, level, max_level):
    """Thin keys chords by pitch, keeping outer voices first — melody
    (highest pitch) + bass (lowest) at the bottom tier, growing inward,
    mirroring simplified piano sheet-music arrangements. No 'chords' output:
    everything flattens to individual notes, same as the fretted path's
    reduced tiers."""
    out_notes = []
    for g in groups:
        if g["level"] > level:
            continue
        ns = list(g["notes"])
        g_time = float(g.get("time", 0))
        is_explicit_chord = g.get("chord") is not None
        if len(ns) > 1 and level < max_level:
            ranked = sorted(ns, key=_note_midi_keys)
            # Every reduced tier starts with the simplified outer voices from
            # the easiest tier. When a newly-added middle voice is an octave
            # duplicate, prefer those established representatives so moving
            # up a tier can only add absolute MIDI identities, never swap one.
            outer = _collapse_octave_duplicates([ranked[0], ranked[-1]])
            if level == 0:
                keep = outer
            elif len(ranked) > 3:
                mid = len(ranked) // 2
                keep = [ranked[0], ranked[mid], ranked[-1]]
            else:
                keep = ranked
            seen = set()
            deduped = []
            for n in keep:
                if id(n) not in seen:
                    seen.add(id(n))
                    deduped.append(n)
            ns = deduped
            if len(ns) > 1:
                ns = _collapse_octave_duplicates(ns, preferred=outer)
        for n in ns:
            merged = dict(n)
            if is_explicit_chord or merged.get("t") is None:
                merged["t"] = g_time
            out_notes.append(merged)
    out_notes.sort(key=lambda n: float(n.get("t", 0)))
    return out_notes, []  # keys never emits chord-shaped entries at reduced tiers


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


def generate_phrases_for_arrangement(arr, *, n_levels=4, section_times: list[float] | None = None):
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
    tempo = _TempoParams.from_beats(beat_times)

    link_next_keep_ids = None
    if is_keys:
        groups_all = _group_notes_keys(notes, chords)
        _score_groups_keys(groups_all, tempo=tempo)
    else:
        groups_all = _group_notes(
            notes, chords, time_window_ms=tempo.time_window_ms,
            hand_shapes=hand_shapes, chord_templates=chord_templates,
        )
        _score_groups(groups_all, n_strings, beat_times, tempo=tempo)
        # A phrase-local ln check alone can't tell "the target was pruned
        # away" apart from "the target is simply in the next phrase" --
        # compute cross-phrase survivorship once up front (issue #68
        # review follow-up) rather than per phrase/level.
        link_next_keep_ids = _global_link_next_survivors(groups_all)

    top_tier = n_levels - 1
    global_thresholds = _tier_thresholds([g["retention_score"] for g in groups_all], n_levels)

    phrases_out = []
    for t0, t1 in windows:
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
        # Every phrase is built on the same n_levels-tier scale (see
        # _assign_tiers); how many DISTINCT levels a phrase ends up with
        # follows from how hard its content is, once
        # _collapse_identical_levels drops the tiers where it has stopped
        # changing — an easy riff is complete early, a hard passage differs
        # at every tier.
        _assign_tiers(phrase_groups, n_levels, global_thresholds, beat_times, tempo=tempo)
        # Per-phrase refinement (promoting beat/bridge anchors) breaks consistent
        # difficulty mapping: equal-retention groups can end up at different tiers
        # when one phrase's local playability needs trigger promotions that don't
        # occur in another phrase. Disabling it preserves the shared global tier
        # scale. _refine_lower_tier_path and its bridge helpers (routes.py
        # 726-820) are now unused in production pending the TODO below.
        # TODO: incorporate playability constraints into arrangement-wide
        # tier assignment (before generating phrase levels) instead of post-hoc.
        levels_out = []
        for lvl in range(n_levels):
            if is_keys:
                lvl_notes, lvl_chords = _notes_for_level_keys(phrase_groups, lvl, top_tier)
            else:
                lvl_notes, lvl_chords = _notes_for_level(
                    phrase_groups, lvl, top_tier, link_next_keep_ids=link_next_keep_ids,
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


# ── Sloppak read/write (dir or zip form) ─────────────────────────────────────

_ZIP_ROOT = Path("/_dd_root").resolve()


def _safe_member_name(rel: str) -> str | None:
    """Canonical, containment-checked member name for `rel`, or None if it
    would escape the pack root. `rel` comes from the manifest's own
    arrangement `file` value — trusted for reading (sloppak.read_member_bytes
    already validates it internally) but NOT pre-validated for this write
    path, so it gets the same safe_join-based check the directory-form write
    path already gets. Mirrors sloppak.py's own _zip_member_key normalization
    (NOT str.lstrip, which strips a character set rather than a "./" prefix
    and would treat "../../x" the same as "x")."""
    safe = safe_join(_ZIP_ROOT, rel or "")
    if safe is None or safe == _ZIP_ROOT:
        return None
    return safe.relative_to(_ZIP_ROOT).as_posix()


def _rewrite_zip_member(zip_path: Path, rel: str, new_bytes: bytes) -> None:
    """Replace ONE member's bytes inside a zip, preserving every other member.

    zipfile has no in-place member update, so this rebuilds the archive into
    a sibling temp file and atomically swaps it in.
    """
    rel_norm = _safe_member_name(rel)
    if rel_norm is None:
        raise ValueError(f"unsafe member path {rel!r}")
    tmp_path = zip_path.with_name(zip_path.name + ".dd_tmp")
    try:
        with zipfile.ZipFile(str(zip_path), "r") as zin:
            infos = zin.infolist()
            with zipfile.ZipFile(str(tmp_path), "w", zipfile.ZIP_DEFLATED) as zout:
                written = False
                for item in infos:
                    item_norm = _safe_member_name(item.filename)
                    if item_norm == rel_norm:
                        zout.writestr(item, new_bytes)
                        written = True
                    else:
                        zout.writestr(item, zin.read(item.filename))
                if not written:
                    zout.writestr(rel_norm, new_bytes)
        os.replace(str(tmp_path), str(zip_path))
    finally:
        # Rebuild failed partway (e.g. disk full) — don't leave a stray
        # .dd_tmp file behind; the original archive is untouched either way
        # since os.replace only runs after the rebuild succeeds.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# One lock per resolved pack path so two requests touching the SAME sloppak
# (e.g. a library-wide sweep and a single-song click, or two arrangements of
# one multi-arrangement song) serialize instead of each reading the original
# file, then racing to os.replace — which would silently drop whichever
# write lost the race. FastAPI runs sync `def` routes in a threadpool, so
# without this, concurrent requests really can interleave.
_pack_locks: dict[str, threading.Lock] = {}
_pack_locks_guard = threading.Lock()


def _lock_for_pack(pack_path: Path) -> threading.Lock:
    key = str(pack_path.resolve())
    with _pack_locks_guard:
        lk = _pack_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _pack_locks[key] = lk
        return lk


def _write_member_bytes(pack_path: Path, rel: str, data: bytes) -> None:
    if pack_path.is_dir():
        target = safe_join(pack_path.resolve(), rel)
        if target is None:
            raise ValueError(f"unsafe member path {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".dd_tmp")
        tmp.write_bytes(data)
        os.replace(str(tmp), str(target))
    else:
        _rewrite_zip_member(pack_path, rel, data)


def _load_manifest_and_arrangement(pack_path: Path, arrangement_index: int):
    manifest = sloppak.load_manifest(pack_path)
    entries = manifest.get("arrangements", []) or []
    if not (0 <= arrangement_index < len(entries)):
        raise HTTPException(400, f"arrangement_index {arrangement_index} out of range")
    entry = entries[arrangement_index]
    if not isinstance(entry, dict):
        raise HTTPException(400, "malformed arrangement entry")

    # Drum-part entries (feedpak 1.17.0 "drums as arrangements") point at a
    # `drum_tab` file, never a note/chord `file` — same routing sloppak.py's
    # own load_song() does. They never carry the notes/chords this generator
    # reads, so this is a clean, expected skip, not an error.
    entry_type = str(entry.get("type") or "").strip().lower()
    if entry_type in ("drums", "drum"):
        return None, None, "unsupported-instrument-drums"

    rel = str(entry.get("file", "")).strip()
    if not rel:
        raise HTTPException(400, "arrangement has no backing file")
    raw_bytes = sloppak.read_member_bytes(pack_path, rel)
    if raw_bytes is None:
        raise HTTPException(404, f"arrangement file {rel!r} not found in pack")
    text = raw_bytes.decode("utf-8")
    # read_member_bytes gives us bytes with no on-disk path (zip-form has
    # none at all), so jsonc.load_json (which requires a real Path to
    # stat the .jsonc suffix and read_text itself) doesn't apply here —
    # detect .jsonc by the manifest-declared relpath instead.
    arr = parse_jsonc(text) if rel.lower().endswith(".jsonc") else json.loads(text)
    return rel, arr, None


def _generate_one(pack_path: Path, arrangement_index: int, *, n_levels: int, force: bool, log,
                  section_times: list[float] | None = None) -> dict:
    # Hold the pack's lock across the whole read-modify-write span. Without
    # this, two requests touching the same pack (a library sweep + a manual
    # click, or two arrangements of one multi-arrangement song) can each read
    # the original zip before either writes, then race to os.replace() —
    # whichever finishes last silently discards the other's phrases.
    with _lock_for_pack(pack_path):
        rel, arr, skip_reason = _load_manifest_and_arrangement(pack_path, arrangement_index)
        if skip_reason:
            instrument = "drums" if skip_reason == "unsupported-instrument-drums" else None
            response = {"ok": True, "skipped": skip_reason, "arrangement_index": arrangement_index}
            if instrument:
                response["instrument"] = instrument
            return response
        instrument = _instrument_kind(arr.get("type", ""), arr.get("name", ""))
        # Usually drums are identified from the manifest entry before we read
        # this file.  Keep this second check because older/malformed packs can
        # label the manifest entry as Lead while the arrangement itself says
        # Drums.  Drum data has different semantics and must never be passed
        # through the fretted/keys phrase generator.
        if instrument == "drums":
            return {
                "ok": True, "skipped": "unsupported-instrument-drums",
                "arrangement_index": arrangement_index, "instrument": instrument,
            }
        if instrument == "unsupported":
            # Explicit allowlist miss (issue #66): a non-empty `type` this
            # generator doesn't recognize (e.g. vocals/harmony/notation) —
            # reported distinctly from "not enough content" so a caller can
            # tell "this generator doesn't support this instrument" apart
            # from "this arrangement was just too short to bother with".
            return {
                "ok": True, "skipped": "unsupported-instrument-type",
                "arrangement_index": arrangement_index, "instrument": instrument,
            }
        if not force and arr.get("phrases"):
            return {
                "ok": True, "skipped": "already-has-phrases",
                "arrangement_index": arrangement_index, "instrument": instrument,
            }

        phrases = generate_phrases_for_arrangement(
            arr, n_levels=n_levels, section_times=section_times
        )
        if phrases is None:
            return {
                "ok": True, "skipped": "not-enough-content-or-unsupported-instrument",
                "arrangement_index": arrangement_index, "instrument": instrument,
            }

        arr["phrases"] = phrases
        new_bytes = json.dumps(arr, ensure_ascii=False).encode("utf-8")
        _write_member_bytes(pack_path, rel, new_bytes)
    log.info("difficulty_ladder: generated %d phrases for %s arrangement %d",
              len(phrases), pack_path.name, arrangement_index)
    return {
        "ok": True, "arrangement_index": arrangement_index,
        "phrases": len(phrases),
        # `requested_levels` is the tier scale the caller asked for
        # (n_levels); `max_difficulty` is the highest phrase max_difficulty
        # actually written -- n_levels - 1 when at least one phrase has a
        # real ladder, 0 when every phrase collapsed to a single level
        # (_collapse_identical_levels). Reporting only `n_levels - 1` here
        # previously claimed a ladder generation may not have produced.
        "requested_levels": n_levels,
        "max_difficulty": max((p["max_difficulty"] for p in phrases), default=0),
        "instrument": instrument,
    }


def _read_pack_json(pack_path: Path, rel: str):
    """Read a JSON/JSONC member from either a directory or zip feedpak."""
    raw = sloppak.read_member_bytes(pack_path, rel)
    if raw is None:
        return None
    text = raw.decode("utf-8")
    return parse_jsonc(text) if rel.lower().endswith(".jsonc") else json.loads(text)


def _canonical_section_times(pack_path: Path, manifest: dict) -> list[float]:
    """Match feedBack's song-level section source for generated phrases.

    feedBack gives a valid ``song_timeline`` precedence over arrangement
    metadata; otherwise it takes sections from the first arrangement that has
    them.  This mirrors that selection so generated phrase intervals line up
    exactly with Section Map's ``highway.getSections()`` boundaries.
    """
    timeline_rel = manifest.get("song_timeline")
    if isinstance(timeline_rel, str) and timeline_rel.strip():
        try:
            timeline = _read_pack_json(pack_path, timeline_rel.strip())
        except ValueError:
            timeline = None
        if isinstance(timeline, dict) and isinstance(timeline.get("beats"), list) and isinstance(timeline.get("sections"), list):
            sections = timeline["sections"]
            times = []
            for section in sections:
                if not isinstance(section, dict):
                    continue
                try:
                    times.append(float(section.get("time", section.get("start_time", 0))))
                except (TypeError, ValueError):
                    continue
            if times:
                return times

    for entry in manifest.get("arrangements", []) or []:
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("file") or "").strip()
        if not rel:
            continue
        try:
            arrangement = _read_pack_json(pack_path, rel)
        except ValueError:
            continue
        sections = arrangement.get("sections", []) if isinstance(arrangement, dict) else []
        if not isinstance(sections, list) or not sections:
            continue
        times = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            try:
                times.append(float(section.get("time", section.get("start_time", 0))))
            except (TypeError, ValueError):
                continue
        if times:
            return times
    return []


def _is_unsupported_skip(reason) -> bool:
    """True for a skip reason meaning "this generator doesn't support this
    arrangement's instrument" (issue #66) — drums or an explicit allowlist
    miss — as opposed to "supported, but nothing to do" (already-has-phrases,
    not-enough-content) or a structural problem (malformed-arrangement)."""
    return isinstance(reason, str) and reason.startswith("unsupported-instrument")


def _generate_song(pack_path: Path, *, n_levels: int, force: bool, log) -> dict:
    """Generate every eligible arrangement in one song.

    Arrangement indices are manifest/storage indices, not the player UI's
    sorted display positions.  Each arrangement is classified independently
    by ``_generate_one`` so mixed guitar/bass/keys packs work correctly and
    drums (or another unsupported instrument type — issue #66) are
    explicitly reported as skipped, broken out from ``skipped`` into their
    own ``unsupported`` count.
    """
    manifest = sloppak.load_manifest(pack_path)
    entries = manifest.get("arrangements", []) or []
    section_times = _canonical_section_times(pack_path, manifest)
    results = []
    generated = skipped = failed = unsupported = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            results.append({"arrangement_index": index, "skipped": "malformed-arrangement"})
            skipped += 1
            continue
        try:
            result = _generate_one(
                pack_path, index, n_levels=n_levels, force=force, log=log,
                section_times=section_times or None,
            )
        except HTTPException as exc:
            # A bad arrangement must not prevent the remaining arrangements
            # in the song from receiving their difficulty ladders.
            result = {"arrangement_index": index, "error": exc.detail}
        except Exception as exc:  # noqa: BLE001 — same best-effort contract as
            # HTTPException above: a JSON/Unicode/filesystem/scoring failure on
            # one arrangement must not abort the rest of the song, and must not
            # bubble up to /generate as an opaque 500 that hides which
            # arrangements did succeed.
            if log is not None:
                log.exception(
                    "difficulty_ladder: unexpected error generating arrangement %d of %s",
                    index, pack_path.name,
                )
            result = {"arrangement_index": index, "error": str(exc)}
        results.append(result)
        if result.get("skipped") or result.get("error"):
            skipped += 1
            if result.get("error"):
                failed += 1
            elif _is_unsupported_skip(result.get("skipped")):
                unsupported += 1
        else:
            generated += 1
    return {
        "ok": True, "generated": generated, "skipped": skipped,
        "unsupported": unsupported, "failed": failed, "arrangements": results,
    }


class GenerateIn(BaseModel):
    """Body for POST .../generate. `force` is strict (only a real JSON
    boolean, never a truthy-string like "false") since `bool("false")`
    is True in Python and previously let any nonempty string clobber an
    authored ladder; `levels` is bounds-checked here instead of via a
    silent `max(2, min(..., 8))` clamp so out-of-range/non-numeric input
    is rejected (422) rather than silently coerced."""
    filename: str
    levels: int = Field(default=4, ge=2, le=8)
    force: StrictBool = False


class AnalyzeChordsIn(BaseModel):
    """Read-only chord grouping preview for one arrangement."""
    filename: str
    arrangement_index: int = Field(ge=0)


class GenerateLibraryIn(BaseModel):
    """Body for POST .../generate-library — same strictness as GenerateIn,
    plus a bounds-checked max_songs and a bounds-checked processing-time
    cap (issue #40) so a caller can shrink the budget for a quick sweep
    without being able to raise it past MAX_PROCESSING_SECONDS' own
    600s ceiling."""
    levels: int = Field(default=4, ge=2, le=8)
    force: StrictBool = False
    max_songs: int = Field(default=500, ge=1, le=2000)
    max_processing_seconds: int = Field(default=MAX_PROCESSING_SECONDS, ge=1, le=600)


def setup(app, context):
    log = context["log"]
    get_dlc_dir = context["get_dlc_dir"]

    def _resolve_pack(dlc_root: Path, filename: str) -> Path:
        safe = _resolve_dlc_path(dlc_root, filename)
        if safe is None:
            raise HTTPException(400, "invalid filename")
        if not sloppak.is_sloppak(safe):
            raise HTTPException(400, "Difficulty Ladder only generates phrase ladders for sloppak/feedpak songs")
        if not safe.exists():
            raise HTTPException(404, "song not found")
        return safe

    @app.post(f"/api/plugins/{PLUGIN_ID}/analyze-chords")
    def analyze_chords(body: AnalyzeChordsIn):
        """Preview Chordr identities and parent groups; never write a pack."""
        dlc_root = get_dlc_dir()
        if dlc_root is None:
            raise HTTPException(400, "no DLC library configured")
        pack_path = _resolve_pack(Path(dlc_root), body.filename.strip())
        try:
            _, arr, skip_reason = _load_manifest_and_arrangement(
                pack_path, body.arrangement_index
            )
        except HTTPException:
            raise
        except FileNotFoundError as exc:
            raise HTTPException(404, "song manifest not found") from exc
        except (UnicodeError, ValueError, yaml.YAMLError, zipfile.BadZipFile) as exc:
            raise HTTPException(400, "malformed arrangement") from exc
        if skip_reason or not isinstance(arr, dict):
            raise HTTPException(400, skip_reason or "malformed arrangement")
        if _instrument_kind(arr.get("type", ""), arr.get("name", "")) != "fretted":
            raise HTTPException(400, "chord grouping requires a fretted arrangement")
        chords = arr.get("chords", [])
        tuning = arr.get("tuning", [])
        templates = arr.get("templates") or arr.get("chordTemplates") or []
        if not all(isinstance(value, list) for value in (chords, tuning, templates)):
            raise HTTPException(400, "malformed arrangement")
        analyze = getattr(app.state, "chordr_analyze_chart_chords_v1", None)
        if not callable(analyze):
            raise HTTPException(503, "Chordr server analysis is not active")
        analysis_context = {
            "tuning": tuning,
            "capo": arr.get("capo", 0) or 0,
            "stringCount": len(tuning) or 6,
            "isBass": bool(re.search(
                r"\bbass\b", f"{arr.get('type') or ''} {arr.get('name') or ''}", re.IGNORECASE
            )),
        }
        try:
            analysis = analyze(
                chords, context=analysis_context,
                templates=templates,
            )
        except Exception as exc:
            log.exception("difficulty_ladder: Chordr analysis failed for %s", pack_path.name)
            raise HTTPException(503, "Chordr analysis failed") from exc
        return {"ok": True, "filename": body.filename,
                "arrangement_index": body.arrangement_index,
                "chord_count": len(chords), **analysis}

    @app.post(f"/api/plugins/{PLUGIN_ID}/generate")
    def generate(body: GenerateIn):
        filename = body.filename.strip()
        if not filename:
            raise HTTPException(400, "filename required")
        n_levels = body.levels
        force = body.force

        dlc_root = get_dlc_dir()
        if dlc_root is None:
            raise HTTPException(400, "no DLC library configured")
        pack_path = _resolve_pack(Path(dlc_root), filename)

        try:
            return _generate_song(pack_path, n_levels=n_levels, force=force, log=log)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — surface as a clean 500, never crash the server
            log.exception("difficulty_ladder: generate failed for %r", filename)
            raise HTTPException(500, str(e))

    @app.post(f"/api/plugins/{PLUGIN_ID}/generate-library")
    def generate_library(body: GenerateLibraryIn):
        """Best-effort sweep: generate a phrase ladder for every sloppak
        arrangement in the library that doesn't already have one. One bad
        pack must never abort the whole sweep."""
        n_levels = body.levels
        force = body.force
        max_songs = body.max_songs
        max_processing_seconds = body.max_processing_seconds

        dlc_root = get_dlc_dir()
        if dlc_root is None:
            raise HTTPException(400, "no DLC library configured")
        root = Path(dlc_root)

        generated, skipped, unsupported, failed = 0, 0, 0, []
        scanned = 0
        time_limit_reached = False
        start_time = time.monotonic()
        # Recursive, matching feedBack's own library scanner (lib/scan.py:
        # `dlc.rglob(f"*{ext}")` across both SONG_EXTS) — a shallow
        # root.iterdir() would silently miss any song organized in a
        # subfolder (e.g. DLC/ArtistName/Song.feedpak), which is a layout
        # core's own scan explicitly supports. Sorted (not just deduped via
        # the set) so which packs land inside vs. outside the max_songs/
        # time budget cutoff is deterministic, not filesystem-order-dependent.
        candidates = sorted(
            {p for ext in sloppak.SONG_EXTS for p in root.rglob(f"*{ext}")},
            key=lambda p: p.relative_to(root).as_posix(),
        )
        for entry in candidates:
            if scanned >= max_songs:
                break
            if time.monotonic() - start_time > max_processing_seconds:
                time_limit_reached = True
                break
            if not sloppak.is_sloppak(entry):
                continue
            label = entry.relative_to(root).as_posix()
            scanned += 1
            try:
                manifest = sloppak.load_manifest(entry)
            except Exception as e:  # noqa: BLE001
                failed.append({"filename": label, "error": str(e)})
                continue
            # Same canonical song-level section source _generate_song() uses
            # for the single-song /generate path (issue #67) — without this,
            # a library sweep computed phrase boundaries from each
            # arrangement's own `sections` field instead, which can diverge
            # both from the /generate path and from Section Map's
            # highway.getSections() for the same song.
            try:
                section_times = _canonical_section_times(entry, manifest)
            except Exception as e:  # noqa: BLE001 — a corrupt archive/filesystem
                # error here must not abort songs not yet visited, same
                # best-effort contract as the load_manifest guard above.
                failed.append({"filename": label, "error": str(e)})
                continue
            arr_entries = manifest.get("arrangements", []) or []
            for idx, arr_entry in enumerate(arr_entries):
                if not isinstance(arr_entry, dict) or not str(arr_entry.get("file", "")).strip():
                    continue
                if time.monotonic() - start_time > max_processing_seconds:
                    time_limit_reached = True
                    break
                try:
                    result = _generate_one(
                        entry, idx, n_levels=n_levels, force=force, log=log,
                        section_times=section_times or None,
                    )
                except HTTPException as e:
                    failed.append({"filename": label, "arrangement_index": idx, "error": e.detail})
                    continue
                except Exception as e:  # noqa: BLE001 — keep the sweep going
                    failed.append({"filename": label, "arrangement_index": idx, "error": str(e)})
                    continue
                if result.get("skipped"):
                    skipped += 1
                    if _is_unsupported_skip(result.get("skipped")):
                        unsupported += 1
                else:
                    generated += 1
            if time_limit_reached:
                break

        return {
            "ok": True, "scanned": scanned, "generated": generated,
            "skipped": skipped, "unsupported": unsupported, "failed": failed,
            "time_limit_reached": time_limit_reached,
        }

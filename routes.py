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

import json
import os
import re
import threading
import zipfile
from itertools import pairwise
from pathlib import Path

from fastapi import Body, HTTPException

import sloppak
from dlc_paths import _resolve_dlc_path
from jsonc import parse_jsonc
from safepath import safe_join

PLUGIN_ID = "difficulty_ladder"

MIN_EVENTS_FOR_GENERATION = 8  # skip near-empty arrangements — nothing to grade
FRET_JUMP_WINDOW_SECONDS = 1.0  # longer rests give the player time to reposition

# Same convention core uses for piano-roll mode (CLAUDE.md: "Any arrangement
# named Keys, Piano, Keyboard, or Synth renders as a piano-roll chart").
_KEYS_NAME_RE = re.compile(r"^(keys|piano|keyboard|synth)", re.IGNORECASE)


def _instrument_kind(arr_type: str, arr_name: str) -> str:
    """Classify an arrangement for generation purposes: 'fretted', 'keys',
    or 'drums'. Keys notes encode `midi = string*24 + fret` (no fretboard at
    all), so the guitar/bass fret-complexity heuristic below is meaningless
    for them and must not run — they get their own pitch/polyphony-based
    scoring. Drums arrangements never reach this module in the first place
    (see setup()'s manifest-entry check) since they carry no notes/chords
    file — 'drums' here is just for a clear, honest skip reason."""
    t = (arr_type or "").strip().lower()
    if t in ("piano", "keys"):
        return "keys"
    if t in ("drums", "drum"):
        return "drums"
    if _KEYS_NAME_RE.match((arr_name or "").strip()):
        return "keys"
    return "fretted"


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


# ── Scoring heuristic ────────────────────────────────────────────────────────

def _fret_score(fret):
    if fret <= 0:
        return 0.0
    return min(1.0, fret / 22.0)


def _span_score(notes):
    frets = [n.get("f", 0) for n in notes if n.get("f", 0) > 0]
    if len(frets) < 2:
        return 0.0
    return min(1.0, (max(frets) - min(frets)) / 6.0)


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
    score = 0.0
    if n.get("bn"):
        score += 0.4
    if n.get("ho") or n.get("po"):
        score += 0.25
    if n.get("tp"):
        score += 0.5
    if n.get("sl", -1) >= 0 or n.get("slu", -1) >= 0:
        score += 0.2
    if n.get("tr"):
        score += 0.3
    if n.get("hm") or n.get("hp"):
        score += 0.15
    return min(1.0, score)


def _group_notes(notes, chords, *, time_window_ms=150, fret_span_max=4):
    """Group flat wire notes/chords into atomic difficulty-scoring units.

    Simplified relative to a full chart editor's grouping (no link_next
    chain or hand-shape-window arpeggio detection) — explicit chords, then
    time-proximity clusters of otherwise-solo notes (fast runs / implicit
    chord-like clusters), then leftover individual notes.
    """
    groups = []
    for ch in chords:
        groups.append({
            "type": "chord", "notes": list(ch.get("notes", []) or []), "chord": ch,
            "time": float(ch.get("t", 0)), "score": 0.0, "level": 0,
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
            "type": "arpeggio" if len(cluster) > 1 else "note",
            "notes": cluster, "chord": None,
            "time": float(cluster[0].get("t", 0)), "score": 0.0, "level": 0,
        })

    groups.sort(key=lambda g: g["time"])
    return groups


def _group_anchor_note(group, *, prefer_fretted=True):
    """Return the fretted group's harmonic/hand-position anchor.

    Rocksmith string indices run high pitch to low pitch, so the highest
    string index is the same root convention used by chord reduction below.

    `prefer_fretted` (default True — used by the fret-jump scoring and
    lower-tier bridging below) picks a fretted note (f > 0) over an open
    one when the group has both: an open string needs no hand position at
    all, so letting it win as the anchor hides where the hand actually is,
    producing bogus fret-jump distances. `_notes_for_level`'s bottom-tier
    arpeggio note selection passes `prefer_fretted=False` — there the goal
    is the harmonic root regardless of fretted state, since an open root
    is a valid (indeed easier) simplification for the bottom tier, not a
    hand-position signal.
    """
    notes = group.get("notes", []) or []
    if prefer_fretted:
        fretted = [n for n in notes if n.get("f", 0) > 0]
        notes = fretted or notes
    return max(notes, key=lambda n: n.get("s", 0), default=None)


def _is_beat_aligned(time, beat_times, tolerance=0.06):
    return any(abs(float(time) - beat) <= tolerance for beat in beat_times)


def _syncopation_score(time, beat_times, beat_interval):
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
    nearest = min(abs(float(time) - b) for b in beat_times)
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
_STRING_JUMP_MAX_BONUS = 0.08  # capped lower than the fret-jump bonus (0.18) — a secondary signal


def _score_groups(groups, n_strings, beat_times=(), *,
                   fret_jump_window_seconds=FRET_JUMP_WINDOW_SECONDS,
                   beat_tolerance=0.06,
                   sustain_ease_norm_seconds=2.0,
                   beat_interval=None):
    total = len(groups)
    for gi, g in enumerate(groups):
        ns = g["notes"]
        if not ns:
            g["score"] = 0.0
            continue
        avg_fret = sum(n.get("f", 0) for n in ns) / len(ns)
        count_ratio = min(1.0, (len(ns) - 1) / max(n_strings - 1, 1))
        spread_ratio = _string_span_score(ns, n_strings)
        string_shape = _STRING_SPREAD_BLEND * spread_ratio + (1.0 - _STRING_SPREAD_BLEND) * count_ratio
        fretting = (
            0.4 * _fret_score(avg_fret)
            + 0.35 * _span_score(ns)
            + 0.25 * string_shape
        )
        technique = max(_tech_score(n) for n in ns)
        lo = max(0, gi - 5)
        hi = min(total, gi + 6)
        nearby = sum(len(groups[k]["notes"]) for k in range(lo, hi))
        raw_density = min(1.0, nearby / 20.0)
        syncopation = _syncopation_score(g["time"], beat_times, beat_interval)
        density = min(1.0, (1.0 - _SYNCOPATION_DENSITY_WEIGHT) * raw_density
                      + _SYNCOPATION_DENSITY_WEIGHT * syncopation)
        max_sus = max(float(n.get("sus", 0)) for n in ns)
        sustain_ease = min(1.0, max_sus / sustain_ease_norm_seconds)
        g["score"] = (
            0.35 * fretting + 0.30 * technique + 0.20 * density + 0.15 * (1.0 - sustain_ease)
        )
        # Easier tiers should retain rhythmic landmarks and avoid introducing
        # hand jumps that are absent between neighbouring authored groups.
        if _is_beat_aligned(g["time"], beat_times, tolerance=beat_tolerance):
            g["score"] -= 0.12
        if gi and float(g["time"]) - float(groups[gi - 1]["time"]) <= fret_jump_window_seconds:
            prev = _group_anchor_note(groups[gi - 1])
            cur = _group_anchor_note(g)
            if prev and cur:
                fret_jump = abs(int(cur.get("f", 0)) - int(prev.get("f", 0)))
                g["score"] += min(0.18, max(0, fret_jump - 5) * 0.03)
                string_jump = abs(int(cur.get("s", 0)) - int(prev.get("s", 0)))
                g["score"] += min(
                    _STRING_JUMP_MAX_BONUS,
                    max(0, string_jump - _STRING_JUMP_THRESHOLD) * _STRING_JUMP_COEF,
                )
        g["score"] = max(0.0, min(1.0, g["score"]))


# Retention-curve exponent: how sparse the bottom of a ladder is relative to
# a flat percentile split. Tuned against a wide sample of authored ladders
# (varied genres, both hand-tuned and tool-generated) — real ladders keep
# roughly 10-20% of a phrase's content at the bottom tier and ramp up
# convexly, not the ~1/n_levels flat share a plain percentile split gives.
# Raising rank-fraction to this power before indexing into the sorted score
# list pushes the low-tier cutoffs down without changing the top tier.
_RETENTION_CURVE_EXPONENT = 1.35

# Notes needed (per level of the cap) before a phrase is considered to have
# enough raw content to justify a deep ladder, independent of how varied
# that content is. A phrase can be short but still highly varied (or long
# but monotonous) — depth needs both signals, not just one.
_EVENTS_PER_LEVEL = 6


def _phrase_level_count(groups, cap):
    """Pick a per-phrase ladder depth (2..cap) from how much difficulty
    *variation* the phrase actually has, instead of using the same depth
    for every phrase in the arrangement (the single biggest gap between a
    mechanical percentile-bucket ladder and one that reads as purpose-built:
    a simple riff gets a short ladder, a technical passage gets a long one).

    Score spread alone isn't enough — a two-note phrase can have maximal
    spread but not enough raw content to fill out a deep ladder — so spread
    is scaled down when there isn't much to grade.
    """
    if not groups:
        return 2
    scores = [g["score"] for g in groups]
    spread = max(scores) - min(scores)
    total_notes = sum(max(1, len(g["notes"])) for g in groups)
    density_factor = min(1.0, total_notes / (_EVENTS_PER_LEVEL * cap))
    estimate = 2 + round(spread * density_factor * (cap - 2))
    return max(2, min(estimate, cap))


def _assign_levels(groups, n_levels, curve_exponent=_RETENTION_CURVE_EXPONENT):
    if not groups:
        return
    scores_sorted = sorted(g["score"] for g in groups)
    total = len(scores_sorted)
    thresholds = [
        scores_sorted[min(int(((i + 1) / n_levels) ** curve_exponent * total), total - 1)]
        for i in range(n_levels - 1)
    ]
    for g in groups:
        lvl = 0
        for t in thresholds:
            if g["score"] > t:
                lvl += 1
        g["level"] = min(lvl, n_levels - 1)


def _best_bridge_candidate(groups, left, right, level, beat_times, original_jump, *,
                            beat_tolerance=0.06):
    left_fret = int(_group_anchor_note(left).get("f", 0))
    right_fret = int(_group_anchor_note(right).get("f", 0))
    candidates = []
    for candidate in groups:
        if candidate["level"] <= level or not (left["time"] < candidate["time"] < right["time"]):
            continue
        anchor = _group_anchor_note(candidate)
        if not anchor:
            continue
        fret = int(anchor.get("f", 0))
        worst_jump = max(abs(fret - left_fret), abs(right_fret - fret))
        if worst_jump < original_jump:
            beat_penalty = 0 if _is_beat_aligned(candidate["time"], beat_times, tolerance=beat_tolerance) else 1
            candidates.append((worst_jump, beat_penalty, candidate["score"], candidate["time"], candidate))
    return min(candidates, key=lambda item: item[:4])[4] if candidates else None


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


def _refine_lower_tier_path(groups, beat_times, max_level, max_jump=7, *,
                             fret_jump_window_seconds=FRET_JUMP_WINDOW_SECONDS,
                             beat_tolerance=0.06,
                             max_string_jump=_MAX_STRING_JUMP_FOR_CONTINUITY):
    """Promote anchors needed for a playable, rhythmically grounded path.

    Promotions only add source groups to lower tiers, preserving nesting and
    providing a conservative fallback when percentile thinning omits every
    beat landmark or creates a large avoidable jump (fret position or
    string distance).
    """
    if not groups or max_level <= 0:
        return
    beat_groups = [g for g in groups if _is_beat_aligned(g["time"], beat_times, tolerance=beat_tolerance)]
    for level in range(max_level):
        kept = [g for g in groups if g["level"] <= level]
        if beat_groups and not any(g in beat_groups for g in kept):
            min(beat_groups, key=lambda g: (g["score"], g["time"]))["level"] = level
            kept = [g for g in groups if g["level"] <= level]

        while True:
            kept = sorted((g for g in groups if g["level"] <= level), key=lambda g: g["time"])
            promoted = False
            for left, right in pairwise(kept):
                if float(right["time"]) - float(left["time"]) > fret_jump_window_seconds:
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
                    groups, left, right, level, beat_times, original_jump,
                    beat_tolerance=beat_tolerance,
                )
                if candidate:
                    candidate["level"] = level
                    promoted = True
                    break
            if not promoted:
                break


# Fraction of the way up a phrase's own ladder (0..1) at which a technique
# is allowed to survive. Ordered coarsely from how corpus analysis (a wide
# sample of authored ladders, hand-tuned and tool-generated, across genres)
# showed these actually get introduced: sustain/legato-adjacent techniques
# (bends, vibrato) show up earliest, palm mutes and slides in the middle,
# harmonics/HOPO chains later, tremolo/tap reserved for the hardest tier.
_TECH_GATE_FRAC = {
    "bn": 0.50,
    "pm": 0.55, "mt": 0.55,
    "vb": 0.70,
    "hm": 0.75,
    "ho": 0.80, "po": 0.80,
    "sl": 0.85, "slu": 0.85,
    "tr": 0.90,
    "hp": 0.95,
    "tp": 0.95,
}


def _prune_techniques(note, diff_percent):
    """Strip technique flags a phrase hasn't "earned" yet at this rung of
    its own ladder, so a low tier reads as a simplified-but-intentional
    version of the part rather than a random note subset that happens to
    keep whatever techniques its underlying notes had."""
    out = dict(note)
    for key, gate in _TECH_GATE_FRAC.items():
        if diff_percent < gate:
            if key in ("sl", "slu"):
                out[key] = -1
            elif key == "bn":
                out[key] = 0
            else:
                out.pop(key, None)
    return out


def _pick_partial_voicing(ranked, n):
    """Pick `n` notes from a chord's notes (already sorted root-first — see
    _notes_for_level's docstring on the root convention) for a reduced
    voicing. Always keeps the root (ranked[0]), then greedily adds whichever
    remaining note keeps the voicing's own fret span smallest. An open
    string (f=0) contributes nothing to the span, so it's always a free,
    no-stretch add — the same filtering _span_score already uses for the
    full-group fret-span term.

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
            frets = [k.get("f", 0) for k in kept if k.get("f", 0) > 0]
            cf = cand.get("f", 0)
            if cf > 0:
                frets.append(cf)
            return (max(frets) - min(frets)) if len(frets) > 1 else 0
        best = min(remaining, key=_span_if_added)
        kept.append(best)
        remaining.remove(best)
    return kept


# Named for clarity — unchanged from the original tuned root-only threshold.
_CHORD_ROOT_ONLY_FRAC = 0.20
# A 4+-note chord's middle voice appears only in roughly the top half of a
# phrase's own ladder, keeping early tiers sparse the same way
# _RETENTION_CURVE_EXPONENT and _TECH_GATE_FRAC already bias the bottom of
# a ladder toward simplicity — a mid-tier chord widens root+1 -> root+2
# instead of jumping straight from a partial voicing to the full chord.
_CHORD_MID_VOICING_FRAC = 0.55


def _notes_for_level(groups, level, max_level):
    """Return (notes, chords) wire lists at/below `level`.

    Below the top tier, chords are reduced by voicing and flattened to plain
    notes (no chord_id references — avoids stale chord-template indices on
    a level that never goes through chord reconstruction); the max-difficulty
    tier keeps chords intact and byte-identical to the source.
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
                # String-index convention follows the arrangement source
                # (Rocksmith-derived): index 0 = highest-pitched string, so
                # the highest index among a chord's notes is its root.
                ranked = sorted(ch_notes, key=lambda n: n.get("s", 0), reverse=True)
                # root-only only very early, then a partial voicing that grows
                # by one note at a mid-ladder threshold, mirroring the keys
                # path's outer-voices -> +middle -> full progression — authored
                # ladders widen chords quickly (root-only is a bottom-tier-only
                # thing) but a 4+-note chord still gets a real middle rung
                # instead of jumping straight from 2 notes to the full voicing.
                if diff_percent < _CHORD_ROOT_ONLY_FRAC:
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
                # Root string, not hand-position: an open root is a valid,
                # easier bottom-tier simplification, so don't skew toward a
                # fretted note here the way the jump-scoring anchor does.
                anchor = _group_anchor_note(g, prefer_fretted=False) or ns[0]
                out_notes.append(_prune_techniques(anchor, diff_percent))
            else:
                keep_n = max(1, (len(ns) * (level + 1)) // max_level)
                out_notes.extend(_prune_techniques(n, diff_percent) for n in ns[:keep_n])
        else:
            if level < max_level:
                out_notes.extend(_prune_techniques(n, diff_percent) for n in g["notes"])
            else:
                out_notes.extend(g["notes"])
    out_notes.sort(key=lambda n: float(n.get("t", 0)))
    out_chords.sort(key=lambda c: float(c.get("t", 0)))
    return out_notes, out_chords


def _generate_anchors(notes, beat_times, *, default_width=4):
    if not notes:
        return []
    anchors = []
    prev_fret = prev_width = None
    for i, bt in enumerate(beat_times):
        bt_end = beat_times[i + 1] if i + 1 < len(beat_times) else bt + 2.0
        window = [n for n in notes if bt <= float(n.get("t", 0)) < bt_end and n.get("f", 0) >= 1]
        if not window:
            continue
        frets = [n.get("f", 0) for n in window]
        min_fret = max(1, min(frets))
        max_fret = max(frets)
        width = max(default_width, max_fret - min_fret + 3)
        if min_fret != prev_fret or width != prev_width:
            anchors.append({"time": round(bt, 3), "fret": min_fret, "width": width})
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
            "time": float(ch.get("t", 0)), "score": 0.0, "level": 0,
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
            "time": base_t, "score": 0.0, "level": 0,
        })
        i = j

    groups.sort(key=lambda g: g["time"])
    return groups


def _score_groups_keys(groups):
    total = len(groups)
    for gi, g in enumerate(groups):
        ns = g["notes"]
        if not ns:
            g["score"] = 0.0
            continue
        midis = [_note_midi_keys(n) for n in ns]

        poly = min(1.0, (len(ns) - 1) / 4.0)  # 1 note=0, 5+ at once=1
        span = (max(midis) - min(midis)) if len(midis) > 1 else 0
        span_score = min(1.0, span / 12.0)  # an octave reach = 1.0

        lo = max(0, gi - 5)
        hi = min(total, gi + 6)
        nearby = sum(len(groups[k]["notes"]) for k in range(lo, hi))
        density = min(1.0, nearby / 20.0)

        speed = 0.0
        if gi + 1 < total:
            dt = float(groups[gi + 1]["time"]) - float(g["time"])
            if dt > 0:
                speed = min(1.0, max(0.0, (0.25 - dt) / 0.25))

        max_sus = max(float(n.get("sus", 0)) for n in ns)
        sustain_ease = min(1.0, max_sus / 2.0)

        g["score"] = (
            0.30 * poly + 0.25 * span_score + 0.20 * density
            + 0.15 * speed + 0.10 * (1.0 - sustain_ease)
        )


def _collapse_octave_duplicates(ns):
    """Merge notes that are the same pitch class exactly one octave apart
    down to a single note — a doubled root/octave voicing plays the same to
    a beginner as the single note, so it's a free simplification on top of
    the voice-thinning above. Keeps whichever note came first in `ns` (i.e.
    whatever the existing keep-logic already selected as the surviving
    voice), just drops the redundant octave partner."""
    kept = []
    dropped = set()
    for i, n in enumerate(ns):
        if id(n) in dropped:
            continue
        midi_n = _note_midi_keys(n)
        for m in ns[i + 1:]:
            if id(m) in dropped:
                continue
            if abs(_note_midi_keys(m) - midi_n) == 12:
                dropped.add(id(m))
        kept.append(n)
    return kept


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
            if level == 0:
                keep = [ranked[0], ranked[-1]]
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
                ns = _collapse_octave_duplicates(ns)
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
    entry is `measure: -1` (sub-beat only, per feedpak-spec §6.8).
    """
    downbeat_times = sorted(
        float(b.get("time", 0)) for b in beats
        if isinstance(b, dict) and int(b.get("measure", -1)) > 0
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


def generate_phrases_for_arrangement(arr, *, n_levels=4, section_times: list[float] | None = None):
    """Build a phrase-level difficulty ladder for one arrangement's raw wire
    dict (as stored in a sloppak's arrangements/*.json).

    Read-only over `arr` — returns the `phrases` wire list to assign onto
    `arr["phrases"]`; every other key in `arr` is left untouched by the
    caller. Returns None when there isn't enough chart content to bother
    (an ambient/silent arrangement, or one already effectively empty), or
    when the arrangement's instrument isn't one this generator supports
    (currently: fretted guitar/bass and keys/piano — see _instrument_kind).
    """
    kind = _instrument_kind(arr.get("type", ""), arr.get("name", ""))
    if kind == "drums":
        return None  # shouldn't normally reach here — see setup()'s pre-check

    notes = arr.get("notes", []) or []
    chords = arr.get("chords", []) or []
    beats = arr.get("beats", []) or []
    sections = arr.get("sections", []) or []
    tuning = arr.get("tuning", [0] * 6) or [0] * 6
    n_strings = max(1, len(tuning))
    is_keys = (kind == "keys")

    total_events = len(notes) + sum(len(c.get("notes", []) or []) for c in chords)
    if total_events < MIN_EVENTS_FOR_GENERATION:
        return None

    duration = 0.0
    for n in notes:
        duration = max(duration, float(n.get("t", 0)) + float(n.get("sus", 0)))
    for c in chords:
        duration = max(duration, float(c.get("t", 0)) + 0.1)
    if duration <= 0.0:
        duration = 30.0

    if section_times:
        # The caller supplies the song-global timeline that feedBack streams
        # through highway.getSections().  Keep an interval for every boundary,
        # including a section with no notes in this particular arrangement:
        # Section Map is song-level while note content is arrangement-level.
        secs = sorted(float(t) for t in section_times)
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
        secs = sorted(sections, key=lambda s: float(s.get("time", s.get("start_time", 0))))
        windows = []
        for i, s in enumerate(secs):
            t0 = float(s.get("time", s.get("start_time", 0)))
            t1 = (
                float(secs[i + 1].get("time", secs[i + 1].get("start_time", 0)))
                if i + 1 < len(secs) else duration
            )
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
    beat_interval = _median_beat_interval(beat_times)
    if beat_interval:
        time_window_ms = beat_interval * 1000 * _GROUP_WINDOW_BEAT_FRACTION
        beat_tolerance = beat_interval * _BEAT_ALIGN_TOLERANCE_FRACTION
        fret_jump_window_seconds = beat_interval * _FRET_JUMP_WINDOW_BEATS
        sustain_ease_norm_seconds = beat_interval * _SUSTAIN_EASE_BEATS
    else:
        time_window_ms = 150.0
        beat_tolerance = 0.06
        fret_jump_window_seconds = FRET_JUMP_WINDOW_SECONDS
        sustain_ease_norm_seconds = 2.0

    if is_keys:
        groups_all = _group_notes_keys(notes, chords)
        _score_groups_keys(groups_all)
    else:
        groups_all = _group_notes(notes, chords, time_window_ms=time_window_ms)
        _score_groups(
            groups_all, n_strings, beat_times,
            fret_jump_window_seconds=fret_jump_window_seconds,
            beat_tolerance=beat_tolerance,
            sustain_ease_norm_seconds=sustain_ease_norm_seconds,
            beat_interval=beat_interval,
        )

    phrases_out = []
    for t0, t1 in windows:
        phrase_groups = [g for g in groups_all if t0 <= g["time"] < t1]
        if not phrase_groups:
            # Preserve the canonical Section Map boundary in this arrangement.
            # An empty level is intentional: there is no chart content to
            # score/filter here, but dropping the phrase would shift all later
            # phrases out of one-to-one alignment with the song timeline.
            phrases_out.append({
                "start_time": round(t0, 3),
                "end_time": round(t1, 3),
                "max_difficulty": 0,
                "levels": [{
                    "difficulty": 0, "notes": [], "chords": [],
                    "anchors": [], "handshapes": [],
                }],
            })
            continue
        # `n_levels` is a cap, not a fixed depth: a sparse phrase gets a
        # short ladder and a dense one gets a long one, instead of every
        # phrase in the arrangement sharing one global depth. Keys keeps a
        # fixed depth for now (its scoring/curve wasn't tuned for this).
        phrase_n_levels = n_levels if is_keys else _phrase_level_count(phrase_groups, n_levels)
        phrase_max_level = phrase_n_levels - 1
        _assign_levels(phrase_groups, phrase_n_levels)
        if not is_keys:
            _refine_lower_tier_path(
                phrase_groups, beat_times, phrase_max_level,
                fret_jump_window_seconds=fret_jump_window_seconds,
                beat_tolerance=beat_tolerance,
            )
        levels_out = []
        for lvl in range(phrase_n_levels):
            if is_keys:
                lvl_notes, lvl_chords = _notes_for_level_keys(phrase_groups, lvl, phrase_max_level)
            else:
                lvl_notes, lvl_chords = _notes_for_level(phrase_groups, lvl, phrase_max_level)
            # Fret anchors and hand shapes are fretboard concepts the piano
            # renderer never consumes (mirrors feedBack's own editor plugin's
            # keys difficulty support) — leave them empty for keys.
            lvl_anchors = [] if is_keys else _generate_anchors(lvl_notes, beat_times)
            levels_out.append({
                "difficulty": lvl,
                "notes": lvl_notes,
                "chords": lvl_chords,
                "anchors": lvl_anchors,
                "handshapes": [],  # not generated in v1 — additive, safe to omit
            })
        phrases_out.append({
            "start_time": round(t0, 3),
            "end_time": round(t1, 3),
            "max_difficulty": phrase_max_level,
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
        "phrases": len(phrases), "max_difficulty": n_levels - 1,
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


def _generate_song(pack_path: Path, *, n_levels: int, force: bool, log) -> dict:
    """Generate every eligible arrangement in one song.

    Arrangement indices are manifest/storage indices, not the player UI's
    sorted display positions.  Each arrangement is classified independently
    by ``_generate_one`` so mixed guitar/bass/keys packs work correctly and
    drums are explicitly reported as skipped.
    """
    manifest = sloppak.load_manifest(pack_path)
    entries = manifest.get("arrangements", []) or []
    section_times = _canonical_section_times(pack_path, manifest)
    results = []
    generated = skipped = failed = 0
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
        results.append(result)
        if result.get("skipped") or result.get("error"):
            skipped += 1
            if result.get("error"):
                failed += 1
        else:
            generated += 1
    return {
        "ok": True, "generated": generated, "skipped": skipped,
        "failed": failed, "arrangements": results,
    }


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

    @app.post(f"/api/plugins/{PLUGIN_ID}/generate")
    def generate(body: dict = Body(...)):
        filename = str((body or {}).get("filename") or "").strip()
        if not filename:
            raise HTTPException(400, "filename required")
        n_levels = max(2, min(int((body or {}).get("levels", 4) or 4), 8))
        force = bool((body or {}).get("force", False))

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
    def generate_library(body: dict = Body(...)):
        """Best-effort sweep: generate a phrase ladder for every sloppak
        arrangement in the library that doesn't already have one. One bad
        pack must never abort the whole sweep."""
        n_levels = max(2, min(int((body or {}).get("levels", 4) or 4), 8))
        force = bool((body or {}).get("force", False))
        max_songs = max(1, min(int((body or {}).get("max_songs", 500) or 500), 2000))

        dlc_root = get_dlc_dir()
        if dlc_root is None:
            raise HTTPException(400, "no DLC library configured")
        root = Path(dlc_root)

        generated, skipped, failed = 0, 0, []
        scanned = 0
        # Recursive, matching feedBack's own library scanner (lib/scan.py:
        # `dlc.rglob(f"*{ext}")` across both SONG_EXTS) — a shallow
        # root.iterdir() would silently miss any song organized in a
        # subfolder (e.g. DLC/ArtistName/Song.feedpak), which is a layout
        # core's own scan explicitly supports.
        candidates = sorted(
            {p for ext in sloppak.SONG_EXTS for p in root.rglob(f"*{ext}")},
            key=lambda p: p.relative_to(root).as_posix(),
        )
        for entry in candidates:
            if scanned >= max_songs:
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
            arr_entries = manifest.get("arrangements", []) or []
            for idx, arr_entry in enumerate(arr_entries):
                if not isinstance(arr_entry, dict) or not str(arr_entry.get("file", "")).strip():
                    continue
                try:
                    result = _generate_one(entry, idx, n_levels=n_levels, force=force, log=log)
                except HTTPException as e:
                    failed.append({"filename": label, "arrangement_index": idx, "error": e.detail})
                    continue
                except Exception as e:  # noqa: BLE001 — keep the sweep going
                    failed.append({"filename": label, "arrangement_index": idx, "error": str(e)})
                    continue
                if result.get("skipped"):
                    skipped += 1
                else:
                    generated += 1

        return {"ok": True, "scanned": scanned, "generated": generated, "skipped": skipped, "failed": failed}

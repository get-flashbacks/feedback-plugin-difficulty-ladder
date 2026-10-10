#!/usr/bin/env python3
"""Calibration script for keys hand-split threshold and melody bonus (issue #176).

Measures fraction of onsets split, tier-0 melody presence, nesting violations,
and collapsed-tier rate across a fixture set of piano textures while sweeping
_KEYS_HAND_SPLIT_SEMITONES and _KEYS_MELODY_LINE_BONUS.
"""

import sys
import json
from copy import deepcopy
from itertools import product
from typing import Any

sys.path.insert(0, "/workspace/app")

import scoring


def _midi_note(t, midi, sus=0.4):
    return {"t": round(t, 3), "s": midi // 24, "f": midi % 24, "sus": sus}


def _voice_texture(notes, bars, spb=0.5):
    beats = []
    for bar in range(bars):
        for b in range(4):
            beats.append({"time": round(bar * 4 * spb + b * spb, 3), "measure": bar if b == 0 else -1})
    return {
        "type": "keys", "name": "Keys", "notes": notes, "chords": [],
        "beats": beats, "sections": [], "tuning": [],
    }, spb


def _two_hand_keys_arrangement(bars=4):
    """LH broken-chord eighths under RH quarter-note melody."""
    spb = 0.6
    melody = [72, 74, 76, 77, 79, 77, 76, 74]
    lh = [48, 52, 55, 52, 48, 52, 55, 52]
    notes, beats = [], []
    for bar in range(bars):
        for b in range(4):
            beats.append({"time": round(bar * 4 * spb + b * spb, 3), "measure": bar if b == 0 else -1})
        for e in range(8):
            notes.append(_midi_note(bar * 4 * spb + e * spb / 2, lh[e]))
        for k in range(4):
            notes.append(_midi_note(bar * 4 * spb + k * spb, melody[(bar + k) % 8], 0.5))
    return {"type": "keys", "name": "Keys", "notes": notes, "chords": [],
            "beats": beats, "sections": [], "tuning": []}, spb


def _alberti_bass_arrangement(bars=4, spb=0.5):
    """LH Alberti C-G-E-G eighths under RH quarter-note melody."""
    melody = [72, 74, 76, 77]
    lh = [48, 55, 52, 55, 48, 55, 52, 55]
    notes = []
    for bar in range(bars):
        base = bar * 4 * spb
        for e in range(8):
            notes.append(_midi_note(base + e * spb / 2, lh[(e + bar) % 8]))
        for k in range(4):
            notes.append(_midi_note(base + k * spb, melody[(bar + k) % 4], 0.5))
    return _voice_texture(notes, bars, spb)


def _stride_arrangement(bars=4, spb=0.5):
    """LH stride: bass on 1/3, chord on 2/4, RH melody on 1/3."""
    notes = []
    for bar in range(bars):
        base = bar * 4 * spb
        notes.append(_midi_note(base, 36 if bar % 2 == 0 else 41, 0.5))
        notes.append(_midi_note(base + 2 * spb, 43 if bar % 2 == 0 else 46, 0.5))
        for k in (1, 3):
            for m in (48, 52, 55):
                notes.append(_midi_note(base + k * spb, m, 0.3))
        for k, m in ((0, 72), (2, 74)):
            notes.append(_midi_note(base + k * spb, m, 0.9))
    return _voice_texture(notes, bars, spb)


def _crossed_hand_arrangement(bars=4, spb=0.5):
    """Hands swap registers: high melody over low accompaniment in opposite hands."""
    mel = [72, 74, 76, 77]
    low = [48, 52, 55, 52]
    notes = []
    for bar in range(bars):
        base = bar * 4 * spb
        for k in range(4):
            notes.append(_midi_note(base + k * spb, mel[(bar + k) % 4], 0.5))
            notes.append(_midi_note(base + k * spb, low[(bar + k) % 4]))
    return _voice_texture(notes, bars, spb)


def _ballad_arrangement(bars=4, spb=0.5):
    """Ballad: LH rolling arpeggios (tenths) under RH sustained melody with ornaments."""
    notes = []
    for bar in range(bars):
        base = bar * 4 * spb
        # LH rolling tenths (root-3rd-5th-3rd)
        lh_pattern = [36, 48, 52, 48]  # C2-E3-G3-E3
        for e in range(4):
            notes.append(_midi_note(base + e * spb, lh_pattern[e], 0.6))
        # RH sustained melody with grace notes
        melody = [72, 74, 76, 77]  # C5-D5-E5-F5
        for k in range(4):
            notes.append(_midi_note(base + k * spb, melody[(bar + k) % 4], 1.2))
            if k % 2 == 0:  # grace note before melody
                notes.append(_midi_note(base + k * spb + 0.05, melody[(bar + k) % 4] - 2, 0.1))
    return _voice_texture(notes, bars, spb)


def _block_chords_arrangement(bars=4, spb=0.5):
    """Block chords: both hands play chords together, homophonic texture."""
    notes = []
    for bar in range(bars):
        base = bar * 4 * spb
        # LH chord (root position)
        lh_chords = [[36, 43, 48], [41, 48, 52], [43, 50, 55], [36, 43, 48]]
        # RH chord (inverted)
        rh_chords = [[72, 76, 79], [74, 79, 83], [76, 81, 84], [72, 76, 79]]
        for k in range(4):
            lh = lh_chords[k]
            rh = rh_chords[k]
            for m in lh:
                notes.append(_midi_note(base + k * spb, m, 0.4))
            for m in rh:
                notes.append(_midi_note(base + k * spb, m, 0.4))
    return _voice_texture(notes, bars, spb)


def _solo_hand_runs_arrangement(bars=4, spb=0.5):
    """Solo hand runs: RH fast runs, LH sparse accompaniment."""
    notes = []
    for bar in range(bars):
        base = bar * 4 * spb
        # LH sparse: single bass notes on downbeats
        for k in range(4):
            notes.append(_midi_note(base + k * spb, 36 + (k * 5) % 12, 0.5))
        # RH fast runs: 16th notes
        run_notes = [60, 62, 64, 65, 67, 69, 71, 72] * 2
        for e in range(16):
            notes.append(_midi_note(base + e * spb / 4, run_notes[e % len(run_notes)], 0.15))
    return _voice_texture(notes, bars, spb)


def _arpeggio_arrangement(bars=4, spb=0.5):
    """Two-hand arpeggios: LH up, RH down, meeting in middle."""
    notes = []
    for bar in range(bars):
        base = bar * 4 * spb
        # LH ascending arpeggio
        lh_arp = [36, 43, 48, 52, 55, 60]
        # RH descending arpeggio
        rh_arp = [84, 79, 76, 72, 67, 64]
        for e in range(6):
            notes.append(_midi_note(base + e * spb / 2, lh_arp[e], 0.3))
            notes.append(_midi_note(base + e * spb / 2, rh_arp[e], 0.3))
    return _voice_texture(notes, bars, spb)


FIXTURES = {
    "two_hand": _two_hand_keys_arrangement(),
    "alberti_bass": _alberti_bass_arrangement(),
    "stride": _stride_arrangement(),
    "crossed_hands": _crossed_hand_arrangement(),
    "ballad": _ballad_arrangement(),
    "block_chords": _block_chords_arrangement(),
    "solo_hand_runs": _solo_hand_runs_arrangement(),
    "arpeggio": _arpeggio_arrangement(),
}


def _note_midi_keys(n):
    return n["s"] * 24 + n["f"]


def _generate_with_constants(arr, hand_split, melody_bonus, n_levels=4):
    """Generate phrases with overridden constants."""
    # Temporarily override module constants
    old_split = scoring._KEYS_HAND_SPLIT_SEMITONES
    old_melody = scoring._KEYS_MELODY_LINE_BONUS
    scoring._KEYS_HAND_SPLIT_SEMITONES = hand_split
    scoring._KEYS_MELODY_LINE_BONUS = melody_bonus
    try:
        phrases = scoring.generate_phrases_for_arrangement(
            arr, n_levels=n_levels,
            section_times=[i * 4 * arr[1]["spb"] if "spb" in arr[1] else i * 2.0 for i in range(4)]
        )
        return phrases
    finally:
        scoring._KEYS_HAND_SPLIT_SEMITONES = old_split
        scoring._KEYS_MELODY_LINE_BONUS = old_melody


def _group_and_score(arr_dict):
    """Group and score arrangement, returning groups and tempo."""
    beat_times = [b["time"] for b in arr_dict["beats"]]
    tempo = scoring._TempoParams.from_beats(beat_times, arr_dict["beats"])
    groups = scoring._group_notes_keys(arr_dict["notes"], arr_dict["chords"])
    scoring._score_groups_keys(groups, beat_times, tempo=tempo)
    return groups, beat_times, tempo


def _assign_tiers(groups, beat_times, tempo, n_levels=4):
    thresholds = scoring._tier_thresholds(
        [g["retention_score"] for g in groups], n_levels,
    )
    scoring._assign_tiers(groups, n_levels, thresholds, beat_times, tempo=tempo)
    return groups


def _materialize_tiers(groups, n_levels=4):
    """Materialize tier notes from grouped levels."""
    tiers = []
    for level in range(n_levels):
        reduced, _ = scoring._notes_for_level_keys(groups, level, max_level=n_levels - 1)
        tiers.append({(_note_midi_keys(n), n["t"]) for n in reduced})
    return tiers


def measure_fixture(arr_dict, hand_split, melody_bonus, n_levels=4):
    """Measure all metrics for one fixture with given constants."""
    # Temporarily override constants
    old_split = scoring._KEYS_HAND_SPLIT_SEMITONES
    old_melody = scoring._KEYS_MELODY_LINE_BONUS
    scoring._KEYS_HAND_SPLIT_SEMITONES = hand_split
    scoring._KEYS_MELODY_LINE_BONUS = melody_bonus

    try:
        # Group and score
        beat_times = [b["time"] for b in arr_dict["beats"]]
        tempo = scoring._TempoParams.from_beats(beat_times, arr_dict["beats"])
        groups = scoring._group_notes_keys(arr_dict["notes"], arr_dict["chords"])
        scoring._score_groups_keys(groups, beat_times, tempo=tempo)

        # Count onsets split
        onset_groups = {}
        for i, g in enumerate(groups):
            onset_groups.setdefault(g["time"], []).append(i)
        total_onsets = len(onset_groups)
        split_onsets = sum(1 for idxs in onset_groups.values() if len(idxs) > 1)
        fraction_split = split_onsets / total_onsets if total_onsets > 0 else 0.0

        # Check melody flag on split onsets
        melody_on_split = 0
        split_with_melody = 0
        for idxs in onset_groups.values():
            if len(idxs) > 1:
                for i in idxs:
                    if groups[i].get("melody"):
                        melody_on_split += 1
                        split_with_melody += 1
                        break

        # Assign tiers
        groups = _assign_tiers(deepcopy(groups), beat_times, tempo, n_levels)

        # Materialize tiers
        tiers = _materialize_tiers(groups, n_levels)

        # Check tier-0 melody presence (highest pitch in tier 0 >= middle C)
        tier0_notes = tiers[0]
        tier0_has_melody = any(midi >= 60 for midi, _ in tier0_notes) if tier0_notes else False

        # Check nesting violations
        nesting_violations = 0
        for lower, higher in zip(tiers[:-1], tiers[1:]):
            if not lower <= higher:
                nesting_violations += 1

        # Check collapsed tiers (identical consecutive tiers)
        collapsed_tiers = 0
        for lower, higher in zip(tiers[:-1], tiers[1:]):
            if lower == higher:
                collapsed_tiers += 1

        # Count total tiers with content
        non_empty_tiers = sum(1 for t in tiers if t)

        return {
            "fraction_onsets_split": fraction_split,
            "split_onsets": split_onsets,
            "total_onsets": total_onsets,
            "melody_on_split": melody_on_split,
            "tier0_has_melody": tier0_has_melody,
            "nesting_violations": nesting_violations,
            "collapsed_tiers": collapsed_tiers,
            "non_empty_tiers": non_empty_tiers,
            "total_tiers": n_levels,
        }
    finally:
        scoring._KEYS_HAND_SPLIT_SEMITONES = old_split
        scoring._KEYS_MELODY_LINE_BONUS = old_melody


def sweep_constants():
    """Sweep both constants across ranges and collect metrics."""
    hand_split_values = [8, 9, 10, 11, 12, 13, 14]
    melody_bonus_values = [0.04, 0.06, 0.08, 0.10, 0.12]

    results = {}

    for fixture_name, (arr_dict, spb) in FIXTURES.items():
        results[fixture_name] = {}
        for hand_split, melody_bonus in product(hand_split_values, melody_bonus_values):
            metrics = measure_fixture(arr_dict, hand_split, melody_bonus)
            results[fixture_name][(hand_split, melody_bonus)] = metrics

    return results


def print_results(results):
    """Print results in a readable format."""
    hand_split_values = [8, 9, 10, 11, 12, 13, 14]
    melody_bonus_values = [0.04, 0.06, 0.08, 0.10, 0.12]

    for fixture_name in FIXTURES:
        print(f"\n{'='*80}")
        print(f"FIXTURE: {fixture_name}")
        print(f"{'='*80}")
        print(f"{'hand_split':>10} {'melody_bonus':>12} {'split%':>8} {'tier0_mel':>10} {'nesting':>9} {'collapsed':>10} {'non_empty':>9}")
        print("-" * 80)

        for hand_split in hand_split_values:
            for melody_bonus in melody_bonus_values:
                m = results[fixture_name][(hand_split, melody_bonus)]
                print(f"{hand_split:>10} {melody_bonus:>12.2f} "
                      f"{m['fraction_onsets_split']*100:>7.1f}% "
                      f"{'YES' if m['tier0_has_melody'] else 'NO':>10} "
                      f"{m['nesting_violations']:>9} "
                      f"{m['collapsed_tiers']:>10} "
                      f"{m['non_empty_tiers']:>9}/{m['total_tiers']}")


def find_best_constants(results):
    """Find constants that optimize metrics across all fixtures."""
    hand_split_values = [8, 9, 10, 11, 12, 13, 14]
    melody_bonus_values = [0.04, 0.06, 0.08, 0.10, 0.12]

    best_score = -1
    best_params = None

    for hand_split, melody_bonus in product(hand_split_values, melody_bonus_values):
        total_score = 0
        for fixture_name in FIXTURES:
            m = results[fixture_name][(hand_split, melody_bonus)]
            # Score: prefer tier-0 melody, no nesting violations, no collapsed tiers,
            # reasonable split fraction (not too high, not too low), all tiers non-empty
            score = 0
            if m["tier0_has_melody"]:
                score += 10
            score -= m["nesting_violations"] * 20
            score -= m["collapsed_tiers"] * 20
            # Prefer split fraction around 20-40% for two-hand textures
            split_pct = m["fraction_onsets_split"] * 100
            if 15 <= split_pct <= 50:
                score += 5
            elif split_pct > 50:
                score -= 5
            # All tiers should be non-empty
            if m["non_empty_tiers"] == m["total_tiers"]:
                score += 5
            total_score += score

        if total_score > best_score:
            best_score = total_score
            best_params = (hand_split, melody_bonus)

    return best_params, best_score


def main():
    print("Calibrating keys constants (issue #176)")
    print(f"Fixtures: {list(FIXTURES.keys())}")
    print(f"Hand split range: 8-14 semitones")
    print(f"Melody bonus range: 0.04-0.12")

    results = sweep_constants()
    print_results(results)

    best_params, best_score = find_best_constants(results)
    print(f"\n{'='*80}")
    print(f"RECOMMENDED: hand_split={best_params[0]}, melody_bonus={best_params[1]:.2f} (score={best_score})")
    print(f"{'='*80}")

    # Also test current defaults
    print("\nCURRENT DEFAULTS: hand_split=10, melody_bonus=0.08")
    for fixture_name in FIXTURES:
        m = results[fixture_name][(10, 0.08)]
        print(f"  {fixture_name}: split={m['fraction_onsets_split']*100:.1f}%, "
              f"tier0_mel={'YES' if m['tier0_has_melody'] else 'NO'}, "
              f"nesting={m['nesting_violations']}, collapsed={m['collapsed_tiers']}, "
              f"non_empty={m['non_empty_tiers']}/{m['total_tiers']}")

    # Save results to JSON
    with open("/workspace/app/tests/calibration_results.json", "w") as f:
        json_results = {}
        for fixture, data in results.items():
            json_results[fixture] = {}
            for (hs, mb), m in data.items():
                json_results[fixture][f"hs{hs}_mb{mb:.2f}"] = m
        json.dump(json_results, f, indent=2)
    print("\nResults saved to tests/calibration_results.json")


if __name__ == "__main__":
    main()
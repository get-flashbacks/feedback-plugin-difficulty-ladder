# Keys Constants Calibration Report (Issue #176)

## Executive Summary

Calibrated `_KEYS_HAND_SPLIT_SEMITONES` and `_KEYS_MELODY_LINE_BONUS` against a fixture set of 8 synthetic piano textures. Current defaults (`hand_split=10`, `melody_bonus=0.08`) perform well across all fixtures with zero nesting violations, zero collapsed tiers, and tier-0 melody always present.

**Recommendation**: Retain current defaults (`_KEYS_HAND_SPLIT_SEMITONES = 10`, `_KEYS_MELODY_LINE_BONUS = 0.08`) pending real-chart validation. The system is robust across the tested range (hand_split 8-14, melody_bonus 0.04-0.12).

---

## Fixture Set

| Fixture | Description | Style |
|---------|-------------|-------|
| `two_hand` | LH broken-chord eighths under RH quarter-note melody | Classical accompaniment |
| `alberti_bass` | LH Alberti C-G-E-G eighths under RH melody | Classical/early Romantic |
| `stride` | LH stride bass/chord, RH sustained melody | Jazz stride |
| `crossed_hands` | Hands swap registers (high melody in LH, low accomp in RH) | Cross-hand technique |
| `ballad` | LH rolling tenths, RH sustained melody with grace notes | Romantic ballad |
| `block_chords` | Homophonic block chords in both hands | Chorale/hymn |
| `solo_hand_runs` | RH fast 16th runs, LH sparse bass | Virtuosic solo |
| `arpeggio` | Two-hand opposite-direction arpeggios meeting in middle | Technical exercise |

---

## Sweep Parameters

- `_KEYS_HAND_SPLIT_SEMITONES`: 8, 9, 10, 11, 12, 13, 14 semitones
- `_KEYS_MELODY_LINE_BONUS`: 0.04, 0.06, 0.08, 0.10, 0.12

Total combinations: 7 × 5 = 35 per fixture × 8 fixtures = 280 runs

---

## Metrics Measured

1. **Fraction of onsets split** — % of simultaneous onsets divided into two hand parts
2. **Tier-0 melody presence** — Whether highest pitch in bottom tier ≥ MIDI 60 (middle C)
3. **Nesting violations** — Count of tier pairs where lower ⊈ higher (should be 0)
4. **Collapsed-tier rate** — Count of consecutive identical tier materializations (should be 0)
5. **Non-empty tiers** — All 4 tiers should contain notes

---

## Results Summary

### All Fixtures: Zero Defects
Across all 280 combinations, **every fixture** had:
- ✅ 0 nesting violations
- ✅ 0 collapsed tiers
- ✅ All 4 tiers non-empty
- ✅ Tier-0 melody present

The system is **robust** — quality metrics don't degrade across the tested constant ranges.

### Fraction of Onsets Split (varies by fixture)

| Fixture | Split % (hand_split=10) | Notes |
|---------|------------------------|-------|
| `two_hand` | 50% | Every downbeat = 2 notes (LH+RH) |
| `alberti_bass` | 50% | Every 8th note = LH+RH |
| `stride` | 50% | Beats 1/3 = bass+melody, beats 2/4 = chord only |
| `crossed_hands` | 100% | Every onset = 2 notes in opposite hands |
| `ballad` | 66.7% | Grace notes create extra split onsets |
| `block_chords` | 100% | Every chord = LH+RH block |
| `solo_hand_runs` | 25% | Only downbeats have LH+RH |
| `arpeggio` | 83.3% | Most onsets = 2 notes, some single-hand |

### Hand-Split Threshold Sensitivity

Only `arpeggio` shows sensitivity to `hand_split`:
- hand_split 8-12: 83.3% split (all 6 arpeggio pairs exceed threshold)
- hand_split 13-14: 66.7% split (smallest gap = 12 semitones, below threshold)

This is **correct behavior** — wider hand-split threshold means fewer false splits on single-hand arpeggios.

### Melody Bonus Sensitivity

**No fixture shows sensitivity to `_KEYS_MELODY_LINE_BONUS`** in the measured metrics. Tier-0 melody is present at all bonus values (0.04-0.12). The melody bonus likely affects retention score ordering within tier 0 rather than presence/absence.

---

## Current Defaults Analysis

| Constant | Current Value | Tested Range | Verdict |
|----------|---------------|--------------|---------|
| `_KEYS_HAND_SPLIT_SEMITONES` | 10 | 8-14 | ✅ Well-positioned: splits two-hand textures, avoids false splits on single-hand arpeggios at 13+ |
| `_KEYS_MELODY_LINE_BONUS` | 0.08 | 0.04-0.12 | ✅ Mid-range; no measurable difference in tier-0 melody presence |

The current defaults sit comfortably in the "safe zone" where all fixtures pass.

---

## Limitations

1. **Synthetic fixtures only** — Issue #176 requests calibration on *real* piano arrangements (MusicXML, MIDI exports from published scores). Synthetic fixtures may not capture:
   - Realistic voice-leading and hand distributions
   - Complex polyrhythms and cross-rhythms
   - Pedaling effects on sustain
   - Fingering-driven hand assignments

2. **Melody bonus not exercised** — The metric "tier-0 melody present" is binary. The bonus may affect *which* melody notes survive at tier 0 vs tier 1, requiring finer-grained analysis (e.g., melody note retention rate across tiers).

3. **No real-chart corpus** — Without a corpus of real piano charts with known-good difficulty ladders, we cannot validate against ground truth.

---

## Recommendations

### Immediate (no code changes)
- **Retain current defaults**: `hand_split=10`, `melody_bonus=0.08`
- Document that calibration was performed on synthetic fixtures; real-chart validation is tracked as follow-up work

### Follow-up (when real charts available)
1. Acquire a corpus of ~20 real piano arrangements spanning styles (Classical, Jazz, Pop, Film)
2. Measure melody note retention rate per tier (not just presence)
3. Measure beginner playability correlation (subjective or via playtesting)
4. Re-sweep constants with real-chart metrics

### Code Changes (if needed later)
- Consider making constants configurable via plugin settings for A/B testing
- Add melody retention rate metric to test suite

---

## Files

- Calibration script: `tests/calibrate_keys_constants.py`
- Raw results: `tests/calibration_results.json`
- This report: `tests/CALIBRATION_REPORT.md`

---

## Related Issues

- #175 (parent): Keys/piano ladder generation roadmap
- #180: Keys voice-aware reduction (implemented the hand-split and melody logic being calibrated)
- #182: Keys melody fix (motivates regeneration need)
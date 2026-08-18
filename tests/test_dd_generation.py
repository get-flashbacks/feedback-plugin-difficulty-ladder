"""Tests for the fretted-instrument difficulty ladder generator in routes.py.

Self-contained sys.path bootstrap (this plugin has no pyproject.toml / shared
conftest of its own) so `pytest tests/` works from this directory directly.
"""
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_CORE_LIB = _PLUGIN_DIR.parent / "feedBack" / "lib"
for p in (_PLUGIN_DIR, _CORE_LIB):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import routes  # noqa: E402


def _arrangement(notes, chords=None, sections=None, n_beats=40):
    return {
        "type": "lead", "name": "lead",
        "notes": notes, "chords": chords or [],
        "beats": [{"time": i * 0.5} for i in range(n_beats)],
        "sections": sections or [],
        "tuning": [0] * 6,
    }


def _simple_notes(t0, t1, step=0.5, string=2, fret=3):
    notes = []
    t = t0
    while t < t1:
        notes.append({"t": round(t, 3), "s": string, "f": fret, "sus": 0})
        t += step
    return notes


def _technical_notes(t0, t1, step=0.1):
    import random
    rng = random.Random(7)
    notes = []
    t = t0
    while t < t1:
        n = {"t": round(t, 3), "s": rng.randint(0, 5), "f": rng.randint(1, 20), "sus": 0}
        if rng.random() < 0.3:
            n["bn"] = 1.0
        if rng.random() < 0.2:
            n["ho"] = True
        if rng.random() < 0.15:
            n["tr"] = True
        if rng.random() < 0.1:
            n["hm"] = True
        notes.append(n)
        t += step
    return notes


def test_returns_none_for_near_empty_arrangement():
    arr = _arrangement(_simple_notes(0, 1, step=0.5))  # well under MIN_EVENTS_FOR_GENERATION
    assert routes.generate_phrases_for_arrangement(arr, n_levels=4) is None


def test_simple_phrase_gets_a_shorter_ladder_than_the_cap():
    notes = _simple_notes(0, 10, step=0.5, fret=3)  # constant fret -> near-zero score spread
    arr = _arrangement(notes)
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=6)
    assert phrases, "expected at least one phrase"
    assert phrases[0]["max_difficulty"] < 5, (
        "a near-constant, single-string phrase should not consume the full ladder cap"
    )


def test_canonical_section_times_create_one_phrase_per_section_including_an_empty_arrangement_section():
    arr = _arrangement(_simple_notes(0, 2, step=0.2, fret=3))
    phrases = routes.generate_phrases_for_arrangement(
        arr, n_levels=4, section_times=[0, 2, 6]
    ) or []
    # section_times carries one entry per section — start times mirroring
    # highway.getSections() — not n+1 boundaries. Three entries therefore mean
    # three sections and must yield three phrases, so Section Map can index
    # phrases by section position. The last section starts after this
    # arrangement's content ends, so it collapses to the t0 + 0.001 floor
    # rather than being dropped.
    assert [(p["start_time"], p["end_time"]) for p in phrases] == [
        (0.0, 2.0), (2.0, 6.0), (6.0, 6.001)
    ]

    # Windows are half-open (t0 <= t < t1), so the note landing exactly on the
    # 2.0 boundary belongs to the second section, not the first.
    assert phrases[1]["levels"][-1]["notes"] == [{"t": 2.0, "s": 2, "f": 3, "sus": 0}]

    # The trailing section has no chart content in this arrangement at all.
    # It is retained regardless — that empty phrase is what keeps the
    # one-phrase-per-section contract intact.
    assert phrases[2]["max_difficulty"] == 0
    assert phrases[2]["levels"][0]["notes"] == []


def test_dense_technical_phrase_uses_more_of_the_cap_than_a_simple_one():
    simple = _arrangement(_simple_notes(0, 10, step=0.5, fret=3))
    technical = _arrangement(_technical_notes(0, 10, step=0.1))

    simple_phrases = routes.generate_phrases_for_arrangement(simple, n_levels=6)
    technical_phrases = routes.generate_phrases_for_arrangement(technical, n_levels=6)

    assert simple_phrases and technical_phrases
    assert technical_phrases[0]["max_difficulty"] > simple_phrases[0]["max_difficulty"]


def test_bottom_tier_is_sparser_than_a_flat_percentile_split():
    arr = _arrangement(_technical_notes(0, 12, step=0.1))
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases
    levels = phrases[0]["levels"]
    max_level = phrases[0]["max_difficulty"]
    top_count = len(levels[max_level]["notes"]) + len(levels[max_level]["chords"])
    bottom_count = len(levels[0]["notes"]) + len(levels[0]["chords"])
    assert top_count > 0
    # a flat percentile split would put ~1/n_levels of the content at the
    # bottom tier; the convex retention curve should land well under that
    assert bottom_count / top_count < 1.0 / (max_level + 1)


def test_flashy_techniques_are_gated_out_of_low_tiers():
    arr = _arrangement(_technical_notes(0, 12, step=0.1))
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=6)
    assert phrases
    levels = phrases[0]["levels"]
    bottom_notes = levels[0]["notes"]
    assert not any(n.get("tr") or n.get("hm") for n in bottom_notes), (
        "tremolo/harmonic should not survive into the bottom tier of a technical phrase"
    )


def test_chords_are_thinned_below_the_top_tier_and_intact_at_the_top():
    chord = {"t": 2.05, "notes": [
        {"s": 5, "f": 0}, {"s": 4, "f": 2}, {"s": 3, "f": 2},
        {"s": 2, "f": 1}, {"s": 1, "f": 0}, {"s": 0, "f": 0},
    ]}
    # plenty of simple filler so the chord isn't one of only ~2 groups
    # (with too few groups, percentile bucketing is degenerate/arbitrary)
    arr = _arrangement(_simple_notes(0, 8, step=0.1), chords=[chord])
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases
    levels = phrases[0]["levels"]
    max_level = phrases[0]["max_difficulty"]

    def chord_note_count_at(lvl):
        return sum(
            1 for n in levels[lvl]["notes"] if abs(float(n.get("t", -1)) - chord["t"]) < 1e-6
        ) + sum(
            len(c.get("notes", [])) for c in levels[lvl]["chords"]
            if abs(float(c.get("t", -1)) - chord["t"]) < 1e-6
        )

    top_count = chord_note_count_at(max_level)
    assert top_count == 6, "top tier should keep the full chord intact"

    # first level (if any) below the top tier where the chord group appears
    # at all should be a partial voicing, not the full 6-note chord
    for lvl in range(max_level):
        count = chord_note_count_at(lvl)
        if count > 0:
            assert count < 6, (
                f"chord should be thinned to a partial voicing at level {lvl}, "
                f"not kept whole below the top tier"
            )
            break


def test_lower_tier_refinement_promotes_a_beat_anchor_and_continuity_bridge():
    groups = [
        {"time": 0.1, "score": 0.1, "level": 0, "notes": [{"s": 5, "f": 2}]},
        {"time": 0.5, "score": 0.3, "level": 2, "notes": [{"s": 5, "f": 7}]},
        {"time": 1.0, "score": 0.2, "level": 0, "notes": [{"s": 5, "f": 12}]},
    ]

    routes._refine_lower_tier_path(groups, [0.0, 0.5, 1.0], max_level=2)

    assert groups[1]["level"] == 0, (
        "the omitted beat-aligned group should bridge the otherwise 10-fret lower-tier jump"
    )


def test_lower_tier_refinement_falls_back_to_a_beat_group_when_none_was_kept():
    groups = [
        {"time": 0.1, "score": 0.1, "level": 0, "notes": [{"s": 5, "f": 3}]},
        {"time": 0.5, "score": 0.2, "level": 2, "notes": [{"s": 5, "f": 4}]},
    ]

    routes._refine_lower_tier_path(groups, [0.0, 0.5], max_level=2)

    assert groups[1]["level"] == 0


def test_bottom_arpeggio_voice_preserves_the_root_string():
    groups = [{
        "type": "arpeggio", "level": 0, "time": 0.0, "chord": None,
        "notes": [{"t": 0.0, "s": 1, "f": 7}, {"t": 0.04, "s": 5, "f": 3}],
    }]

    notes, chords = routes._notes_for_level(groups, level=0, max_level=2)

    assert chords == []
    assert [(n["s"], n["f"]) for n in notes] == [(5, 3)]


def test_bottom_arpeggio_voice_preserves_an_open_root_string():
    # The root string (s=5) is played open here. Bottom-tier arpeggio
    # selection cares about the harmonic root, not hand position — an open
    # root is a valid, easier simplification, so it must not be skipped in
    # favor of the fretted note the way the jump-scoring anchor now is.
    groups = [{
        "type": "arpeggio", "level": 0, "time": 0.0, "chord": None,
        "notes": [{"t": 0.0, "s": 5, "f": 0}, {"t": 0.04, "s": 2, "f": 5}],
    }]

    notes, chords = routes._notes_for_level(groups, level=0, max_level=2)

    assert chords == []
    assert [(n["s"], n["f"]) for n in notes] == [(5, 0)]


def test_fret_jump_penalty_ignores_groups_separated_by_a_long_rest():
    def groups(second_time):
        return [
            {"time": 0.0, "notes": [{"s": 5, "f": 2, "sus": 0}]},
            {"time": second_time, "notes": [{"s": 5, "f": 15, "sus": 0}]},
        ]

    nearby = groups(0.5)
    after_rest = groups(2.0)
    routes._score_groups(nearby, n_strings=6)
    routes._score_groups(after_rest, n_strings=6)

    assert nearby[1]["score"] > after_rest[1]["score"]
    assert abs(nearby[1]["score"] - after_rest[1]["score"] - 0.18) < 1e-9


def test_group_anchor_note_prefers_a_fretted_note_over_an_incidental_open_string():
    # An open string needs no hand position at all, so it must not be picked
    # as the hand-position anchor when the group also has fretted notes —
    # even though it's the highest string index (the usual root convention).
    group = {"notes": [
        {"s": 0, "f": 12}, {"s": 1, "f": 12}, {"s": 2, "f": 13},
        {"s": 3, "f": 13}, {"s": 4, "f": 12}, {"s": 5, "f": 0},
    ]}
    anchor = routes._group_anchor_note(group)
    assert anchor["f"] > 0

    # All-open group: falls back to the highest-string-index note as before.
    open_group = {"notes": [{"s": 5, "f": 0}, {"s": 4, "f": 0}]}
    assert routes._group_anchor_note(open_group) == {"s": 5, "f": 0}


def test_fret_jump_penalty_reflects_the_true_fretted_position_not_an_incidental_open_string():
    # group1 is IDENTICAL in both scenarios (so its own fretting/technique/
    # density terms don't change); only group0's note count-preserving fret
    # value changes, isolating the jump-bonus term exactly like the
    # long-rest test above. group1's anchor string (s=5) is played open, but
    # its true hand position is fret 13 (on s=0).
    def groups(prev_fret):
        return [
            {"time": 0.0, "notes": [{"s": 0, "f": prev_fret, "sus": 0}]},
            {"time": 0.4, "notes": [{"s": 0, "f": 13, "sus": 0}, {"s": 5, "f": 0, "sus": 0}]},
        ]

    close_position = groups(12)   # true jump 13->12 = 1, below the penalty threshold
    far_position = groups(2)      # true jump 13->2 = 11, should trigger the penalty
    routes._score_groups(close_position, n_strings=6)
    routes._score_groups(far_position, n_strings=6)

    assert far_position[1]["score"] > close_position[1]["score"], (
        "a real large hand-position jump must still be penalized even when "
        "the anchor string happens to be open in the current group"
    )
    assert abs(far_position[1]["score"] - close_position[1]["score"] - 0.18) < 1e-9, (
        "an incidental open string on the anchor string must not itself "
        "read as a hand-position jump — the bonus must track the true "
        "fretted position (s=0), not the coincidentally-open anchor string"
    )


def test_lower_tier_refinement_does_not_insert_a_needless_bridge_for_an_open_anchor():
    groups = [
        {"time": 0.0, "score": 0.1, "level": 0, "notes": [{"s": 0, "f": 12}]},
        # Would look like a plausible bridge under the old (buggy) jump
        # computation, but nothing here actually needs bridging.
        {"time": 0.2, "score": 0.5, "level": 2, "notes": [{"s": 0, "f": 6}]},
        {"time": 0.5, "score": 0.1, "level": 0, "notes": [
            {"s": 0, "f": 13}, {"s": 5, "f": 0},
        ]},
    ]
    routes._refine_lower_tier_path(groups, [], max_level=2)
    assert groups[1]["level"] == 2


def test_lower_tier_refinement_still_bridges_a_genuine_fretted_anchor_jump():
    # Same shape as the open-anchor case above, but every note is fretted:
    # the fretted-note preference in _group_anchor_note must not suppress
    # bridging for a real, large hand-position jump.
    groups = [
        {"time": 0.0, "score": 0.1, "level": 0, "notes": [{"s": 0, "f": 2}]},
        {"time": 0.2, "score": 0.5, "level": 2, "notes": [{"s": 0, "f": 8}]},
        {"time": 0.5, "score": 0.1, "level": 0, "notes": [{"s": 0, "f": 15}]},
    ]
    routes._refine_lower_tier_path(groups, [], max_level=2)
    assert groups[1]["level"] == 0, (
        "a genuine fret-2-to-fret-15 jump should still get bridged"
    )


def test_unsupported_drums_skip_preserves_instrument_classification():
    class _Lock:
        def __enter__(self) -> "_Lock":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    with patch.object(routes, "_lock_for_pack", return_value=_Lock()), patch.object(
        routes,
        "_load_manifest_and_arrangement",
        return_value=(None, None, "unsupported-instrument-drums"),
    ):
        result = routes._generate_one(Path("unused"), 0, n_levels=4, force=False, log=None)

    assert result == {
        "ok": True,
        "skipped": "unsupported-instrument-drums",
        "arrangement_index": 0,
        "instrument": "drums",
    }


def test_generate_song_processes_every_arrangement_and_keeps_going_after_a_bad_one():
    manifest = {"arrangements": [{}, {}, {}]}
    results = [
        {"ok": True, "arrangement_index": 0, "instrument": "fretted"},
        {"ok": True, "arrangement_index": 1, "skipped": "unsupported-instrument-drums", "instrument": "drums"},
        routes.HTTPException(400, "arrangement has no backing file"),
    ]
    with patch.object(routes.sloppak, "load_manifest", return_value=manifest), patch.object(
        routes, "_generate_one", side_effect=results
    ):
        summary = routes._generate_song(Path("unused"), n_levels=4, force=False, log=None)

    assert summary["generated"] == 1
    assert summary["skipped"] == 2
    assert summary["failed"] == 1
    assert summary["arrangements"][1]["instrument"] == "drums"
    assert summary["arrangements"][2]["error"] == "arrangement has no backing file"


def test_lower_tier_refinement_does_not_bridge_a_repositioning_rest():
    groups = [
        {"time": 0.0, "score": 0.1, "level": 0, "notes": [{"s": 5, "f": 2}]},
        {"time": 1.0, "score": 0.2, "level": 2, "notes": [{"s": 5, "f": 7}]},
        {"time": 2.0, "score": 0.1, "level": 0, "notes": [{"s": 5, "f": 12}]},
    ]

    routes._refine_lower_tier_path(groups, [], max_level=2)

    assert groups[1]["level"] == 2


# ── Item 1: tempo-relative thresholds ────────────────────────────────────────

def test_median_beat_interval_returns_none_for_too_few_beats():
    assert routes._median_beat_interval([i * 0.5 for i in range(7)]) is None


def test_median_beat_interval_returns_none_for_out_of_band_spacing():
    # Sub-24ms spacing is far outside the ~24-400bpm sanity band — treat it
    # as corrupt/duplicate beat data, not a real (absurdly fast) tempo.
    assert routes._median_beat_interval([i * 0.001 for i in range(20)]) is None


def test_median_beat_interval_resists_a_single_outlier():
    times = [round(i * 0.5, 6) for i in range(20)]
    times[-1] = times[-2] + 3.0  # one dropped-click-sized outlier
    median = routes._median_beat_interval(times)
    assert median is not None and abs(median - 0.5) < 1e-9


def test_tempo_params_from_beats_derives_all_fields_from_a_clean_click_track():
    tempo = routes._TempoParams.from_beats([i * 0.5 for i in range(20)])
    assert abs(tempo.beat_interval - 0.5) < 1e-9
    assert abs(tempo.time_window_ms - 125.0) < 1e-9
    assert abs(tempo.beat_tolerance - 0.06) < 1e-9
    assert abs(tempo.fret_jump_window_seconds - 1.0) < 1e-9
    assert abs(tempo.sustain_ease_norm_seconds - 2.0) < 1e-9


def test_tempo_params_from_beats_falls_back_to_absolute_defaults_without_enough_data():
    tempo = routes._TempoParams.from_beats([0.0, 0.5, 1.0])  # well under the 8-beat floor
    assert tempo.beat_interval is None
    assert tempo == routes._TempoParams()


def test_group_notes_time_window_is_tempo_configurable():
    # Same 100ms gap between two different-string notes: should NOT cluster
    # under a tight (fast-tempo-derived) window, but SHOULD cluster under a
    # loose (slow-tempo-derived) window — this is the exact mechanism
    # generate_phrases_for_arrangement now drives from the song's own beats.
    notes = [
        {"t": 0.0, "s": 0, "f": 3},
        {"t": 0.1, "s": 1, "f": 3},
    ]
    tight = routes._group_notes(notes, [], time_window_ms=62.5)
    loose = routes._group_notes(notes, [], time_window_ms=250.0)
    assert [g["type"] for g in tight] == ["note", "note"]
    assert [g["type"] for g in loose] == ["arpeggio"]


# ── Item 2: measure-aligned fallback phrase windows ──────────────────────────

def _measure_beats(n_measures, beats_per_measure=4, step=0.5):
    beats = []
    t = 0.0
    for m in range(1, n_measures + 1):
        for b in range(beats_per_measure):
            beats.append({"time": round(t, 3), "measure": m if b == 0 else -1})
            t += step
    return beats


def test_measure_aligned_windows_returns_none_below_min_downbeats():
    beats = [{"time": 0.0, "measure": 1}, {"time": 2.0, "measure": 2}]  # only 2 downbeats
    assert routes._measure_aligned_windows(beats, duration=10.0) is None


def test_measure_aligned_windows_groups_every_n_downbeats():
    beats = [{"time": float(i * 2), "measure": i + 1} for i in range(10)]  # 10 downbeats, 2s apart
    windows = routes._measure_aligned_windows(beats, duration=25.0, measures_per_phrase=4)
    assert windows == [(0.0, 8.0), (8.0, 16.0), (16.0, 25.0)]


def test_measure_aligned_windows_treats_measure_zero_as_a_valid_downbeat():
    # feedBack's own runtime convention (static/highway.js's `isMeasure =
    # beat.measure >= 0`, static/js/count-in.js, plugins/highway_3d) is
    # that ANY non-negative measure value is a downbeat, not just measure
    # > 0 -- only -1 means "not a downbeat." A song numbered 0-based must
    # produce the exact same windows as the 1-based fixture above, not
    # silently lose its first downbeat.
    beats = [{"time": float(i * 2), "measure": i} for i in range(10)]  # measures 0..9, 2s apart
    windows = routes._measure_aligned_windows(beats, duration=25.0, measures_per_phrase=4)
    assert windows == [(0.0, 8.0), (8.0, 16.0), (16.0, 25.0)]


def test_measure_aligned_fallback_groups_every_8_measures_when_no_sections():
    beats = _measure_beats(n_measures=16, beats_per_measure=4, step=0.5)
    arr = {
        "type": "lead", "name": "lead",
        "notes": _simple_notes(0, 30, step=0.5, fret=3),
        "chords": [], "beats": beats, "sections": [], "tuning": [0] * 6,
    }
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases
    # 16 measures at 8-per-phrase should split at the 9th measure's downbeat
    # (t=16.0), not the blind 30s chunker's single (0, ~29.5) window this
    # song's duration would otherwise produce.
    assert len(phrases) == 2
    assert phrases[0]["start_time"] == 0.0
    assert phrases[0]["end_time"] == 16.0
    assert phrases[1]["start_time"] == 16.0


def test_measure_aligned_fallback_is_skipped_when_beats_carry_no_downbeats():
    beats = [{"time": round(i * 0.5, 3), "measure": -1} for i in range(40)]
    arr = {
        "type": "lead", "name": "lead",
        "notes": _simple_notes(0, 40, step=0.5, fret=3),
        "chords": [], "beats": beats, "sections": [], "tuning": [0] * 6,
    }
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases
    # No usable downbeats (sub-beat-only data) -> falls through to the
    # legacy 30s chunker unchanged.
    assert [(p["start_time"], p["end_time"]) for p in phrases] == [(0.0, 30.0), (30.0, 39.5)]


# ── Item 3: syncopation-aware density scoring ────────────────────────────────

def test_syncopation_score_zero_on_beat_max_between_beats():
    beat_times = [0.0, 0.5, 1.0]
    assert routes._syncopation_score(0.5, beat_times, beat_interval=0.5) == 0.0
    assert routes._syncopation_score(0.75, beat_times, beat_interval=0.5) == 1.0
    assert routes._syncopation_score(0.5, [], beat_interval=0.5) == 0.0
    assert routes._syncopation_score(0.5, beat_times, beat_interval=None) == 0.0


def test_syncopation_term_scores_a_more_off_beat_group_higher():
    # Neither onset lands within _is_beat_aligned's tolerance, so the
    # existing beat-alignment discount doesn't fire for either — isolating
    # the syncopation contribution to the density sub-score specifically.
    beat_times = [0.0, 0.5, 1.0, 1.5]
    near_beat = [{"time": 0.6, "notes": [{"s": 2, "f": 3, "sus": 0}]}]
    far_from_beat = [{"time": 0.75, "notes": [{"s": 2, "f": 3, "sus": 0}]}]
    tempo = routes._TempoParams(beat_interval=0.5)
    routes._score_groups(near_beat, n_strings=6, beat_times=beat_times, tempo=tempo)
    routes._score_groups(far_from_beat, n_strings=6, beat_times=beat_times, tempo=tempo)
    assert far_from_beat[0]["score"] > near_beat[0]["score"], (
        "landing further from the beat grid (more syncopated) should score "
        "harder even with identical note/fret/technique content"
    )


# ── Item 4: string-skip / hand-shape difficulty ──────────────────────────────

def test_string_spread_increases_fretting_score():
    narrow = [{"time": 0.0, "notes": [{"s": 0, "f": 5, "sus": 0}, {"s": 1, "f": 5, "sus": 0}]}]
    wide = [{"time": 0.0, "notes": [{"s": 0, "f": 5, "sus": 0}, {"s": 5, "f": 5, "sus": 0}]}]
    routes._score_groups(narrow, n_strings=6)
    routes._score_groups(wide, n_strings=6)
    assert wide[0]["score"] > narrow[0]["score"], (
        "a wider string spread (1<->6) should score harder than an adjacent-"
        "string group, even though both groups touch 2 strings"
    )


def test_string_jump_bonus_isolated_from_fret_jump():
    def groups(prev_string):
        return [
            {"time": 0.0, "notes": [{"s": prev_string, "f": 5, "sus": 0}]},
            {"time": 0.4, "notes": [{"s": 5, "f": 5, "sus": 0}]},
        ]

    small_skip = groups(4)  # string jump 4->5 = 1, below the threshold (3)
    big_skip = groups(0)    # string jump 0->5 = 5, above the threshold

    routes._score_groups(small_skip, n_strings=6)
    routes._score_groups(big_skip, n_strings=6)

    assert big_skip[1]["score"] > small_skip[1]["score"]
    assert abs(big_skip[1]["score"] - small_skip[1]["score"] - 0.06) < 1e-9  # min(0.08, (5-3)*0.03)


def test_lower_tier_refinement_bridges_a_string_skip_even_when_the_fret_jump_is_small():
    groups = [
        {"time": 0.0, "score": 0.1, "level": 0, "notes": [{"s": 0, "f": 3}]},
        # Small fret movement (3->4->5, jump of 2 -- well under the fret-only
        # max_jump=7) but a full string skip (0->2->5) -- the old fret-only
        # trigger would never have looked here; the new string-jump trigger
        # (skip of 5, over max_string_jump=3) does.
        {"time": 0.2, "score": 0.5, "level": 2, "notes": [{"s": 2, "f": 4}]},
        {"time": 0.5, "score": 0.1, "level": 0, "notes": [{"s": 5, "f": 5}]},
    ]
    routes._refine_lower_tier_path(groups, [], max_level=2)
    assert groups[1]["level"] == 0, (
        "a hand-shape-changing string skip should get bridged even when the "
        "fret distance alone is small"
    )


def test_lower_tier_refinement_bridges_a_pure_string_skip_with_zero_fret_movement():
    # Regression for a real gap: _best_bridge_candidate's original acceptance
    # check only accepted a candidate that improved the FRET jump (worst_jump
    # < original_jump). When the fret jump is already 0 (identical fret on
    # both sides, only the string differs), no candidate could ever satisfy
    # worst_jump < 0 -- the string-jump trigger fired but bridging was
    # silently a no-op. Acceptance now also accepts a candidate that improves
    # the STRING jump instead.
    groups = [
        {"time": 0.0, "score": 0.1, "level": 0, "notes": [{"s": 0, "f": 5}]},
        {"time": 0.2, "score": 0.5, "level": 2, "notes": [{"s": 2, "f": 5}]},
        {"time": 0.5, "score": 0.1, "level": 0, "notes": [{"s": 5, "f": 5}]},
    ]
    routes._refine_lower_tier_path(groups, [], max_level=2)
    assert groups[1]["level"] == 0, (
        "a pure string skip (0->5, identical fret throughout) should still "
        "get bridged by an intermediate string position"
    )


# ── Item 5: fretted chord mid-tier voicing parity with keys ──────────────────

def test_fretted_chord_widens_through_three_tiers_before_the_top():
    chord = {"t": 1.0, "notes": [
        {"s": 4, "f": 0}, {"s": 3, "f": 2}, {"s": 2, "f": 2},
        {"s": 1, "f": 1}, {"s": 0, "f": 0},
    ]}
    groups = [{"type": "chord", "level": 0, "time": 1.0, "chord": chord, "notes": chord["notes"]}]
    max_level = 5
    counts = []
    for level in range(max_level + 1):
        notes, chords = routes._notes_for_level(groups, level, max_level)
        counts.append(len(notes) + sum(len(c.get("notes", [])) for c in chords))
    assert counts == sorted(counts), "chord note count must never decrease as the tier increases"
    assert len(set(counts[:-1])) >= 3, (
        "a 5-note chord should pass through at least 3 distinct partial-voicing "
        "sizes before the max-level full-chord state, not jump straight from a "
        "2-note voicing to the full chord"
    )
    assert counts[-1] == 5, "top tier keeps the full chord intact"


def test_pick_partial_voicing_prefers_fret_proximity_over_positional_order():
    root = {"s": 3, "f": 10}
    close = {"s": 2, "f": 11}  # 1 fret from root
    far = {"s": 1, "f": 2}     # 8 frets from root
    ranked = [root, far, close]  # naive positional order would pick root+far
    picked = routes._pick_partial_voicing(ranked, 2)
    assert picked == [root, close], (
        "partial voicing should keep the fret-close note, not the first "
        "positional one, so the reduced voicing isn't still a hard stretch"
    )


def test_pick_partial_voicing_prefers_an_open_string_when_spans_tie():
    root = {"s": 3, "f": 10}
    open_string = {"s": 2, "f": 0}    # free -- contributes no span
    fretted_tie = {"s": 1, "f": 10}   # same fret as root -> also zero added span
    ranked = [root, open_string, fretted_tie]
    picked = routes._pick_partial_voicing(ranked, 2)
    assert picked[0] == root
    assert picked[1] == open_string, (
        "an open string should be preferred over a same-span fretted "
        "alternative when the added span is tied"
    )


# ── Follow-up: pinch harmonics vs natural harmonics, bass slap/pop ─────────

def test_pinch_harmonic_scores_higher_than_natural_harmonic():
    natural = [{"time": 0.0, "notes": [{"s": 2, "f": 5, "sus": 0, "hm": True}]}]
    pinch = [{"time": 0.0, "notes": [{"s": 2, "f": 5, "sus": 0, "hp": True}]}]
    routes._score_groups(natural, n_strings=6)
    routes._score_groups(pinch, n_strings=6)
    assert pinch[0]["score"] > natural[0]["score"], (
        "a pinch harmonic requires more precise thumb-touch timing than a "
        "natural harmonic and should score harder, not the same"
    )


def test_slap_scores_higher_than_pop():
    pop = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0, "plk": True}]}]
    slap = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0, "slp": True}]}]
    routes._score_groups(pop, n_strings=6)
    routes._score_groups(slap, n_strings=6)
    assert slap[0]["score"] > pop[0]["score"], (
        "slap's percussive thumb strike is the harder half of the "
        "slap-and-pop pairing and should score harder than pop alone"
    )


def test_bass_slap_and_pop_previously_scored_as_a_plain_note():
    # Regression guard for the gap this follow-up closes: before plk/slp
    # were recognized, a slap-bass note scored identically to a plain
    # picked note (technique contributed nothing at all).
    plain = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0}]}]
    slap = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0, "slp": True}]}]
    routes._score_groups(plain, n_strings=6)
    routes._score_groups(slap, n_strings=6)
    assert slap[0]["score"] > plain[0]["score"]


def test_pinch_harmonic_gated_out_later_than_natural_harmonic():
    note_hm = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "hm": True}
    note_hp = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "hp": True}
    assert "hm" not in routes._prune_techniques(note_hm, diff_percent=0.70)
    assert "hp" not in routes._prune_techniques(note_hp, diff_percent=0.90), (
        "pinch harmonic should still be stripped at a diff_percent that "
        "already keeps a natural harmonic"
    )
    assert routes._prune_techniques(note_hp, diff_percent=0.96).get("hp") is True


def test_bass_slap_and_pop_are_gated_and_pruned():
    note_slp = {"t": 0.0, "s": 3, "f": 0, "sus": 0, "slp": True}
    note_plk = {"t": 0.0, "s": 3, "f": 0, "sus": 0, "plk": True}
    assert "slp" not in routes._prune_techniques(note_slp, diff_percent=0.5)
    assert routes._prune_techniques(note_slp, diff_percent=0.95).get("slp") is True
    assert "plk" not in routes._prune_techniques(note_plk, diff_percent=0.5)
    assert routes._prune_techniques(note_plk, diff_percent=0.85).get("plk") is True


# ── Follow-up 2: palm mute / string mute / vibrato / fret-hand mute ────────

def test_palm_mute_string_mute_and_vibrato_now_contribute_to_the_score():
    # Regression guard for the gap this follow-up closes: pm/mt/vb were
    # already gated (stripped correctly once a tier was assigned) but
    # contributed nothing to the score that decides which tier a note
    # lands in to begin with.
    plain = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0}]}]
    palm_muted = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0, "pm": True}]}]
    string_muted = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0, "mt": True}]}]
    vibrato = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0, "vb": True}]}]
    for group in (plain, palm_muted, string_muted, vibrato):
        routes._score_groups(group, n_strings=6)
    assert palm_muted[0]["score"] > plain[0]["score"]
    assert string_muted[0]["score"] > plain[0]["score"]
    assert vibrato[0]["score"] > plain[0]["score"]


def test_fret_hand_mute_now_scored_and_gated():
    # fhm was previously in neither _tech_score nor _TECH_GATE_FRAC: unscored
    # AND ungated, so it survived at every difficulty tier regardless of how
    # hard the passage was.
    plain = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0}]}]
    fret_hand_muted = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0, "fhm": True}]}]
    routes._score_groups(plain, n_strings=6)
    routes._score_groups(fret_hand_muted, n_strings=6)
    assert fret_hand_muted[0]["score"] > plain[0]["score"]

    note = {"t": 0.0, "s": 2, "f": 3, "sus": 0, "fhm": True}
    assert "fhm" not in routes._prune_techniques(note, diff_percent=0.5)
    assert routes._prune_techniques(note, diff_percent=0.80).get("fhm") is True


def test_pm_mt_vb_fhm_gated_out_of_low_tiers_end_to_end():
    def arr_with_technique(key):
        notes = []
        t = 0.0
        for i in range(60):
            n = {"t": round(t, 3), "s": i % 6, "f": (i * 3) % 20 + 1, "sus": 0}
            if i % 3 == 0:
                n[key] = True if key != "bn" else 1.0
            notes.append(n)
            t += 0.15
        return _arrangement(notes)

    for key in ("pm", "mt", "vb", "fhm"):
        arr = arr_with_technique(key)
        phrases = routes.generate_phrases_for_arrangement(arr, n_levels=6)
        assert phrases
        bottom_notes = phrases[0]["levels"][0]["notes"]
        assert not any(n.get(key) for n in bottom_notes), (
            f"{key} should not survive into the bottom tier of a technical phrase "
            f"now that it's gated and scored"
        )


# ── Follow-up 3: bend intent (bt) and bend curve (bnv) ──────────────────────

def test_bend_intent_scoring_reflects_relative_difficulty():
    def group(bt=None):
        note = {"s": 2, "f": 5, "sus": 0, "bn": 1.0}
        if bt is not None:
            note["bt"] = bt
        return [{"time": 0.0, "notes": [note]}]

    plain = group()  # bt omitted -> defaults to 0 (bend up)
    release = group(1)
    pre_bend = group(2)
    pre_bend_release = group(3)
    round_trip = group(4)
    for g in (plain, release, pre_bend, pre_bend_release, round_trip):
        routes._score_groups(g, n_strings=6)

    assert release[0]["score"] == plain[0]["score"], (
        "a release isn't meaningfully harder than a plain bend-up and should "
        "score identically"
    )
    assert pre_bend[0]["score"] > plain[0]["score"], (
        "a pre-bend (blind bend to pitch, no real-time auditory feedback) "
        "should score harder than a plain bend"
    )
    assert round_trip[0]["score"] > plain[0]["score"], (
        "a round-trip bend (bidirectional control within one note) should "
        "score harder than a plain bend"
    )
    assert pre_bend_release[0]["score"] > pre_bend[0]["score"], (
        "pre-bend-and-release combines the blind-bend and controlled-release "
        "demands and should score hardest"
    )


def test_bend_curve_with_shaping_scores_higher_than_a_trivial_two_point_curve():
    def group(bnv):
        note = {"s": 2, "f": 5, "sus": 0, "bn": 1.0, "bnv": bnv}
        return [{"time": 0.0, "notes": [note]}]

    trivial = group([{"t": 0, "v": 0}, {"t": 0.25, "v": 1.0}])
    shaped = group([
        {"t": 0, "v": 0}, {"t": 0.1, "v": 0.5}, {"t": 0.2, "v": 1.0}, {"t": 0.3, "v": 0.7},
    ])
    routes._score_groups(trivial, n_strings=6)
    routes._score_groups(shaped, n_strings=6)
    assert shaped[0]["score"] > trivial[0]["score"], (
        "a bend curve beyond a trivial two-point ramp signals deliberate "
        "mid-bend shaping and should score harder"
    )


def test_bend_intent_downgraded_below_its_gate_but_release_is_spared():
    note_pre_bend = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "bn": 1.0, "bt": 2}
    note_release = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "bn": 1.0, "bt": 1}

    below_bt_gate = routes._prune_techniques(note_pre_bend, diff_percent=0.60)
    assert below_bt_gate["bt"] == 0, "pre-bend should downgrade to a plain bend-up below its gate"
    assert below_bt_gate["bn"] == 1.0, "bn itself survives above its own (earlier) gate"

    above_bt_gate = routes._prune_techniques(note_pre_bend, diff_percent=0.70)
    assert above_bt_gate["bt"] == 2

    release_below_bt_gate = routes._prune_techniques(note_release, diff_percent=0.60)
    assert release_below_bt_gate["bt"] == 1, (
        "release is not meaningfully harder than a plain bend and should not "
        "be downgraded by the bt gate"
    )


def test_bend_curve_stripped_below_its_gate_bn_and_bt_survive():
    note = {
        "t": 0.0, "s": 2, "f": 5, "sus": 0, "bn": 1.0, "bt": 0,
        "bnv": [{"t": 0, "v": 0}, {"t": 0.25, "v": 1.0}],
    }
    below_bnv_gate = routes._prune_techniques(note, diff_percent=0.75)
    assert "bnv" not in below_bnv_gate
    assert below_bnv_gate["bn"] == 1.0
    assert below_bnv_gate["bt"] == 0

    above_bnv_gate = routes._prune_techniques(note, diff_percent=0.85)
    assert above_bnv_gate["bnv"] == note["bnv"]


def test_stripped_bend_does_not_leave_a_stale_bt_or_bnv_behind():
    note = {
        "t": 0.0, "s": 2, "f": 5, "sus": 0,
        "bn": 1.5, "bt": 3,
        "bnv": [{"t": 0, "v": 0}, {"t": 0.1, "v": 0.5}, {"t": 0.2, "v": 1.5}],
    }
    pruned = routes._prune_techniques(note, diff_percent=0.30)
    assert pruned["bn"] == 0
    assert pruned["bt"] == 0, "a pre-bend flag on a bn=0 note is nonsensical and must not survive"
    assert "bnv" not in pruned, "a stale bend curve must not survive when the bend itself is gone"


def test_stripped_bend_clears_a_release_bt_too_even_though_release_alone_is_spared():
    # Regression guard: bt's OWN gate deliberately spares release (bt=1)
    # since it isn't meaningfully harder than a plain bend (see
    # test_bend_intent_downgraded_below_its_gate_but_release_is_spared).
    # But once bn's gate strips the bend entirely, "release" is no longer
    # a meaningful description of anything -- there's no bend left to
    # release -- so it must be cleared too, not just the harder intents.
    note = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "bn": 1.5, "bt": 1}
    pruned = routes._prune_techniques(note, diff_percent=0.30)
    assert pruned["bn"] == 0
    assert pruned["bt"] == 0, (
        "a release flag on a bn=0 note is nonsensical and must not survive, "
        "even though release alone (bn intact) is never downgraded"
    )


# ---------------------------------------------------------------------------
# /generate and /generate-library input validation (issue #74) — `force`
# used to be `bool(value)`, which makes any nonempty string (including the
# literal string "false") truthy, and `levels`/`max_songs` were parsed with
# a bare `int(...)` that either silently clamped out-of-range values or
# raised an unhandled ValueError (500) on non-numeric input. GenerateIn /
# GenerateLibraryIn replace that with typed, bounds-checked models so
# malformed bodies are rejected (422) before any generation code runs.
# ---------------------------------------------------------------------------

def test_generate_in_accepts_a_well_formed_body():
    body = routes.GenerateIn(filename="song.feedpak", levels=6, force=True)
    assert body.filename == "song.feedpak"
    assert body.levels == 6
    assert body.force is True


def test_generate_in_defaults_levels_and_force_when_omitted():
    body = routes.GenerateIn(filename="song.feedpak")
    assert body.levels == 4
    assert body.force is False


def test_generate_in_rejects_string_boolean_for_force():
    # The historical bug: bool("false") is True. A strict model must
    # reject the string outright rather than coerce it to True.
    with pytest.raises(ValidationError):
        routes.GenerateIn(filename="song.feedpak", force="false")


def test_generate_in_rejects_out_of_range_levels():
    with pytest.raises(ValidationError):
        routes.GenerateIn(filename="song.feedpak", levels=99)
    with pytest.raises(ValidationError):
        routes.GenerateIn(filename="song.feedpak", levels=1)


def test_generate_in_rejects_malformed_levels():
    with pytest.raises(ValidationError):
        routes.GenerateIn(filename="song.feedpak", levels="not-a-number")


def test_generate_library_in_rejects_string_boolean_for_force():
    with pytest.raises(ValidationError):
        routes.GenerateLibraryIn(force="true")


def test_generate_library_in_rejects_out_of_range_max_songs():
    with pytest.raises(ValidationError):
        routes.GenerateLibraryIn(max_songs=5000)
    with pytest.raises(ValidationError):
        routes.GenerateLibraryIn(max_songs=0)


def _client_for(tmp_path):
    """A real FastAPI app with only this plugin's routes registered,
    wired to an empty tmp_path as the DLC root — lets the route-level
    tests below prove malformed requests never reach the filesystem."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    routes.setup(app, {"log": logging.getLogger("dd-test"), "get_dlc_dir": lambda: tmp_path})
    return TestClient(app)


def test_generate_route_rejects_string_boolean_force_with_no_write(tmp_path):
    client = _client_for(tmp_path)
    with patch.object(routes, "_generate_song") as generate_song:
        resp = client.post(
            f"/api/plugins/{routes.PLUGIN_ID}/generate",
            json={"filename": "song.feedpak", "force": "false"},
        )
    assert resp.status_code == 422
    generate_song.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_generate_route_rejects_malformed_levels_with_no_write(tmp_path):
    client = _client_for(tmp_path)
    with patch.object(routes, "_generate_song") as generate_song:
        resp = client.post(
            f"/api/plugins/{routes.PLUGIN_ID}/generate",
            json={"filename": "song.feedpak", "levels": "not-a-number"},
        )
    assert resp.status_code == 422
    generate_song.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_generate_library_route_rejects_out_of_range_max_songs_with_no_write(tmp_path):
    client = _client_for(tmp_path)
    with patch.object(routes.sloppak, "load_manifest") as load_manifest:
        resp = client.post(
            f"/api/plugins/{routes.PLUGIN_ID}/generate-library",
            json={"max_songs": 999999},
        )
    assert resp.status_code == 422
    load_manifest.assert_not_called()
    assert list(tmp_path.iterdir()) == []

"""Tests for the fretted-instrument difficulty ladder generator in routes.py.

Self-contained sys.path bootstrap (this plugin has no pyproject.toml / shared
conftest of its own) so `pytest tests/` works from this directory directly.
"""
import json
import logging
import math
import sys
from copy import deepcopy
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
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


def _assert_on_tier_scale(phrase, n_levels):
    """A generated phrase's levels sit on the shared tier scale: difficulty
    numbers start at 0 and strictly increase (sparse where tiers collapsed),
    and max_difficulty is the scale's top tier -- or 0 when the phrase
    collapsed to a single level (no ladder)."""
    diffs = [lvl["difficulty"] for lvl in phrase["levels"]]
    assert diffs[0] == 0  # nosec B101 - pytest assertion
    assert all(b > a for a, b in pairwise(diffs))  # nosec B101 - pytest assertion
    assert diffs[-1] <= n_levels - 1  # nosec B101 - pytest assertion
    expected_max = n_levels - 1 if len(diffs) > 1 else 0
    assert phrase["max_difficulty"] == expected_max  # nosec B101 - pytest assertion


def test_returns_none_for_near_empty_arrangement():
    arr = _arrangement(_simple_notes(0, 1, step=0.5))  # well under MIN_EVENTS_FOR_GENERATION
    assert routes.generate_phrases_for_arrangement(arr, n_levels=4) is None


def test_simple_phrase_gets_a_shorter_ladder_than_the_cap():
    notes = _simple_notes(0, 10, step=0.5, fret=3)  # constant fret -> near-zero score spread
    arr = _arrangement(notes)
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=6)
    assert phrases, "expected at least one phrase"
    assert len(phrases[0]["levels"]) < 6, (  # nosec B101 - pytest assertion
        "a near-constant, single-string phrase should not have a distinct level at every tier"
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
    assert phrases[2]["difficulty_cost"] == 0.0  # nosec B101 - pytest assertion


def test_dense_technical_phrase_uses_more_of_the_cap_than_a_simple_one():
    simple = _arrangement(_simple_notes(0, 10, step=0.5, fret=3))
    technical = _arrangement(_technical_notes(0, 10, step=0.1))

    simple_phrases = routes.generate_phrases_for_arrangement(simple, n_levels=6)
    technical_phrases = routes.generate_phrases_for_arrangement(technical, n_levels=6)

    assert simple_phrases and technical_phrases
    assert len(technical_phrases[0]["levels"]) > len(simple_phrases[0]["levels"])  # nosec B101 - pytest assertion


def test_difficulty_cost_reflects_mechanical_difficulty_of_full_phrase_content():
    """difficulty_cost (#72/B1) is the mean of the internal, purely-mechanical
    `cost` field across a phrase's full, untiered content -- it should rank a
    technically harder phrase above a simple one independent of, and by a
    wider margin than, ladder depth alone."""
    simple = _arrangement(_simple_notes(0, 10, step=0.5, fret=3))
    technical = _arrangement(_technical_notes(0, 10, step=0.1))

    simple_phrases = routes.generate_phrases_for_arrangement(simple, n_levels=6)
    technical_phrases = routes.generate_phrases_for_arrangement(technical, n_levels=6)

    assert simple_phrases and technical_phrases
    assert "difficulty_cost" in simple_phrases[0]  # nosec B101 - pytest assertion
    assert technical_phrases[0]["difficulty_cost"] > simple_phrases[0]["difficulty_cost"]  # nosec B101


def test_difficulty_cost_is_independent_of_ladder_depth():
    """Two phrases that both collapse to a single level (max_difficulty 0 --
    nothing to thin) can still report very different difficulty_cost: ladder
    depth answers 'how much does this get thinned', difficulty_cost answers
    'how hard is this to play at all' -- they must not be conflated."""
    easy = _arrangement(_simple_notes(0, 5, step=0.5, fret=0))
    hard_but_flat = _arrangement(_technical_notes(0, 2, step=0.1))

    easy_phrases = routes.generate_phrases_for_arrangement(easy, n_levels=1)
    hard_phrases = routes.generate_phrases_for_arrangement(hard_but_flat, n_levels=1)

    assert easy_phrases and hard_phrases
    assert easy_phrases[0]["max_difficulty"] == 0  # nosec B101 - pytest assertion
    assert hard_phrases[0]["max_difficulty"] == 0  # nosec B101 - pytest assertion
    assert hard_phrases[0]["difficulty_cost"] > easy_phrases[0]["difficulty_cost"]  # nosec B101


def test_bottom_tier_is_sparser_than_a_flat_percentile_split():
    arr = _arrangement(_technical_notes(0, 12, step=0.1))
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases
    levels = phrases[0]["levels"]
    top_count = len(levels[-1]["notes"]) + len(levels[-1]["chords"])
    bottom_count = len(levels[0]["notes"]) + len(levels[0]["chords"])
    assert top_count > 0
    # a flat percentile split would put ~1/n_levels of the content at the
    # bottom tier; the convex retention curve should land well under that
    assert bottom_count / top_count < 1.0 / 4  # nosec B101 - pytest assertion


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
    max_level = len(levels) - 1

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
        {"time": 0.1, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 5, "f": 2}]},
        {"time": 0.5, "cost": 0.3, "value": 1.0, "retention_score": 0.3,
         "level": 2, "notes": [{"s": 5, "f": 7}]},
        {"time": 1.0, "cost": 0.2, "value": 1.0, "retention_score": 0.2,
         "level": 0, "notes": [{"s": 5, "f": 12}]},
    ]

    routes._refine_lower_tier_path(groups, [0.0, 0.5, 1.0], max_level=2)

    assert groups[1]["level"] == 0, (
        "the omitted beat-aligned group should bridge the otherwise 10-fret lower-tier jump"
    )


def test_lower_tier_refinement_falls_back_to_a_beat_group_when_none_was_kept():
    groups = [
        {"time": 0.1, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 5, "f": 3}]},
        {"time": 0.5, "cost": 0.2, "value": 1.0, "retention_score": 0.2,
         "level": 2, "notes": [{"s": 5, "f": 4}]},
    ]

    routes._refine_lower_tier_path(groups, [0.0, 0.5], max_level=2)

    assert groups[1]["level"] == 0


def test_bottom_arpeggio_voice_preserves_the_root_string():
    # String 0 is the LOWEST string (feedpak-v1 §6.2), so s=1 is the root
    # here even though it's played after the higher s=5 note.
    groups = [{
        "type": "arpeggio", "level": 0, "time": 0.0, "chord": None,
        "notes": [{"t": 0.0, "s": 5, "f": 7}, {"t": 0.04, "s": 1, "f": 3}],
    }]

    notes, chords = routes._notes_for_level(groups, level=0, max_level=2)

    assert chords == []
    assert [(n["s"], n["f"]) for n in notes] == [(1, 3)]  # nosec B101 - pytest assertion


def test_bottom_arpeggio_voice_preserves_an_open_root_string():
    # The root string (s=0, the lowest) is played open here. Bottom-tier
    # arpeggio selection cares about the harmonic root, not hand position —
    # an open root is a valid, easier simplification, so it must not be
    # skipped in favor of the fretted note the way the jump-scoring anchor is.
    groups = [{
        "type": "arpeggio", "level": 0, "time": 0.0, "chord": None,
        "notes": [{"t": 0.0, "s": 0, "f": 0}, {"t": 0.04, "s": 3, "f": 5}],
    }]

    notes, chords = routes._notes_for_level(groups, level=0, max_level=2)

    assert chords == []
    assert [(n["s"], n["f"]) for n in notes] == [(0, 0)]  # nosec B101 - pytest assertion


def test_fret_jump_cost_decreases_with_more_time_available():
    def groups(second_time):
        return [
            {"time": 0.0, "notes": [{"s": 5, "f": 2, "sus": 0}]},
            {"time": second_time, "notes": [{"s": 5, "f": 15, "sus": 0}]},
        ]

    nearby = groups(0.5)
    after_rest = groups(2.0)
    routes._score_groups(nearby, n_strings=6)
    routes._score_groups(after_rest, n_strings=6)

    assert nearby[1]["cost"] > after_rest[1]["cost"]  # nosec B101 - pytest assertion
    # Movement pressure fades smoothly rather than disappearing at a fixed
    # one-second cutoff. Density also differs, so compare the isolated term.
    tempo = routes._TempoParams()
    assert routes._fitts_shift_bonus(13, 0.5, tempo) > routes._fitts_shift_bonus(13, 2.0, tempo) > 0  # nosec B101


def test_fitts_shift_cost_tracks_distance_and_tempo_relative_time():
    tempo = routes._TempoParams(fret_jump_window_seconds=1.0)
    assert routes._fitts_shift_bonus(7, 0.125, tempo) > routes._fitts_shift_bonus(7, 0.5, tempo)  # nosec B101
    assert routes._fitts_shift_bonus(7, 0.5, tempo) > routes._fitts_shift_bonus(7, 2.0, tempo)  # nosec B101
    assert routes._fitts_shift_bonus(9, 0.5, tempo) > routes._fitts_shift_bonus(3, 0.5, tempo)  # nosec B101
    assert routes._fitts_shift_bonus(0, 0.125, tempo) == 0  # nosec B101
    assert abs(routes._fitts_shift_bonus(8, 1.0, tempo) - 0.05) < 1e-12  # nosec B101


def test_group_anchor_note_prefers_a_fretted_note_over_an_incidental_open_string():
    # An open string needs no hand position at all, so it must not be picked
    # as the hand-position anchor when the group also has fretted notes —
    # even though it's the lowest string (the usual root convention).
    group = {"notes": [
        {"s": 0, "f": 0}, {"s": 1, "f": 12}, {"s": 2, "f": 13},
        {"s": 3, "f": 13}, {"s": 4, "f": 12}, {"s": 5, "f": 12},
    ]}
    anchor = routes._group_anchor_note(group)
    assert anchor == {"s": 1, "f": 12}  # nosec B101 - pytest assertion

    # All-open group: falls back to the lowest-string note.
    open_group = {"notes": [{"s": 5, "f": 0}, {"s": 4, "f": 0}]}
    assert routes._group_anchor_note(open_group) == {"s": 4, "f": 0}  # nosec B101 - pytest assertion


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

    assert far_position[1]["cost"] > close_position[1]["cost"], (  # nosec B101 - pytest assertion
        "a real large hand-position jump must still be penalized even when "
        "the anchor string happens to be open in the current group"
    )
    expected = routes._fitts_shift_bonus(11, 0.4, routes._TempoParams()) - routes._fitts_shift_bonus(1, 0.4, routes._TempoParams())
    assert abs(far_position[1]["cost"] - close_position[1]["cost"] - expected) < 1e-9, (  # nosec B101 - pytest assertion
        "an incidental open string on the anchor string must not itself "
        "read as a hand-position jump — the bonus must track the true "
        "fretted position (s=0), not the coincidentally-open anchor string"
    )


def test_low_position_wide_shape_has_extra_posture_cost():
    low = [{"s": 0, "f": 1}, {"s": 1, "f": 6}]
    high = [{"s": 0, "f": 12}, {"s": 1, "f": 17}]
    narrow = [{"s": 0, "f": 1}, {"s": 1, "f": 4}]
    assert routes._posture_score(low) > routes._posture_score(high) == 0  # nosec B101
    assert routes._posture_score(low) > routes._posture_score(narrow) == 0  # nosec B101

    group = [{"time": 0.0, "notes": [{**n, "sus": 0} for n in low]}]
    without_posture = deepcopy(group)
    with patch.object(routes, "_posture_score", return_value=0):
        routes._score_groups(without_posture, n_strings=6)
    routes._score_groups(group, n_strings=6)
    expected_bonus = 0.35 * 0.15 * routes._posture_score(low)
    assert abs(group[0]["cost"] - without_posture[0]["cost"] - expected_bonus) < 1e-12  # nosec B101


def test_technique_coordination_bonus_scores_simultaneous_techniques():
    """#72/B4: a chord mixing two different single-note techniques scores
    above the harder of the two alone, even though _tech_score's own
    max-only term is unchanged (it already reports the same 0.4 for a lone
    bend either way -- the coordination bonus is the only thing that can
    tell the two chords apart)."""
    assert routes._technique_coordination_bonus(set(), set()) == 0.0  # nosec B101
    assert routes._technique_coordination_bonus({"bend"}, set()) == 0.0  # nosec B101
    two_techniques = routes._technique_coordination_bonus({"bend", "palm_mute"}, set())
    assert two_techniques == pytest.approx(routes._COORD_PER_EXTRA_CATEGORY)  # nosec B101
    assert two_techniques > 0.0  # nosec B101


def test_technique_coordination_bonus_scores_a_switch_between_groups():
    assert routes._technique_coordination_bonus({"bend"}, {"bend"}) == 0.0  # nosec B101
    switched = routes._technique_coordination_bonus({"palm_mute"}, {"bend"})
    assert switched == pytest.approx(routes._COORD_SWITCH_BONUS)  # nosec B101
    # No previous group (start of the phrase) is not a "switch" -- there is
    # nothing to switch away from.
    assert routes._technique_coordination_bonus({"bend"}, set()) == 0.0  # nosec B101


def test_technique_coordination_bonus_is_capped():
    many_categories = {"bend", "hopo", "tap", "slide", "trem", "harm_nat"}
    capped = routes._technique_coordination_bonus(many_categories, {"vibrato"})
    assert capped == routes._COORD_MAX_BONUS  # nosec B101


def test_chord_with_two_different_techniques_scores_above_either_alone():
    """End-to-end through _score_groups: max(_tech_score) alone can't
    distinguish a chord using two different techniques from a chord using
    only the harder of the two -- the coordination bonus is what makes the
    two-technique chord cost more."""
    single = [{"time": 0.0, "notes": [
        {"s": 0, "f": 5, "bn": 1.0, "sus": 0}, {"s": 1, "f": 5, "sus": 0},
    ]}]
    mixed = [{"time": 0.0, "notes": [
        {"s": 0, "f": 5, "bn": 1.0, "sus": 0}, {"s": 1, "f": 5, "pm": True, "sus": 0},
    ]}]
    routes._score_groups(single, n_strings=6)
    routes._score_groups(mixed, n_strings=6)
    expected_bonus = 0.30 * routes._COORD_PER_EXTRA_CATEGORY
    assert abs(mixed[0]["cost"] - single[0]["cost"] - expected_bonus) < 1e-12  # nosec B101


def test_lower_tier_refinement_does_not_insert_a_needless_bridge_for_an_open_anchor():
    groups = [
        {"time": 0.0, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 0, "f": 12}]},
        # Would look like a plausible bridge under the old (buggy) jump
        # computation, but nothing here actually needs bridging.
        {"time": 0.2, "cost": 0.5, "value": 0.0, "retention_score": 0.5,
         "level": 2, "notes": [{"s": 0, "f": 6}]},
        {"time": 0.5, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [
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
        {"time": 0.0, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 0, "f": 2}]},
        {"time": 0.2, "cost": 0.5, "value": 0.0, "retention_score": 0.5,
         "level": 2, "notes": [{"s": 0, "f": 8}]},
        {"time": 0.5, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 0, "f": 15}]},
    ]
    routes._refine_lower_tier_path(groups, [], max_level=2)
    assert groups[1]["level"] == 0, (
        "a genuine fret-2-to-fret-15 jump should still get bridged"
    )


def test_refinement_prioritizes_phrase_then_bar_join_over_earlier_jump():
    groups = [
        {"time": t, "notes": [{"s": 2, "f": fret}], "level": level,
         "retention_score": 0.2}
        for t, fret, level in (
            (0.0, 2, 0), (0.2, 8, 2), (0.4, 15, 0),
            (0.7, 8, 2), (1.0, 2, 0),
            (1.3, 8, 2), (1.6, 15, 0),
        )
    ]
    times = [g["time"] for g in groups]
    promoted = []
    for _ in range(3):
        kept = [g for g in groups if g["level"] == 0]
        assert routes._promote_bridge_candidate(  # nosec B101 - pytest assertion
            kept, groups, times, [], 0, 7,
            tempo=routes._TempoParams(),
            phrase_boundaries=[1.2], bar_boundaries=[0.8],
        )
        promoted.append(next(g["time"] for g in groups
                             if g["time"] not in promoted and g["level"] == 0
                             and g["time"] in (0.2, 0.7, 1.3)))
    assert promoted == [1.3, 0.7, 0.2]  # nosec B101 - pytest assertion


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


# ── Issue #66: explicit allowlist for supported generator instruments ───────

@pytest.mark.parametrize("arr_type", ["lead", "rhythm", "bass", "combo", "chord", "humstrum"])
def test_instrument_kind_allows_every_known_fretted_type(arr_type):
    assert routes._instrument_kind(arr_type, "some name") == "fretted"


@pytest.mark.parametrize("arr_type", ["Lead", " RHYTHM ", "Bass"])
def test_instrument_kind_fretted_types_are_case_and_whitespace_insensitive(arr_type):
    assert routes._instrument_kind(arr_type, "") == "fretted"


def test_instrument_kind_blank_type_still_falls_back_to_fretted():
    # feedpakr (the GP importer) never sets `type` at all for fretted/keys
    # arrangements -- an absent/blank type must keep working exactly like
    # before this fix, or the vast majority of real packs would suddenly be
    # reported as unsupported.
    assert routes._instrument_kind("", "Lead") == "fretted"
    assert routes._instrument_kind(None, "Lead") == "fretted"


@pytest.mark.parametrize("arr_type", ["piano", "keys", "Piano", " KEYS "])
def test_instrument_kind_allows_known_keys_types(arr_type):
    assert routes._instrument_kind(arr_type, "some name") == "keys"


def test_instrument_kind_still_name_sniffs_keys_when_type_is_blank():
    assert routes._instrument_kind("", "Keys") == "keys"
    assert routes._instrument_kind("", "Synth Pad") == "keys"


@pytest.mark.parametrize("arr_type", ["drums", "drum", "Drums"])
def test_instrument_kind_recognizes_drum_types(arr_type):
    assert routes._instrument_kind(arr_type, "some name") == "drums"


@pytest.mark.parametrize("arr_type", ["vocals", "harmony", "notation", "bonus-track", "bogus"])
def test_instrument_kind_returns_unsupported_for_an_unrecognized_non_empty_type(arr_type):
    # The bug: an unknown non-drum type used to fall through to the fretted
    # heuristic (silently mis-scoring content this generator has no business
    # reading) instead of being explicitly rejected.
    assert routes._instrument_kind(arr_type, "some name") == "unsupported"


def test_instrument_kind_detects_drums_by_name_when_type_is_blank():
    # Issue #102: missing type should not silently mean fretted for names
    # identifying drums. Name-sniff for "Drums", "Drum 2", etc.
    assert routes._instrument_kind("", "Drums") == "drums"
    assert routes._instrument_kind("", "Drums 2") == "drums"
    assert routes._instrument_kind(None, "Drum Kit") == "drums"
    assert routes._instrument_kind("", "  Percussion  ") == "drums"


def test_instrument_kind_detects_unsupported_by_name_when_type_is_blank():
    # Issue #102: missing type should not silently mean fretted for names
    # identifying unsupported instruments (Sax, Vocals, etc.). Narrow to only
    # unambiguous non-fretted names to avoid false positives (e.g., "Harmony"
    # guitar, "Strings" arrangement are common fretted part names).
    assert routes._instrument_kind("", "Sax") == "unsupported"
    assert routes._instrument_kind("", "Saxophone") == "unsupported"
    assert routes._instrument_kind("", "Vocals") == "unsupported"
    assert routes._instrument_kind(None, "Violin") == "unsupported"
    assert routes._instrument_kind("", "  Cello  ") == "unsupported"
    assert routes._instrument_kind("", "Flute") == "unsupported"
    assert routes._instrument_kind("", "Trumpet") == "unsupported"


def test_instrument_kind_blank_type_with_fretted_names_still_defaults_to_fretted():
    # When type is blank and name doesn't match unsupported patterns,
    # should still default to fretted for backward compatibility with
    # legacy packs that omit type.
    assert routes._instrument_kind("", "Lead") == "fretted"
    assert routes._instrument_kind("", "Rhythm") == "fretted"
    assert routes._instrument_kind(None, "Combo") == "fretted"
    assert routes._instrument_kind("", "My Custom Arrangement") == "fretted"


def test_generate_phrases_for_arrangement_skips_an_unsupported_instrument_type():
    arr = _arrangement(_simple_notes(0, 10, step=0.5))
    arr["type"] = "vocals"
    assert routes.generate_phrases_for_arrangement(arr, n_levels=4) is None


def test_generate_one_reports_unsupported_instrument_type_distinctly_from_drums():
    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    fake_arr = {"type": "vocals"}
    with patch.object(routes, "_lock_for_pack", return_value=_Lock()), \
         patch.object(routes, "_load_manifest_and_arrangement",
                      return_value=("arrangements/vocals.json", fake_arr, None)):
        result = routes._generate_one(Path("unused"), 0, n_levels=4, force=False, log=None)

    assert result == {
        "ok": True,
        "skipped": "unsupported-instrument-type",
        "arrangement_index": 0,
        "instrument": "unsupported",
    }


def test_generate_song_breaks_out_unsupported_from_generic_skipped_count():
    manifest = {"arrangements": [{}, {}, {}]}
    results = [
        {"ok": True, "arrangement_index": 0, "instrument": "fretted"},
        {"ok": True, "arrangement_index": 1, "skipped": "unsupported-instrument-drums", "instrument": "drums"},
        {"ok": True, "arrangement_index": 2, "skipped": "unsupported-instrument-type", "instrument": "unsupported"},
    ]
    with patch.object(routes.sloppak, "load_manifest", return_value=manifest), patch.object(
        routes, "_generate_one", side_effect=results
    ):
        summary = routes._generate_song(Path("unused"), n_levels=4, force=False, log=None)

    assert summary["generated"] == 1
    assert summary["skipped"] == 2
    assert summary["unsupported"] == 2


def test_generate_song_does_not_count_already_has_phrases_as_unsupported():
    manifest = {"arrangements": [{}]}
    results = [
        {"ok": True, "arrangement_index": 0, "skipped": "already-has-phrases", "instrument": "fretted"},
    ]
    with patch.object(routes.sloppak, "load_manifest", return_value=manifest), patch.object(
        routes, "_generate_one", side_effect=results
    ):
        summary = routes._generate_song(Path("unused"), n_levels=4, force=False, log=None)

    assert summary["skipped"] == 1
    assert summary["unsupported"] == 0


def test_generate_library_route_reports_unsupported_instrument_count(tmp_path):
    fretted = _arrangement(_simple_notes(0, 10, step=0.5), sections=[{"time": 0}, {"time": 4}])
    vocals = _arrangement(_simple_notes(0, 10, step=0.5), sections=[{"time": 0}, {"time": 4}])
    vocals["type"] = "vocals"
    arrangements = [
        ("arrangements/lead.json", fretted),
        ("arrangements/vocals.json", vocals),
    ]
    dlc_root = tmp_path / "dlc"
    dlc_root.mkdir()
    _write_pack(dlc_root, "song.feedpak", arrangements, song_timeline_sections=[0, 5])

    client = _client_for(dlc_root)
    resp = client.post(f"/api/plugins/{routes.PLUGIN_ID}/generate-library", json={"force": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["generated"] == 1
    assert body["unsupported"] == 1
    assert body["skipped"] == 1


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
        {"time": 0.0, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 5, "f": 2}]},
        {"time": 1.0, "cost": 0.2, "value": 0.0, "retention_score": 0.2,
         "level": 2, "notes": [{"s": 5, "f": 7}]},
        {"time": 2.0, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 5, "f": 12}]},
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
    # Neither note has evidence of arpeggio identity (issue #73: no sustain
    # overlap, no authored hand-shape/chord-template match), so the loose
    # window clusters them into a "run" (melodic sequence), not an
    # "arpeggio" — see test_group_notes_classifies_* below for the
    # evidence-specific cases.
    notes = [
        {"t": 0.0, "s": 0, "f": 3},
        {"t": 0.1, "s": 1, "f": 3},
    ]
    tight = routes._group_notes(notes, [], time_window_ms=62.5)
    loose = routes._group_notes(notes, [], time_window_ms=250.0)
    assert [g["type"] for g in tight] == ["note", "note"]
    assert [g["type"] for g in loose] == ["run"]


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
    assert far_from_beat[0]["cost"] > near_beat[0]["cost"], (
        "landing further from the beat grid (more syncopated) should score "
        "harder even with identical note/fret/technique content"
    )


def _reference_fretted_scores(groups, n_strings, beat_times=(), *, tempo=None):
    """Independent oracle for the current fretted retention formula."""
    tempo = tempo or routes._TempoParams()
    legacy = deepcopy(groups)
    times_sorted = [float(g["time"]) for g in legacy]
    for gi, group in enumerate(legacy):
        notes = group["notes"]
        if not notes:
            group["score"] = 0.0
            continue
        avg_fret = sum(n.get("f", 0) for n in notes) / len(notes)
        count_ratio = min(1.0, (len(notes) - 1) / max(n_strings - 1, 1))
        spread_ratio = routes._string_span_score(notes, n_strings)
        string_shape = (
            routes._STRING_SPREAD_BLEND * spread_ratio
            + (1.0 - routes._STRING_SPREAD_BLEND) * count_ratio
        )
        frets = [int(n.get("f", 0)) for n in notes if int(n.get("f", 0)) > 0]
        posture = 0.0
        if len(frets) >= 2:
            stretch = min(1.0, max(0, max(frets) - min(frets) - 3) / 4.0)
            low_position = min(1.0, max(0.0, (9 - min(frets)) / 8.0))
            posture = stretch * low_position
        fretting = min(1.0,
            0.4 * routes._fret_score(avg_fret)
            + 0.35 * routes._span_score(notes)
            + 0.25 * string_shape
            + 0.15 * posture
        )
        technique = max(routes._tech_score(n) for n in notes)
        raw_density = routes._sequential_density(times_sorted, gi, tempo)
        syncopation = routes._syncopation_score(
            group["time"], beat_times, tempo.beat_interval,
        )
        density = min(
            1.0,
            (1.0 - routes._SYNCOPATION_DENSITY_WEIGHT) * raw_density
            + routes._SYNCOPATION_DENSITY_WEIGHT * syncopation,
        )
        max_sus = max(float(n.get("sus", 0)) for n in notes)
        sustain_ease = min(1.0, max_sus / tempo.sustain_ease_norm_seconds)
        group["score"] = (
            0.35 * fretting + 0.30 * technique + 0.20 * density
            + 0.15 * (1.0 - sustain_ease)
        )
        if routes._is_beat_aligned(
            group["time"], beat_times, tolerance=tempo.beat_tolerance,
        ):
            group["score"] -= 0.12
        if gi:
            previous = routes._group_anchor_note(legacy[gi - 1])
            current = routes._group_anchor_note(group)
            if previous and current:
                available = float(group["time"]) - float(legacy[gi - 1]["time"])
                fret_jump = abs(int(current.get("f", 0)) - int(previous.get("f", 0)))
                index = math.log2(fret_jump / 2.0 + 1.0)
                reference = math.log2(8.0 / 2.0 + 1.0)
                pressure = tempo.fret_jump_window_seconds / (tempo.fret_jump_window_seconds + max(available, 0.0))
                group["score"] += min(0.10, 0.10 * index / reference * pressure)
                if available <= tempo.fret_jump_window_seconds:
                    string_jump = abs(int(current.get("s", 0)) - int(previous.get("s", 0)))
                    group["score"] += min(
                        routes._STRING_JUMP_MAX_BONUS,
                        max(0, string_jump - routes._STRING_JUMP_THRESHOLD)
                        * routes._STRING_JUMP_COEF,
                    )
        group["score"] = max(0.0, min(1.0, group["score"]))
    return [g["score"] for g in legacy]


def _legacy_keys_scores(groups, *, tempo=None):
    """Independent oracle for the pre-split Keys score implementation."""
    tempo = tempo or routes._TempoParams()
    legacy = deepcopy(groups)
    total = len(legacy)
    times_sorted = [float(g["time"]) for g in legacy]
    for gi, group in enumerate(legacy):
        notes = group["notes"]
        if not notes:
            group["score"] = 0.0
            continue
        midis = [routes._note_midi_keys(n) for n in notes]
        poly = min(1.0, (len(notes) - 1) / 4.0)
        span = max(midis) - min(midis) if len(midis) > 1 else 0
        span_score = min(1.0, span / 12.0)
        density = routes._sequential_density(times_sorted, gi, tempo)
        speed = 0.0
        if gi + 1 < total:
            dt = float(legacy[gi + 1]["time"]) - float(group["time"])
            if dt > 0:
                speed = min(1.0, max(0.0, (0.25 - dt) / 0.25))
        max_sus = max(float(n.get("sus", 0)) for n in notes)
        sustain_ease = min(1.0, max_sus / 2.0)
        group["score"] = (
            0.30 * poly + 0.25 * span_score + 0.20 * density
            + 0.15 * speed + 0.10 * (1.0 - sustain_ease)
        )
    return [g["score"] for g in legacy]


def test_fretted_cost_is_intrinsic_while_beat_value_changes_retention_rank():
    groups = [
        {"time": 0.0, "notes": [{"s": 2, "f": 5, "sus": 0}]},
        {"time": 1.0, "notes": [{"s": 2, "f": 5, "sus": 0}]},
    ]
    beat_times = [0.0]

    routes._score_groups(groups, n_strings=6, beat_times=beat_times)

    assert groups[0]["cost"] == groups[1]["cost"]  # nosec B101 - pytest assertion
    assert [g["value"] for g in groups] == [1.0, 0.0]  # nosec B101 - pytest assertion
    assert groups[0]["retention_score"] < groups[1]["retention_score"]  # nosec B101 - pytest assertion

    thresholds = routes._tier_thresholds(
        [g["retention_score"] for g in groups], n_tiers=2,
    )
    routes._assign_tiers(groups, 2, thresholds, beat_times)
    assert [g["level"] for g in groups] == [0, 1]  # nosec B101 - pytest assertion


def test_fretted_retention_discount_precedes_clamp():
    groups = [
        {"time": i / 10, "notes": [{"s": 5, "f": 1, "sus": 0}]}
        for i in range(7)
    ]
    groups.append(
        {"time": 0.7, "notes": [
            {"s": s, "f": 20 + s, "sus": 0, "tp": True,
             "bn": 2, "bt": 3}
            for s in range(6)
        ]}
    )

    reference_scores = _reference_fretted_scores(groups, n_strings=6, beat_times=[0.7])
    routes._score_groups(groups, n_strings=6, beat_times=[0.7])

    scored = groups[-1]
    assert scored["cost"] > 1.0  # nosec B101 - pytest assertion
    assert scored["retention_score"] == reference_scores[-1]  # nosec B101 - pytest assertion
    assert scored["retention_score"] < 1.0  # nosec B101 - pytest assertion


@pytest.mark.parametrize("sustain", [0, 2.0, -0.5, "2.0"])
def test_fretted_retention_score_matches_movement_reference_formula(sustain):
    groups = [
        {"time": 0.0, "notes": [{"s": 5, "f": 0, "sus": sustain}]},
        {"time": 0.1, "notes": [
            {"s": s, "f": 20 + s, "sus": sustain, "tp": True}
            for s in range(6)
        ]},
        *[
            {"time": 0.1 + i / 10, "notes": [{"s": i % 6, "f": 3, "sus": sustain}]}
            for i in range(1, 8)
        ],
        {"time": 2.0, "notes": []},
    ]
    beat_times = [0.0, 0.1]
    expected = _reference_fretted_scores(groups, n_strings=6, beat_times=beat_times)

    routes._score_groups(groups, n_strings=6, beat_times=beat_times)

    assert [g["retention_score"] for g in groups] == expected  # nosec B101 - pytest assertion
    assert groups[-1] == {  # nosec B101 - pytest assertion
        "time": 2.0, "notes": [], "cost": 0.0, "value": 0.0,
        "retention_score": 0.0,
    }


def test_fretted_malformed_sustain_keeps_error_behavior():
    groups = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": "invalid"}]}]

    with pytest.raises(ValueError):
        _reference_fretted_scores(groups, n_strings=6)
    with pytest.raises(ValueError):
        routes._score_groups(deepcopy(groups), n_strings=6)


def test_keys_cost_and_retention_score_remain_identical_with_no_value():
    groups = [{
        "time": 0.0,
        "notes": [{"s": 2, "f": 0, "sus": 0}, {"s": 2, "f": 12, "sus": 0}],
    }]

    routes._score_groups_keys(groups)

    assert groups[0]["value"] == 0.0  # nosec B101 - pytest assertion
    assert groups[0]["cost"] == groups[0]["retention_score"]  # nosec B101 - pytest assertion
    assert 0.0 <= groups[0]["cost"] <= 1.0  # nosec B101 - pytest assertion


@pytest.mark.parametrize("sustain", [0, 2.0, -0.5, "2.0"])
def test_keys_cost_exactly_matches_legacy_formula(sustain):
    groups = [
        {"time": 0.0, "notes": []},
        {"time": 0.1, "notes": [
            {"s": 2, "f": 0, "sus": sustain},
            {"s": 2, "f": 12, "sus": sustain},
        ]},
        {"time": 0.2, "notes": [{"s": 3, "f": 7, "sus": sustain}]},
    ]
    expected = _legacy_keys_scores(groups)

    routes._score_groups_keys(groups)

    assert [g["cost"] for g in groups] == expected  # nosec B101 - pytest assertion
    assert [g["retention_score"] for g in groups] == expected  # nosec B101 - pytest assertion
    assert all(g["value"] == 0.0 for g in groups)  # nosec B101 - pytest assertion


def test_keys_malformed_sustain_keeps_legacy_error_behavior():
    groups = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": "invalid"}]}]

    with pytest.raises(ValueError):
        _legacy_keys_scores(groups)
    with pytest.raises(ValueError):
        routes._score_groups_keys(deepcopy(groups))


def test_movement_cost_generates_nested_ladder_fixture():
    notes = [
        {"t": i * 0.5, "s": 2, "f": 3 + (i % 3), "sus": 0}
        for i in range(8)
    ]
    arr = _arrangement(notes, n_beats=10)

    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=3)

    assert phrases is not None  # nosec B101 - pytest assertion
    assert [(level["difficulty"], [n["t"] for n in level["notes"]])
            for level in phrases[0]["levels"]] == [
        (0, [0.0, 0.5]),
        (1, [0.0, 0.5, 1.5, 2.0, 3.0]),
        (2, [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]),
    ]


# ── Item 4: string-skip / hand-shape difficulty ──────────────────────────────

def test_string_spread_increases_fretting_score():
    narrow = [{"time": 0.0, "notes": [{"s": 0, "f": 5, "sus": 0}, {"s": 1, "f": 5, "sus": 0}]}]
    wide = [{"time": 0.0, "notes": [{"s": 0, "f": 5, "sus": 0}, {"s": 5, "f": 5, "sus": 0}]}]
    routes._score_groups(narrow, n_strings=6)
    routes._score_groups(wide, n_strings=6)
    assert wide[0]["cost"] > narrow[0]["cost"], (
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

    assert big_skip[1]["cost"] > small_skip[1]["cost"]
    assert abs(big_skip[1]["cost"] - small_skip[1]["cost"] - 0.06) < 1e-9  # min(0.08, (5-3)*0.03)


def test_lower_tier_refinement_bridges_a_string_skip_even_when_the_fret_jump_is_small():
    groups = [
        {"time": 0.0, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 0, "f": 3}]},
        # Small fret movement (3->4->5, jump of 2 -- well under the fret-only
        # max_jump=7) but a full string skip (0->2->5) -- the old fret-only
        # trigger would never have looked here; the new string-jump trigger
        # (skip of 5, over max_string_jump=3) does.
        {"time": 0.2, "cost": 0.5, "value": 0.0, "retention_score": 0.5,
         "level": 2, "notes": [{"s": 2, "f": 4}]},
        {"time": 0.5, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 5, "f": 5}]},
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
        {"time": 0.0, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 0, "f": 5}]},
        {"time": 0.2, "cost": 0.5, "value": 0.0, "retention_score": 0.5,
         "level": 2, "notes": [{"s": 2, "f": 5}]},
        {"time": 0.5, "cost": 0.1, "value": 0.0, "retention_score": 0.1,
         "level": 0, "notes": [{"s": 5, "f": 5}]},
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
    assert pinch[0]["cost"] > natural[0]["cost"], (
        "a pinch harmonic requires more precise thumb-touch timing than a "
        "natural harmonic and should score harder, not the same"
    )


def test_slap_scores_higher_than_pop():
    pop = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0, "plk": True}]}]
    slap = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0, "slp": True}]}]
    routes._score_groups(pop, n_strings=6)
    routes._score_groups(slap, n_strings=6)
    assert slap[0]["cost"] > pop[0]["cost"], (
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
    assert slap[0]["cost"] > plain[0]["cost"]


def test_pinch_harmonic_gated_out_later_than_natural_harmonic():
    # Fret 12: stripping a natural harmonic there keeps its pitch, so the
    # gate applies (see test_natural_harmonic_is_kept_where_stripping_would_change_its_pitch).
    note_hm = {"t": 0.0, "s": 2, "f": 12, "sus": 0, "hm": True}
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
    assert palm_muted[0]["cost"] > plain[0]["cost"]
    assert string_muted[0]["cost"] > plain[0]["cost"]
    assert vibrato[0]["cost"] > plain[0]["cost"]


def test_fret_hand_mute_now_scored_and_gated():
    # fhm was previously in neither _tech_score nor _TECH_GATE_FRAC: unscored
    # AND ungated, so it survived at every difficulty tier regardless of how
    # hard the passage was.
    plain = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0}]}]
    fret_hand_muted = [{"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0, "fhm": True}]}]
    routes._score_groups(plain, n_strings=6)
    routes._score_groups(fret_hand_muted, n_strings=6)
    assert fret_hand_muted[0]["cost"] > plain[0]["cost"]

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

    assert release[0]["cost"] == plain[0]["cost"], (
        "a release isn't meaningfully harder than a plain bend-up and should "
        "score identically"
    )
    assert pre_bend[0]["cost"] > plain[0]["cost"], (
        "a pre-bend (blind bend to pitch, no real-time auditory feedback) "
        "should score harder than a plain bend"
    )
    assert round_trip[0]["cost"] > plain[0]["cost"], (
        "a round-trip bend (bidirectional control within one note) should "
        "score harder than a plain bend"
    )
    assert pre_bend_release[0]["cost"] > pre_bend[0]["cost"], (
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
    assert shaped[0]["cost"] > trivial[0]["cost"], (
        "a bend curve beyond a trivial two-point ramp signals deliberate "
        "mid-bend shaping and should score harder"
    )


def test_bend_intent_downgraded_below_its_gate_but_release_is_spared():
    note_round_trip = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "bn": 1.0, "bt": 4}
    note_pre_bend = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "bn": 1.0, "bt": 2}
    note_release = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "bn": 1.0, "bt": 1}

    below_bt_gate = routes._prune_techniques(note_round_trip, diff_percent=0.60)
    assert below_bt_gate["bt"] == 0, "round-trip should downgrade to a plain bend-up below its gate"  # nosec B101 - pytest assertion
    assert below_bt_gate["bn"] == 1.0, "bn itself survives above its own (earlier) gate"
    assert below_bt_gate["f"] == 5, "both are struck unbent, so the fret is unchanged"  # nosec B101 - pytest assertion

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
        "bn": 1.5, "bt": 4,
        "bnv": [{"t": 0, "v": 0}, {"t": 0.1, "v": 1.5}, {"t": 0.2, "v": 0}],
    }
    pruned = routes._prune_techniques(note, diff_percent=0.30)
    assert pruned["bn"] == 0
    assert pruned["bt"] == 0, "a round-trip flag on a bn=0 note is nonsensical and must not survive"  # nosec B101 - pytest assertion
    assert "bnv" not in pruned, "a stale bend curve must not survive when the bend itself is gone"
    assert pruned["f"] == 5, "a round-trip is struck unbent, so the fret is unchanged"  # nosec B101 - pytest assertion


def test_stripped_bend_clears_a_release_bt_too_even_though_release_alone_is_spared():
    # Regression guard: bt's OWN gate deliberately spares release (bt=1)
    # since it isn't meaningfully harder than a plain bend (see
    # test_bend_intent_downgraded_below_its_gate_but_release_is_spared).
    # But once bn's gate strips the bend entirely, "release" is no longer
    # a meaningful description of anything -- there's no bend left to
    # release -- so it must be cleared too, not just the harder intents.
    note = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "bn": 1.0, "bt": 1}
    pruned = routes._prune_techniques(note, diff_percent=0.30)
    assert pruned["bn"] == 0
    assert pruned["bt"] == 0, (
        "a release flag on a bn=0 note is nonsensical and must not survive, "
        "even though release alone (bn intact) is never downgraded"
    )
    # A release is struck at the bent pitch: the simplified note is fretted there.
    assert pruned["f"] == 6  # nosec B101 - pytest assertion


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


def test_generate_library_in_rejects_out_of_range_max_processing_seconds():
    with pytest.raises(ValidationError):
        routes.GenerateLibraryIn(max_processing_seconds=601)
    with pytest.raises(ValidationError):
        routes.GenerateLibraryIn(max_processing_seconds=0)


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


# ---------------------------------------------------------------------------
# _generate_song / /generate-library: canonical section boundaries and
# failure isolation (issue #67) — a directory-form feedpak fixture is real
# enough to drive both entry points end-to-end and diff their output.
# ---------------------------------------------------------------------------

_TEST_LOG = logging.getLogger("dd-generation-test")


def _write_pack(root, name, arrangements, song_timeline_sections=None):
    """A minimal directory-form feedpak under root/name. `arrangements` is a
    list of (relpath, arr_dict) pairs. When `song_timeline_sections` is
    given, a timeline.json is written and wired up as the manifest's
    `song_timeline` key -- feedBack's canonical section source (see
    routes._canonical_section_times)."""
    pack_dir = root / name
    (pack_dir / "arrangements").mkdir(parents=True)
    manifest = {"arrangements": [{"file": rel} for rel, _ in arrangements]}
    if song_timeline_sections is not None:
        (pack_dir / "timeline.json").write_text(json.dumps({
            "beats": [{"time": i * 0.5} for i in range(40)],
            "sections": [{"time": t} for t in song_timeline_sections],
        }))
        manifest["song_timeline"] = "timeline.json"
    (pack_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    for rel, arr in arrangements:
        (pack_dir / rel).write_text(json.dumps(arr))
    return pack_dir


# Chordr's service can be stubbed: these tests pin the preview's HTTP and
# forwarding contract without requiring the sibling plugin to be installed.
_CHORD_PREVIEW_URL = f"/api/plugins/{routes.PLUGIN_ID}/analyze-chords"


def _preview(client, filename="song.feedpak", arrangement_index=0):
    return client.post(_CHORD_PREVIEW_URL, json={
        "filename": filename, "arrangement_index": arrangement_index,
    })


def test_chord_preview_forwards_fretted_data_without_writing(tmp_path):
    arr = _arrangement([], chords=[{"t": 1.0, "id": 0, "notes": [{"s": 0, "f": 2}]}])
    arr.update(type="bass", name="Bass", capo=2, tuning=[0, 0, 0, 0],
               templates=[{"name": "F#"}])
    pack = _write_pack(tmp_path, "song.feedpak", [("arrangements/bass.json", arr)])
    before = {p.relative_to(pack): p.read_bytes() for p in pack.rglob("*") if p.is_file()}
    client = _client_for(tmp_path)
    calls = []

    def analyze(chords, *, context, templates):
        calls.append((chords, context, templates))
        return {"grouped": [{"parentIndex": 0, "continuation": False}]}

    client.app.state.chordr_analyze_chart_chords_v1 = analyze
    resp = _preview(client)
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True, "filename": "song.feedpak", "arrangement_index": 0,
        "chord_count": 1,
        "grouped": [{"parentIndex": 0, "continuation": False}],
    }
    assert calls == [(arr["chords"], {
        "tuning": arr["tuning"], "capo": 2, "stringCount": 4, "isBass": True,
    }, arr["templates"])]
    assert before == {p.relative_to(pack): p.read_bytes() for p in pack.rglob("*") if p.is_file()}


def test_chord_preview_does_not_infer_bass_from_name_fragment(tmp_path):
    arr = _arrangement([])
    arr["name"] = "Ambassador Lead"
    _write_pack(tmp_path, "song.feedpak", [("arrangements/lead.json", arr)])
    client = _client_for(tmp_path)
    contexts = []
    client.app.state.chordr_analyze_chart_chords_v1 = (
        lambda chords, *, context, templates: contexts.append(context) or {}
    )
    assert _preview(client).status_code == 200
    assert contexts[0]["isBass"] is False


def test_chord_preview_infers_bass_from_legacy_name(tmp_path):
    arr = _arrangement([])
    arr.update(type="", name="Bass 2")
    _write_pack(tmp_path, "song.feedpak", [("arrangements/bass.json", arr)])
    client = _client_for(tmp_path)
    contexts = []
    client.app.state.chordr_analyze_chart_chords_v1 = (
        lambda chords, *, context, templates: contexts.append(context) or {}
    )
    assert _preview(client).status_code == 200
    assert contexts[0]["isBass"] is True


def test_chord_preview_rejects_missing_library_and_invalid_filenames(tmp_path):
    assert _preview(_client_for(None)).status_code == 400
    client = _client_for(tmp_path)
    for filename, expected in (("../song.feedpak", 400), ("song.mp3", 400),
                               ("missing.feedpak", 404)):
        assert _preview(client, filename).status_code == expected
    assert list(tmp_path.iterdir()) == []


def test_chord_preview_rejects_bad_index_drums_and_keys(tmp_path):
    lead = _arrangement([])
    keys = _arrangement([])
    keys.update(type="keys", name="Keys")
    pack = _write_pack(tmp_path, "song.feedpak", [
        ("arrangements/lead.json", lead), ("arrangements/drums.json", lead),
        ("arrangements/keys.json", keys),
    ])
    manifest_path = pack / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["arrangements"][1]["type"] = "drums"
    manifest_path.write_text(yaml.safe_dump(manifest))
    client = _client_for(tmp_path)
    for index in (3, 1, 2):
        assert _preview(client, arrangement_index=index).status_code == 400


def test_chord_preview_reports_missing_service_and_service_failure(tmp_path):
    _write_pack(tmp_path, "song.feedpak", [("arrangements/lead.json", _arrangement([]))])
    client = _client_for(tmp_path)
    assert _preview(client).status_code == 503
    assert _preview(client).json()["detail"] == "Chordr server analysis is not active"
    client.app.state.chordr_analyze_chart_chords_v1 = lambda *args, **kwargs: 1 / 0
    resp = _preview(client)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Chordr analysis failed"


@pytest.mark.parametrize("bad_data", ["{bad json", b"\xff\xfe"])
def test_chord_preview_rejects_malformed_arrangement_bytes(tmp_path, bad_data):
    pack = _write_pack(tmp_path, "song.feedpak", [("arrangements/lead.json", _arrangement([]))])
    (pack / "arrangements/lead.json").write_bytes(
        bad_data if isinstance(bad_data, bytes) else bad_data.encode()
    )
    resp = _preview(_client_for(tmp_path))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "malformed arrangement"


@pytest.mark.parametrize("field,value", [("tuning", 123), ("chords", "bad"),
                                       ("templates", "bad")])
def test_chord_preview_rejects_malformed_field_shapes(tmp_path, field, value):
    arr = _arrangement([])
    arr[field] = value
    _write_pack(tmp_path, "song.feedpak", [("arrangements/lead.json", arr)])
    assert _preview(_client_for(tmp_path)).status_code == 400


def test_chord_preview_preserves_missing_member_and_manifest_404(tmp_path):
    pack = _write_pack(tmp_path, "song.feedpak", [("arrangements/lead.json", _arrangement([]))])
    (pack / "arrangements/lead.json").unlink()
    assert _preview(_client_for(tmp_path)).status_code == 404
    (pack / "manifest.yaml").unlink()
    assert _preview(_client_for(tmp_path)).status_code == 404
    (pack / "manifest.yaml").write_text("arrangements: [not valid")
    assert _preview(_client_for(tmp_path)).status_code == 400


def _phrase_boundaries(pack_dir, rel):
    arr = json.loads((pack_dir / rel).read_text())
    return [(p["start_time"], p["end_time"]) for p in arr["phrases"]]


def test_generate_song_uses_canonical_song_timeline_over_arrangement_sections(tmp_path):
    # Two arrangements with DIFFERENT own `sections`, and a manifest-level
    # song_timeline with a THIRD set of boundaries. _generate_song must use
    # the canonical song_timeline for both, not each arrangement's own
    # (divergent) `sections` field -- this is what keeps generated phrases
    # aligned with Section Map's highway.getSections().
    arrangements = [
        ("arrangements/lead.json", _arrangement(_simple_notes(0, 10, step=0.5), sections=[{"time": 0}, {"time": 4}])),
        ("arrangements/bass.json", _arrangement(_simple_notes(0, 10, step=0.5), sections=[{"time": 0}, {"time": 6}])),
    ]
    pack_dir = _write_pack(tmp_path, "song.feedpak", arrangements, song_timeline_sections=[0, 5])

    summary = routes._generate_song(pack_dir, n_levels=4, force=True, log=_TEST_LOG)
    assert summary["generated"] == 2

    lead_bounds = _phrase_boundaries(pack_dir, "arrangements/lead.json")
    bass_bounds = _phrase_boundaries(pack_dir, "arrangements/bass.json")
    # Canonical boundary is 5.0 -- neither arrangement's own 4.0 nor 6.0.
    assert lead_bounds[0][1] == 5.0
    assert bass_bounds[0][1] == 5.0
    assert lead_bounds == bass_bounds


def test_generate_library_route_matches_generate_song_phrase_boundaries(tmp_path):
    # Same fixture (two copies), one run through _generate_song() directly,
    # one through the /generate-library sweep -- both entry points must
    # compute identical canonical-section phrase boundaries for the same
    # song content.
    arrangements = [
        ("arrangements/lead.json", _arrangement(_simple_notes(0, 10, step=0.5), sections=[{"time": 0}, {"time": 4}])),
        ("arrangements/bass.json", _arrangement(_simple_notes(0, 10, step=0.5), sections=[{"time": 0}, {"time": 6}])),
    ]
    single_song = _write_pack(tmp_path, "single.feedpak", arrangements, song_timeline_sections=[0, 5])
    single_summary = routes._generate_song(single_song, n_levels=4, force=True, log=_TEST_LOG)
    assert single_summary["generated"] == 2

    dlc_root = tmp_path / "dlc"
    dlc_root.mkdir()
    library_song = _write_pack(dlc_root, "library.feedpak", arrangements, song_timeline_sections=[0, 5])

    client = _client_for(dlc_root)
    resp = client.post(f"/api/plugins/{routes.PLUGIN_ID}/generate-library", json={"force": True})
    assert resp.status_code == 200
    assert resp.json()["generated"] == 2

    for rel in ("arrangements/lead.json", "arrangements/bass.json"):
        assert _phrase_boundaries(single_song, rel) == _phrase_boundaries(library_song, rel)


def test_generate_library_route_stops_at_time_budget_and_reports_time_limit_reached(tmp_path):
    # Issue #40 (DoS mitigation): a sweep that exceeds max_processing_seconds
    # must stop early rather than run unbounded, and must say so in the
    # response instead of looking like a normal, complete sweep.
    #
    # Uses a real (small) sleep per pack rather than mocking time.monotonic:
    # generate_library's budget check shares the process-global time module
    # with anyio/httpx's own internals (patch.object(routes.time, ...)
    # patches the *actual* time module, not a copy), and freezing/jumping it
    # wedges TestClient's underlying event loop instead of just the code
    # under test.
    arrangements = [
        ("arrangements/lead.json", _arrangement(_simple_notes(0, 10, step=0.5), sections=[{"time": 0}, {"time": 4}])),
    ]
    for name in ("a.feedpak", "b.feedpak", "c.feedpak"):
        _write_pack(tmp_path, name, arrangements, song_timeline_sections=[0, 5])

    client = _client_for(tmp_path)
    real_generate_one = routes._generate_one

    def slow_generate_one(*args, **kwargs):
        import time as _time
        _time.sleep(0.6)
        return real_generate_one(*args, **kwargs)

    # Each pack takes ~0.6s; a 1s budget lets at most one pack finish before
    # the per-pack check (ahead of the second pack) trips the cutoff.
    with patch.object(routes, "_generate_one", side_effect=slow_generate_one):
        resp = client.post(
            f"/api/plugins/{routes.PLUGIN_ID}/generate-library",
            json={"force": True, "max_processing_seconds": 1},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["time_limit_reached"] is True
    assert body["scanned"] < 3
    assert body["generated"] < 3


def test_generate_library_route_completes_normally_within_time_budget(tmp_path):
    # Sanity check for the above: with a generous budget and time.monotonic
    # unpatched, both packs are actually processed and time_limit_reached
    # is False -- the flag isn't stuck on or spuriously tripped.
    arrangements = [
        ("arrangements/lead.json", _arrangement(_simple_notes(0, 10, step=0.5), sections=[{"time": 0}, {"time": 4}])),
    ]
    _write_pack(tmp_path, "a.feedpak", arrangements, song_timeline_sections=[0, 5])
    _write_pack(tmp_path, "b.feedpak", arrangements, song_timeline_sections=[0, 5])

    client = _client_for(tmp_path)
    resp = client.post(
        f"/api/plugins/{routes.PLUGIN_ID}/generate-library",
        json={"force": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["time_limit_reached"] is False
    assert body["scanned"] == 2
    assert body["generated"] == 2


def test_generate_song_records_unexpected_error_and_generates_remaining_arrangements(tmp_path):
    # A JSON decode failure on one arrangement -- NOT an HTTPException -- must
    # not abort the whole song: the remaining arrangement still gets
    # generated, and the failure is recorded per-arrangement instead of
    # propagating out of _generate_song as an unhandled exception.
    arrangements = [
        ("arrangements/bad.json", {}),  # placeholder; overwritten with invalid JSON below
        ("arrangements/lead.json", _arrangement(_simple_notes(0, 10, step=0.5), sections=[{"time": 0}, {"time": 4}])),
    ]
    pack_dir = _write_pack(tmp_path, "song.feedpak", arrangements, song_timeline_sections=[0, 5])
    (pack_dir / "arrangements/bad.json").write_text("{not valid json")

    summary = routes._generate_song(pack_dir, n_levels=4, force=True, log=_TEST_LOG)

    assert summary["generated"] == 1
    assert summary["failed"] == 1
    bad_result, good_result = summary["arrangements"]
    assert bad_result["arrangement_index"] == 0
    assert "error" in bad_result
    assert good_result.get("ok") is True
    assert _phrase_boundaries(pack_dir, "arrangements/lead.json")


# ---------------------------------------------------------------------------
# link-next (`ln`) integrity and chord-onset anchors (issue #68)
# ---------------------------------------------------------------------------

def test_prune_note_for_level_clears_ln_when_slide_is_gated_out():
    # sl=9 is a real pitched slide destination; ln announces it. The sl/slu
    # gate is 0.85 -- below it the slide itself gets stripped to -1, and ln
    # must go with it (otherwise the highway would suppress the next note's
    # gem for a slide that no longer exists at this tier).
    note = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "sl": 9, "ln": True}
    pruned = routes._prune_note_for_level(note, diff_percent=0.5)
    assert pruned["sl"] == -1
    assert "ln" not in pruned


def test_prune_note_for_level_keeps_ln_when_slide_survives():
    note = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "sl": 9, "ln": True}
    pruned = routes._prune_note_for_level(note, diff_percent=0.9)  # above the 0.85 gate
    assert pruned["sl"] == 9
    assert pruned["ln"] is True


def test_prune_note_for_level_leaves_letring_only_ln_to_the_target_check():
    # No sl/slu at all -- a letRing-only link has no technique to strip;
    # _prune_note_for_level must not touch it (target survival is
    # _clear_orphaned_link_next's job).
    note = {"t": 0.0, "s": 2, "f": 5, "sus": 1.0, "ln": True}
    pruned = routes._prune_note_for_level(note, diff_percent=0.1)
    assert pruned["ln"] is True


def test_clear_orphaned_link_next_drops_ln_with_no_target_on_same_string():
    notes = [{"t": 0.0, "s": 2, "f": 5, "ln": True}]
    routes._clear_orphaned_link_next(notes)
    assert "ln" not in notes[0]


def test_clear_orphaned_link_next_keeps_ln_when_target_follows_on_same_string():
    notes = [
        {"t": 0.0, "s": 2, "f": 5, "ln": True},
        {"t": 0.5, "s": 2, "f": 7},
    ]
    routes._clear_orphaned_link_next(notes)
    assert notes[0]["ln"] is True


def test_clear_orphaned_link_next_ignores_a_later_note_on_a_different_string():
    notes = [
        {"t": 0.0, "s": 2, "f": 5, "ln": True},
        {"t": 0.5, "s": 3, "f": 7},  # different string -- not a valid target
    ]
    routes._clear_orphaned_link_next(notes)
    assert "ln" not in notes[0]


def test_notes_for_level_drops_ln_when_arpeggio_truncation_removes_the_target():
    # letRing-style ln (no sl/slu) so only target survival is exercised --
    # keep_n at level 0 of a 3-note arpeggio keeps just the anchor note,
    # truncating away the note it was linked into.
    groups = [{
        "level": 0,
        "type": "arpeggio",
        "notes": [
            {"t": 0.0, "s": 2, "f": 5, "sus": 0, "ln": True},
            {"t": 0.1, "s": 2, "f": 9, "sus": 0},
            {"t": 0.2, "s": 2, "f": 12, "sus": 0},
        ],
    }]
    notes, _chords = routes._notes_for_level(groups, level=0, max_level=3)
    assert len(notes) == 1
    assert "ln" not in notes[0], "target was truncated away -- ln must not survive"


def test_notes_for_level_keeps_ln_when_arpeggio_target_survives():
    groups = [{
        "level": 0,
        "type": "arpeggio",
        "notes": [
            {"t": 0.0, "s": 2, "f": 5, "sus": 0, "ln": True},
            {"t": 0.1, "s": 2, "f": 9, "sus": 0},
            {"t": 0.2, "s": 2, "f": 12, "sus": 0},
        ],
    }]
    # level=2 of max_level=4 keeps 2 of the 3 notes -- the linked target
    # (t=0.1) survives alongside the anchor note.
    notes, _chords = routes._notes_for_level(groups, level=2, max_level=4)
    assert [n["t"] for n in notes] == [0.0, 0.1]
    assert notes[0]["ln"] is True


def test_notes_for_anchors_includes_chord_constituents_at_chord_onset():
    chord = {"t": 1.0, "notes": [{"s": 5, "f": 3}, {"s": 4, "f": 5}]}
    combined = routes._notes_for_anchors([{"t": 0.5, "f": 2}], [chord])
    assert {"t": 0.5, "f": 2} in combined
    assert {"t": 1.0, "f": 3} in combined
    assert {"t": 1.0, "f": 5} in combined
    assert len(combined) == 3


def test_chord_only_top_tier_gets_anchors():
    # No standalone notes at all -- every event is a chord constituent, so
    # a top tier that keeps chords intact (see _notes_for_level) must still
    # get fret anchors from _notes_for_anchors, not an empty list.
    chords = [
        {"t": t, "notes": [{"s": 5, "f": 3}, {"s": 4, "f": 5}, {"s": 3, "f": 5}, {"s": 2, "f": 4}]}
        for t in (0.0, 0.5, 1.0, 1.5)
    ]
    arr = _arrangement([], chords=chords)
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases
    top = phrases[0]["levels"][-1]
    assert top["notes"] == []
    assert len(top["chords"]) == 4
    assert top["anchors"], "a chord-only top tier must still get fret anchors"


def test_generated_anchors_are_time_monotonic_with_chord_constituents():
    chord = {"t": 4.0, "notes": [{"s": 5, "f": 8}, {"s": 4, "f": 10}]}
    notes = _simple_notes(0, 4, step=0.5, fret=3)
    combined = routes._notes_for_anchors(notes, [chord])
    beat_times = [i * 0.5 for i in range(12)]
    anchors = routes._generate_anchors(combined, beat_times)
    assert anchors
    times = [a["time"] for a in anchors]
    assert times == sorted(times), "anchors must be strictly time-ordered"


# ---------------------------------------------------------------------------
# Phrase boundary validation, anchor containment, and chord-sustain-aware
# duration (issue #69)
# ---------------------------------------------------------------------------

def test_valid_section_times_drops_non_finite_and_non_numeric():
    assert routes._valid_section_times(
        [0, float("nan"), float("inf"), float("-inf"), "not-a-number", None, 5]
    ) == [0.0, 5.0]


def test_valid_section_times_clamps_negatives_to_zero():
    assert routes._valid_section_times([-5, 2, -1]) == [0.0, 2.0]


def test_valid_section_times_dedupes_and_sorts():
    assert routes._valid_section_times([3, 0, 3, 0, 6]) == [0.0, 3.0, 6.0]


def test_valid_section_times_preserves_a_real_pickup_offset():
    # A pickup (anacrusis) section legitimately starts after t=0 -- only
    # genuinely negative/non-finite input gets clamped/dropped.
    assert routes._valid_section_times([0.3, 4.0, 8.0]) == [0.3, 4.0, 8.0]


def test_generate_phrases_survives_malformed_canonical_section_times():
    arr = _arrangement(_simple_notes(0, 10, step=0.5))
    phrases = routes.generate_phrases_for_arrangement(
        arr, n_levels=4, section_times=[0, float("nan"), 0, -3, 5, float("inf")]
    )
    assert phrases
    bounds = [(p["start_time"], p["end_time"]) for p in phrases]
    for t0, t1 in bounds:
        assert t1 > t0, "no window may be zero-length or reversed"
    assert bounds == sorted(bounds)


def test_generate_phrases_own_sections_reject_degenerate_duplicate_boundary():
    # The `elif sections:` (per-arrangement) path had no t1>t0 guard at all
    # before #69 -- a duplicate boundary silently produced a zero-length
    # window.
    arr = _arrangement(
        _simple_notes(0, 10, step=0.5),
        sections=[{"time": 0}, {"time": 3}, {"time": 3}, {"time": 6}],
    )
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases
    for p in phrases:
        assert p["end_time"] > p["start_time"]


def test_generate_phrases_with_a_pickup_first_section():
    arr = _arrangement(_simple_notes(0, 10, step=0.5), sections=[{"time": 0.3}, {"time": 5}])
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases
    assert phrases[0]["start_time"] == 0.3
    assert phrases[0]["end_time"] == 5.0


def test_anchors_never_precede_their_own_phrase():
    # The note's enclosing beat starts before the phrase boundary even
    # though the note itself is inside the phrase -- the anchor must be
    # clamped to the phrase start, not emitted at the earlier beat time.
    notes = [{"t": 2.05, "s": 2, "f": 5, "sus": 0}]
    beat_times = [1.98, 2.48, 2.98]
    anchors = routes._generate_anchors(notes, beat_times, phrase_start=2.0, phrase_end=2.5)
    assert anchors
    assert all(a["time"] >= 2.0 for a in anchors)


def test_anchors_excluded_when_beat_window_is_entirely_outside_the_phrase():
    notes = [{"t": 2.05, "s": 2, "f": 5, "sus": 0}]
    beat_times = [1.98, 2.48]
    anchors = routes._generate_anchors(notes, beat_times, phrase_start=5.0, phrase_end=6.0)
    assert anchors == []


def test_generated_anchors_stay_within_their_phrase_across_the_full_pipeline():
    arr = _arrangement(
        _technical_notes(0, 12, step=0.1),
        sections=[{"time": 0}, {"time": 4}, {"time": 8}],
    )
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases
    for p in phrases:
        for lvl in p["levels"]:
            for a in lvl["anchors"]:
                assert p["start_time"] - 1e-6 <= a["time"] < p["end_time"] + 1e-6


def test_duration_includes_chord_constituent_sustain():
    chord = {"t": 5.0, "notes": [{"s": 5, "f": 3, "sus": 3.0}, {"s": 4, "f": 5, "sus": 0.5}]}
    arr = _arrangement(_simple_notes(0, 5, step=0.5), chords=[chord])
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4, section_times=[0, 5])
    assert phrases
    last = phrases[-1]
    # duration must reflect the chord's longest constituent sustain (3.0s),
    # not the old flat +0.1 -- so the trailing window extends to ~8.0, not ~5.001.
    assert last["end_time"] >= 8.0


# ---------------------------------------------------------------------------
# Reporting actual generated depth and collapsing duplicate tiers (issue #70)
# ---------------------------------------------------------------------------

def test_canonical_note_for_compare_drops_prune_sentinel_defaults():
    pruned = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "sl": -1, "slu": -1, "bn": 0, "bt": 0}
    raw = {"t": 0.0, "s": 2, "f": 5, "sus": 0}
    assert routes._canonical_note_for_compare(pruned) == routes._canonical_note_for_compare(raw)


def test_canonical_note_for_compare_keeps_a_real_slide_destination():
    with_slide = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "sl": 9}
    without = {"t": 0.0, "s": 2, "f": 5, "sus": 0}
    assert routes._canonical_note_for_compare(with_slide) != routes._canonical_note_for_compare(without)


def test_collapse_identical_levels_merges_duplicate_adjacent_tiers():
    levels = [
        {"difficulty": 0, "notes": [{"t": 0, "s": 2, "f": 3, "sl": -1, "slu": -1}],
         "chords": [], "anchors": [], "handshapes": []},
        {"difficulty": 1, "notes": [{"t": 0, "s": 2, "f": 3}], "chords": [], "anchors": [], "handshapes": []},
        {"difficulty": 2, "notes": [{"t": 0, "s": 2, "f": 5}], "chords": [], "anchors": [], "handshapes": []},
    ]
    collapsed = routes._collapse_identical_levels(levels)
    assert len(collapsed) == 2
    # Tier numbers are kept, not renumbered: the second level's content
    # starts at tier 2, and a reader needs that to map the slider correctly.
    assert [lvl["difficulty"] for lvl in collapsed] == [0, 2]  # nosec B101 - pytest assertion
    # the cleaner (un-pruned) representative of the duplicate run survives
    assert collapsed[0]["notes"] == [{"t": 0, "s": 2, "f": 3}]
    assert collapsed[1]["notes"] == [{"t": 0, "s": 2, "f": 5}]


def test_collapse_identical_levels_keeps_distinct_tiers_untouched():
    levels = [
        {"difficulty": 0, "notes": [{"t": 0}], "chords": [], "anchors": [], "handshapes": []},
        {"difficulty": 1, "notes": [{"t": 0}, {"t": 1}], "chords": [], "anchors": [], "handshapes": []},
    ]
    collapsed = routes._collapse_identical_levels(levels)
    assert len(collapsed) == 2


def test_repetitive_fretted_phrase_collapses_duplicate_tiers():
    # Equal-score-ish content: identical string/fret/no techniques,
    # evenly spaced. The per-phrase floor could still nominally split this
    # into more tiers than there's real variation for -- no two adjacent
    # tiers may describe the same notes.
    notes = [{"t": round(i * 0.25, 3), "s": 2, "f": 3, "sus": 0} for i in range(60)]
    arr = _arrangement(notes, n_beats=60)
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=6)
    assert phrases
    levels = phrases[0]["levels"]
    for a, b in pairwise(levels):
        a_notes = [routes._canonical_note_for_compare(n) for n in a["notes"]]
        b_notes = [routes._canonical_note_for_compare(n) for n in b["notes"]]
        assert a_notes != b_notes or a["chords"] != b["chords"]
    _assert_on_tier_scale(phrases[0], n_levels=6)


def test_keys_fixed_depth_collapses_duplicate_tiers():
    # Keys always requests the full n_levels regardless of content
    # variation (generate_phrases_for_arrangement's is_keys branch) --
    # a uniform, unvarying keys pattern must not ship duplicate tiers
    # just because the fixed depth was asked for.
    notes = [{"t": round(i * 0.5, 3), "s": 2, "f": 0, "sus": 0} for i in range(20)]
    arr = {
        "type": "keys", "name": "keys", "notes": notes, "chords": [],
        "beats": [{"time": i * 0.5} for i in range(40)], "sections": [], "tuning": [],
    }
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases
    levels = phrases[0]["levels"]
    for a, b in pairwise(levels):
        assert a["notes"] != b["notes"] or a["chords"] != b["chords"]
    _assert_on_tier_scale(phrases[0], n_levels=4)
    assert len(levels) < 4, (  # nosec B101 - pytest assertion
        "keys must not always ship the full requested depth when tiers are duplicates"
    )


def test_shallow_phrase_reports_actual_depth_not_the_requested_cap():
    # A near-minimal phrase (just above MIN_EVENTS_FOR_GENERATION) has too
    # little content to fill out a deep ladder.
    notes = _simple_notes(0, 4, step=0.5, fret=3)  # 8 events, right at the floor
    arr = _arrangement(notes)
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=8)
    assert phrases
    assert len(phrases[0]["levels"]) < 8  # nosec B101 - pytest assertion


def test_empty_section_phrase_still_reports_zero_depth_after_collapse():
    arr = _arrangement(_simple_notes(0, 2, step=0.2, fret=3))
    phrases = routes.generate_phrases_for_arrangement(
        arr, n_levels=4, section_times=[0, 2, 6]
    ) or []
    empty_phrase = phrases[2]
    assert empty_phrase["max_difficulty"] == 0
    assert len(empty_phrase["levels"]) == 1
    assert empty_phrase["levels"][0]["notes"] == []


def test_generate_one_reports_requested_cap_separately_from_actual_depth():
    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    fake_arr = {"type": "lead", "phrases": None}
    fake_phrases = [
        {"start_time": 0.0, "end_time": 2.0, "max_difficulty": 1, "levels": []},
        {"start_time": 2.0, "end_time": 5.0, "max_difficulty": 3, "levels": []},
    ]
    with patch.object(routes, "_lock_for_pack", return_value=_Lock()), \
         patch.object(routes, "_load_manifest_and_arrangement",
                      return_value=("arrangements/lead.json", fake_arr, None)), \
         patch.object(routes, "_instrument_kind", return_value="fretted"), \
         patch.object(routes, "generate_phrases_for_arrangement", return_value=fake_phrases), \
         patch.object(routes, "_write_member_bytes"):
        result = routes._generate_one(
            Path("unused"), 0, n_levels=6, force=False, log=_TEST_LOG
        )

    assert result["requested_levels"] == 6
    # actual max across the generated phrases, not the old n_levels - 1 (5)
    assert result["max_difficulty"] == 3


# ---------------------------------------------------------------------------
# Tempo-relative sequential density, replacing the fixed-index-neighborhood
# approach (issue #71)
# ---------------------------------------------------------------------------

def test_sequential_density_scores_compressed_pattern_higher_than_stretched():
    tempo = routes._TempoParams(beat_interval=0.5)
    compressed_times = [round(i * 0.1, 3) for i in range(11)]  # 11 onsets in ~1 second
    stretched_times = [round(i * 2.0, 3) for i in range(11)]   # 11 onsets over ~20 seconds
    compressed_density = routes._sequential_density(compressed_times, 5, tempo)
    stretched_density = routes._sequential_density(stretched_times, 5, tempo)
    assert compressed_density > stretched_density


def test_sequential_density_normalizes_equivalent_patterns_across_bpm():
    # The same musical pattern -- one onset per beat -- must score the same
    # density whether the song is slow or fast: it's the same rhythmic
    # complexity either way, just at a different absolute tempo. Sparse
    # enough that neither density saturates to 1.0 -- a saturated pair
    # would pass this assertion even if tempo normalization were broken
    # (e.g. a fixed-size window ignoring beat_interval entirely), since
    # both sides would coincidentally hit the same ceiling regardless.
    slow_tempo = routes._TempoParams(beat_interval=1.0)  # 60 BPM
    fast_tempo = routes._TempoParams(beat_interval=0.5)  # 120 BPM
    slow_times = [round(i * 1.0, 3) for i in range(9)]   # one onset per beat at 60 BPM
    fast_times = [round(i * 0.5, 3) for i in range(9)]   # one onset per beat at 120 BPM
    slow_density = routes._sequential_density(slow_times, 4, slow_tempo)
    fast_density = routes._sequential_density(fast_times, 4, fast_tempo)
    assert 0 < slow_density < 1, "test fixture must not saturate, or it can't detect broken normalization"
    assert slow_density == fast_density


def test_sequential_density_window_scales_with_tempo_not_a_fixed_index_count():
    # A pattern that's dense in BEATS but sparse in raw event count must
    # still register as dense -- the window is sized in beats
    # (_DENSITY_WINDOW_BEATS), not a fixed number of neighboring groups
    # (the pre-#71 approach), so a handful of onsets at a slow tempo can
    # still saturate density the same way many onsets do at a fast tempo.
    slow_tempo = routes._TempoParams(beat_interval=2.0)  # 30 BPM -- a wide window in seconds
    times = [0.0, 2.0, 4.0]  # one onset per beat, only 3 total onsets
    # The middle onset's window (±2 beats = ±4s) covers all three onsets.
    density = routes._sequential_density(times, 1, slow_tempo)
    assert density == min(1.0, 3 / routes._DENSITY_SATURATION_ONSETS)


def test_compressed_fretted_notes_score_higher_density_than_stretched():
    tempo = routes._TempoParams(beat_interval=0.5)
    compressed = [{"time": round(i * 0.1, 3), "notes": [{"s": 2, "f": 3, "sus": 0}]} for i in range(11)]
    stretched = [{"time": round(i * 2.0, 3), "notes": [{"s": 2, "f": 3, "sus": 0}]} for i in range(11)]
    routes._score_groups(compressed, n_strings=6, tempo=tempo)
    routes._score_groups(stretched, n_strings=6, tempo=tempo)
    # A middle group (unaffected by start/end edge effects) scores
    # meaningfully higher when packed into ~1 second than spread across
    # ~20 seconds -- previously both scored identically (issue #71).
    assert compressed[5]["cost"] > stretched[5]["cost"]


def test_compressed_keys_notes_score_higher_density_than_stretched():
    tempo = routes._TempoParams(beat_interval=0.5)
    compressed = [{"time": round(i * 0.1, 3), "notes": [{"s": 2, "f": 0, "sus": 0}]} for i in range(11)]
    stretched = [{"time": round(i * 2.0, 3), "notes": [{"s": 2, "f": 0, "sus": 0}]} for i in range(11)]
    routes._score_groups_keys(compressed, tempo=tempo)
    routes._score_groups_keys(stretched, tempo=tempo)
    assert compressed[5]["cost"] > stretched[5]["cost"]


def test_wide_chord_does_not_inflate_density_beyond_a_single_note_group():
    # Simultaneous polyphony (a wide chord) must not count as "denser" than
    # a single note at the same onset -- density counts distinct onsets
    # (groups), never each group's own note count, since polyphony is
    # already scored separately (fretting/string_shape here, `poly` in the
    # keys path). _sequential_density's signature only ever takes onset
    # TIMES, never per-group note counts, so a group's polyphony is
    # structurally invisible to it -- extracting the times from a
    # 6-note-wide chord group gives the exact same density as a run of
    # single-note groups at the same onsets.
    tempo = routes._TempoParams(beat_interval=0.5)
    single_note_times = [0.0, 0.5, 1.0]
    wide_chord_groups = [
        {"time": 0.0, "notes": [{"s": 2, "f": 3, "sus": 0}]},
        {"time": 0.5, "notes": [{"s": s, "f": 3, "sus": 0} for s in range(6)]},
        {"time": 1.0, "notes": [{"s": 2, "f": 3, "sus": 0}]},
    ]
    wide_chord_times = [g["time"] for g in wide_chord_groups]
    assert wide_chord_times == single_note_times
    single_density = routes._sequential_density(single_note_times, 1, tempo)
    chord_density = routes._sequential_density(wide_chord_times, 1, tempo)
    assert single_density == chord_density


# ---------------------------------------------------------------------------
# PR #76 review follow-ups: cross-phrase `ln` survivorship (#68) and
# generate_library's canonical-section-times failure isolation (#67)
# ---------------------------------------------------------------------------

def test_global_link_next_survivors_marks_notes_with_a_later_same_string_note():
    groups_all = [
        {"time": 0.0, "notes": [{"t": 0.0, "s": 2, "ln": True}]},
        {"time": 5.0, "notes": [{"t": 5.0, "s": 2}]},  # same string, later -- a genuine target
    ]
    survivors = routes._global_link_next_survivors(groups_all)
    assert id(groups_all[0]["notes"][0]) in survivors
    assert id(groups_all[1]["notes"][0]) not in survivors  # last note on its string has no target


def test_notes_for_level_keeps_top_tier_ln_when_target_is_in_the_next_phrase():
    # Phrase A's own groups only contain the FIRST note; its real target
    # (same string, later) lives in phrase B and is therefore invisible to
    # a phrase-local check alone -- global survivorship must still
    # preserve it at the (unpruned) top tier.
    groups_all = [
        {"time": 0.0, "type": "note", "notes": [{"t": 0.0, "s": 2, "f": 5, "ln": True}], "level": 0},
        {"time": 5.0, "type": "note", "notes": [{"t": 5.0, "s": 2, "f": 7}], "level": 0},
    ]
    keep_ids = routes._global_link_next_survivors(groups_all)
    phrase_a_groups = [groups_all[0]]  # phrase A's window excludes phrase B's group
    notes, _chords = routes._notes_for_level(
        phrase_a_groups, level=0, max_level=0, link_next_keep_ids=keep_ids,
    )
    assert notes[0]["ln"] is True


def test_notes_for_level_still_drops_ln_with_no_target_anywhere_even_with_keep_ids():
    groups_all = [
        {"time": 0.0, "type": "note", "notes": [{"t": 0.0, "s": 2, "f": 5, "ln": True}], "level": 0},
    ]
    keep_ids = routes._global_link_next_survivors(groups_all)
    notes, _chords = routes._notes_for_level(
        groups_all, level=0, max_level=0, link_next_keep_ids=keep_ids,
    )
    assert "ln" not in notes[0]


def test_notes_for_level_keep_ids_never_matches_a_pruned_lower_tier_copy():
    # keep_ids holds the object ids of the ORIGINAL grouped notes. A lower
    # (non-top) tier always emits fresh dict() copies (_prune_note_for_level),
    # so keep_ids must never accidentally preserve an orphaned ln there --
    # this is what makes passing keep_ids to every tier safe by construction.
    groups_all = [{
        "time": 0.0, "type": "arpeggio", "level": 0,
        "notes": [
            {"t": 0.0, "s": 2, "f": 5, "sus": 0, "ln": True},
            {"t": 0.1, "s": 2, "f": 9, "sus": 0},
        ],
    }]
    keep_ids = routes._global_link_next_survivors(groups_all)
    # level=0 of max_level=3 truncates the arpeggio down to just the anchor
    # note, dropping its target -- a lower-tier copy, so keep_ids must not
    # rescue it.
    notes, _chords = routes._notes_for_level(
        groups_all, level=0, max_level=3, link_next_keep_ids=keep_ids,
    )
    assert len(notes) == 1
    assert "ln" not in notes[0]


def test_generate_phrases_preserves_ln_across_a_phrase_boundary():
    # letRing-style ln (no sl/slu) on the last note before a section
    # boundary; its real target sits just after the boundary, in the next
    # phrase. A phrase-local-only check would incorrectly clear it.
    notes = _simple_notes(0, 10, step=0.5, string=2, fret=3)
    for n in notes:
        if n["t"] == 4.5:
            n["ln"] = True
    arr = _arrangement(notes, sections=[{"time": 0}, {"time": 5}])
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=2)
    assert phrases
    first_phrase = phrases[0]
    top_level = first_phrase["levels"][-1]
    tagged = [n for n in top_level["notes"] if n["t"] == 4.5]
    assert tagged and tagged[0].get("ln") is True


def test_generate_library_route_records_canonical_section_times_failure_and_continues(tmp_path):
    # Even if _canonical_section_times() raises something outside its own
    # internal ValueError handling, one bad pack's boundary computation
    # must not abort songs not yet visited in the sweep (matches the
    # load_manifest guard immediately above it).
    dlc_root = tmp_path / "dlc"
    dlc_root.mkdir()
    arrangements = [("arrangements/lead.json", _arrangement(_simple_notes(0, 10, step=0.5)))]
    _write_pack(dlc_root, "bad.feedpak", arrangements)
    _write_pack(dlc_root, "good.feedpak", arrangements, song_timeline_sections=[0, 5])

    client = _client_for(dlc_root)
    with patch.object(
        routes, "_canonical_section_times",
        side_effect=[RuntimeError("corrupt archive"), []],
    ):
        resp = client.post(f"/api/plugins/{routes.PLUGIN_ID}/generate-library", json={"force": True})

    assert resp.status_code == 200
    data = resp.json()
    assert data["generated"] == 1
    assert any("corrupt archive" in f.get("error", "") for f in data["failed"])


# ── Issue #73: evidence-gated implicit arpeggio grouping ────────────────────
#
# Before this fix, ANY different-string notes landing inside the grouping
# time/fret window became an "arpeggio" with no further evidence -- a fast
# cross-string scale run read identically to a genuine broken chord, and the
# bottom-tier reduction (_notes_for_level's arpeggio branch) collapsed either
# one down to a single note (really just the note on the highest string
# index -- a position convention, not a proven harmonic root or bass note).
# These tests cover the acceptance criteria: stronger evidence required for
# "arpeggio", melodic sequences preserved when evidence is absent, position
# terminology (exercised via _group_anchor_note behavior), and inversions /
# cross-string scales / linked arpeggios / unusual tunings.
#
# Fixture note: chord-template `frets` lists follow feedpak's own wire
# convention -- index 0 = lowest-pitched string (feedpak-v1.md §6.2/§6.6,
# mirrored in song.py's _TUNING_BASE_MIDI) -- so e.g. a real open-C voicing
# (x-3-2-0-1-0) is `[-1, 3, 2, 0, 1, 0]`, not `[-1, 0, 1, 0, 2, 3]`.

def test_classify_cluster_with_no_evidence_is_a_run_not_an_arpeggio():
    # Two different-string notes close in time, no sustain overlap, no
    # authored hand-shape or chord-template evidence: a fast cross-string
    # scale run, not a proven broken chord.
    cluster = [
        {"t": 0.0, "s": 0, "f": 3, "sus": 0},
        {"t": 0.02, "s": 1, "f": 5, "sus": 0},
    ]
    assert routes._classify_cluster(cluster) == "run"


def test_classify_cluster_with_overlapping_sustain_is_an_arpeggio():
    # First note rings past the second note's onset -- the notes were left
    # to sound together, the hallmark of a broken chord.
    cluster = [
        {"t": 0.0, "s": 0, "f": 3, "sus": 0.3},
        {"t": 0.05, "s": 1, "f": 5, "sus": 0},
    ]
    assert routes._classify_cluster(cluster) == "arpeggio"


def test_classify_cluster_covered_by_authored_hand_shape_is_an_arpeggio():
    # Authored linkage: the chart's own hand-shape window covers both
    # onsets, even though nothing overlaps and no chord template matches.
    cluster = [
        {"t": 1.00, "s": 2, "f": 2, "sus": 0},
        {"t": 1.05, "s": 4, "f": 0, "sus": 0},
    ]
    hand_shapes = [{"chord_id": 0, "start_time": 0.9, "end_time": 1.2, "arp": True}]
    assert routes._classify_cluster(cluster, hand_shapes=hand_shapes) == "arpeggio"


def test_classify_cluster_outside_hand_shape_window_is_unaffected():
    cluster = [
        {"t": 2.00, "s": 2, "f": 2, "sus": 0},
        {"t": 2.05, "s": 4, "f": 0, "sus": 0},
    ]
    hand_shapes = [{"chord_id": 0, "start_time": 0.9, "end_time": 1.2, "arp": True}]
    assert routes._classify_cluster(cluster, hand_shapes=hand_shapes) == "run"


def test_classify_cluster_matching_chord_template_shape_is_an_arpeggio():
    # Chord identity: the cluster's exact per-string frets appear in an
    # authored chord template -- a real open-C voicing (x-3-2-0-1-0, low-
    # string-first) -- covering 3 of its 5 used strings (a meaningful
    # share, not a coincidental fragment; see
    # test_classify_cluster_rejects_a_coincidental_partial_chord_template_match
    # below), regardless of timing evidence.
    cluster = [
        {"t": 0.0, "s": 1, "f": 3, "sus": 0},
        {"t": 0.03, "s": 2, "f": 2, "sus": 0},
        {"t": 0.06, "s": 4, "f": 1, "sus": 0},
    ]
    chord_templates = [{"name": "C", "frets": [-1, 3, 2, 0, 1, 0]}]
    assert routes._classify_cluster(cluster, chord_templates=chord_templates) == "arpeggio"


def test_classify_cluster_partial_chord_template_mismatch_stays_a_run():
    # Same strings, but one fret disagrees with every template -- not a
    # real chord-shape match.
    cluster = [
        {"t": 0.0, "s": 1, "f": 3, "sus": 0},
        {"t": 0.03, "s": 2, "f": 9, "sus": 0},  # doesn't match the template's fret 2
    ]
    chord_templates = [{"name": "C", "frets": [-1, 3, 2, 0, 1, 0]}]
    assert routes._classify_cluster(cluster, chord_templates=chord_templates) == "run"


def test_classify_cluster_rejects_a_coincidental_partial_chord_template_match():
    # Regression (PR #100 review): a 2-note cluster that happens to
    # coincidentally match 2 of a 6-string open-C's 5 used strings must
    # NOT read as chord identity -- that's exactly the false-arpeggio class
    # issue #73 set out to eliminate. A real open-C is x-3-2-0-1-0
    # (low-string-first); this cluster is just the A-string/D-string notes
    # (fret 3 / fret 2) landing on 2 of that template's 5 positions.
    cluster = [
        {"t": 0.0, "s": 1, "f": 3, "sus": 0},
        {"t": 0.02, "s": 2, "f": 2, "sus": 0},
    ]
    chord_templates = [{"name": "C", "frets": [-1, 3, 2, 0, 1, 0]}]
    assert routes._classify_cluster(cluster, chord_templates=chord_templates) == "run"


def test_classify_cluster_single_note_is_plain_note():
    assert routes._classify_cluster([{"t": 0.0, "s": 0, "f": 3}]) == "note"


def test_classify_cluster_works_for_unusual_tunings_with_more_strings():
    # A 7-string extended-range chart: chord-template matching must not
    # assume a fixed 6-string layout -- string index 6 is valid here. The
    # template is an illustrative 2-string shape (not a claimed real voicing
    # of any named tuning/chord) matched in full (2 of its 2 used strings),
    # which is meaningful-share evidence regardless of chord size.
    cluster = [
        {"t": 0.0, "s": 6, "f": 0, "sus": 0},
        {"t": 0.02, "s": 3, "f": 2, "sus": 0},
    ]
    chord_templates = [{"name": "fixture-2-string-shape", "frets": [-1, -1, -1, 2, -1, -1, 0]}]
    assert routes._classify_cluster(cluster, chord_templates=chord_templates) == "arpeggio"


def test_classify_cluster_rejects_a_small_fraction_of_a_larger_unusual_tuning_template():
    # Same 7-string layout, but now the matched 2 notes are only a small
    # fraction of a larger (5-used-string) template -- must not pass on
    # subset coincidence alone, same as the 6-string case above.
    cluster = [
        {"t": 0.0, "s": 6, "f": 0, "sus": 0},
        {"t": 0.02, "s": 3, "f": 2, "sus": 0},
    ]
    chord_templates = [{
        "name": "fixture-5-string-shape",
        "frets": [-1, 1, 2, 2, 3, -1, 0],
    }]
    assert routes._classify_cluster(cluster, chord_templates=chord_templates) == "run"


def test_group_notes_threads_hand_shapes_and_chord_templates_into_classification():
    notes = [
        {"t": 0.0, "s": 2, "f": 2, "sus": 0},
        {"t": 0.05, "s": 4, "f": 0, "sus": 0},
    ]
    hand_shapes = [{"chord_id": 0, "start_time": 0.0, "end_time": 0.3, "arp": True}]
    groups = routes._group_notes(
        notes, [], time_window_ms=150, hand_shapes=hand_shapes, chord_templates=[],
    )
    assert [g["type"] for g in groups] == ["arpeggio"]


def test_notes_for_level_preserves_a_melodic_run_instead_of_collapsing_to_one_note():
    # A 6-note cross-string scale run with no arpeggio evidence. Under the
    # old behavior this cluster would have been labeled "arpeggio" and the
    # bottom tier would keep only the single note on the group's highest
    # string index. As a "run", the bottom tier should still thin it, but
    # keep more than one note so the melodic sequence survives.
    ns = [{"t": i * 0.02, "s": i % 6, "f": i + 1, "sus": 0} for i in range(6)]
    groups = [{"type": "run", "level": 0, "time": 0.0, "chord": None, "notes": ns}]

    notes, chords = routes._notes_for_level(groups, level=0, max_level=3)

    assert chords == []
    assert len(notes) > 1, "a melodic run must not collapse to a single presumed-anchor note"
    assert len(notes) < len(ns), "the bottom tier still thins the run"


def test_notes_for_level_run_thinning_preserves_contour_not_just_a_prefix():
    # _evenly_sample should span the run (first + last + spread), not just
    # take a fixed-length prefix the way naive truncation would.
    ns = [{"t": i * 0.02, "s": 0, "f": i, "sus": 0} for i in range(9)]
    groups = [{"type": "run", "level": 1, "time": 0.0, "chord": None, "notes": ns}]

    notes, chords = routes._notes_for_level(groups, level=1, max_level=3)

    frets = sorted(n["f"] for n in notes)
    assert frets[0] == 0, "the run's first note should survive thinning"
    assert frets[-1] == 8, "the run's last note should survive thinning"


def test_notes_for_level_run_at_top_tier_keeps_every_note_untouched():
    ns = [{"t": i * 0.02, "s": i % 6, "f": i + 1, "sus": 0} for i in range(4)]
    groups = [{"type": "run", "level": 0, "time": 0.0, "chord": None, "notes": ns}]

    notes, chords = routes._notes_for_level(groups, level=2, max_level=2)

    assert len(notes) == len(ns)


def test_evenly_sample_keeps_first_and_last_and_spreads_the_middle():
    ns = list(range(10))
    kept = routes._evenly_sample(ns, 3)
    assert kept[0] == 0
    assert kept[-1] == 9
    assert len(kept) == 3


def test_evenly_sample_returns_everything_when_keep_n_covers_the_whole_list():
    ns = [1, 2, 3]
    assert routes._evenly_sample(ns, 5) == ns


def test_evenly_sample_returns_first_item_when_keep_n_is_one():
    ns = [1, 2, 3, 4]
    assert routes._evenly_sample(ns, 1) == [1]


def test_group_anchor_note_picks_the_lowest_string_index_not_a_claimed_harmonic_root():
    # An inverted voicing: _group_anchor_note picks min(s) -- the note on
    # the lowest string index (s=1 here), which per feedpak's low-string-
    # first indexing (index 0 = lowest-pitched string, mirrored in song.py's
    # _TUNING_BASE_MIDI) is the bass-most note. That is a position, usually
    # the root in standard shapes, but it must NOT be read as a proven root:
    # in an inversion like this one the bass note isn't the root.
    group = {"notes": [
        {"s": 5, "f": 3},
        {"s": 1, "f": 7},  # lowest string index in this group
    ]}
    anchor = routes._group_anchor_note(group, prefer_fretted=False)
    assert (anchor["s"], anchor["f"]) == (1, 7)  # nosec B101 - pytest assertion


def test_notes_for_level_linked_arpeggio_via_hand_shape_still_reduces_to_one_note():
    # A genuine authored arpeggio (hand-shape evidence) should still get the
    # bottom-tier lowest-string (bass-note) reduction -- only
    # unsubstantiated "run" clusters get the new preserve-the-sequence
    # treatment.
    groups = [{
        "type": "arpeggio", "level": 0, "time": 0.0, "chord": None,
        "notes": [{"t": 0.0, "s": 1, "f": 7}, {"t": 0.04, "s": 5, "f": 3}],
    }]
    notes, chords = routes._notes_for_level(groups, level=0, max_level=2)
    assert [(n["s"], n["f"]) for n in notes] == [(1, 7)]  # nosec B101 - pytest assertion


# ---------------------------------------------------------------------------
# Arrangement-wide tier scale: the mastery slider means the same difficulty in
# every phrase, an easy phrase is complete early, and a hard phrase keeps a
# full ladder even when it's hard all the way through.
# ---------------------------------------------------------------------------

def _tiered_beats(n):
    return [{"time": i * 0.5, "measure": (i // 4 + 1) if i % 4 == 0 else -1} for i in range(n)]


def _easy_then_hard(hard_notes):
    easy = [{"t": float(i), "s": 1, "f": 3, "sus": 0.9} for i in range(16)]
    return {
        "type": "lead", "name": "lead", "tuning": [0] * 6,
        "notes": easy + hard_notes, "chords": [], "beats": _tiered_beats(80),
        "sections": [{"time": 0}, {"time": 16}],
    }


def test_easy_phrase_is_complete_at_a_lower_tier_than_a_hard_phrase():
    hard = [{"t": 16 + i * 0.125, "s": 3 + (i % 3), "f": 14 + (i % 5), "sus": 0, "ho": i % 2 == 1}
            for i in range(128)]
    phrases = routes.generate_phrases_for_arrangement(_easy_then_hard(hard), n_levels=4)
    easy_phrase, hard_phrase = phrases
    for p in phrases:
        _assert_on_tier_scale(p, n_levels=4)
    # The easy verse is played in full at every slider position -- it used
    # to be thinned at the bottom exactly as hard as the solo.
    assert len(easy_phrase["levels"]) == 1  # nosec B101 - pytest assertion
    assert len(easy_phrase["levels"][0]["notes"]) == 16  # nosec B101 - pytest assertion
    # The solo differs at every tier and still has a skeleton at the bottom.
    assert [lvl["difficulty"] for lvl in hard_phrase["levels"]] == [0, 1, 2, 3]  # nosec B101 - pytest assertion
    counts = [len(lvl["notes"]) for lvl in hard_phrase["levels"]]
    assert 0 < counts[0] < counts[1] < counts[2] < counts[3] == 128  # nosec B101 - pytest assertion


def test_uniformly_hard_phrase_gets_a_full_ladder():
    # Every note in the solo scores the same (same string, fret, technique,
    # spacing). Depth used to come from score SPREAD, so this got the
    # shortest ladder; the per-phrase floor now thins it evenly instead.
    hard = [{"t": 16 + i * 0.125, "s": 4, "f": 17, "sus": 0, "tp": True} for i in range(128)]
    phrases = routes.generate_phrases_for_arrangement(_easy_then_hard(hard), n_levels=4)
    hard_phrase = phrases[1]
    assert [lvl["difficulty"] for lvl in hard_phrase["levels"]] == [0, 1, 2, 3]  # nosec B101 - pytest assertion
    bottom_times = [n["t"] for n in hard_phrase["levels"][0]["notes"]]
    assert bottom_times, "the bottom tier must not go silent"  # nosec B101 - pytest assertion
    # Spread across the phrase, not just its first few notes.
    assert max(bottom_times) - min(bottom_times) > 0.75 * (hard[-1]["t"] - hard[0]["t"])  # nosec B101 - pytest assertion


def test_all_multi_level_phrases_share_the_requested_tier_scale():
    arr = _arrangement(_technical_notes(0, 30, step=0.1), sections=[{"time": 0}, {"time": 10}, {"time": 20}])
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=5)
    assert phrases  # nosec B101 - pytest assertion
    for p in phrases:
        _assert_on_tier_scale(p, n_levels=5)


def test_tier_levels_are_nested():
    # Each tier must contain every (onset, string) the tier below plays,
    # counting chord members as well as single notes. Frets can legitimately
    # differ between tiers (a simplified pre-bend is fretted at its peak), so
    # they are not part of the key.
    hard = [{"t": 16 + i * 0.125, "s": 3 + (i % 3), "f": 14 + (i % 5), "sus": 0} for i in range(128)]
    arr = _easy_then_hard(hard)
    arr["chords"] = [
        {"t": 16.0625 + i * 1.0, "notes": [{"s": 0, "f": 3}, {"s": 1, "f": 5}, {"s": 2, "f": 5}, {"s": 3, "f": 4}]}
        for i in range(12)
    ]
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)

    def played(lvl):
        keys = {(n["t"], n["s"]) for n in lvl["notes"]}
        keys |= {(c["t"], n["s"]) for c in lvl["chords"] for n in c["notes"]}
        return keys

    for p in phrases:
        for lower, higher in pairwise([played(lvl) for lvl in p["levels"]]):
            assert lower <= higher  # nosec B101 - pytest assertion


def test_keys_phrases_use_the_tier_scale_too():
    notes = [{"t": round(i * 0.25, 3), "s": 2 + (i % 3), "f": (i * 7) % 24, "sus": 0} for i in range(64)]
    arr = {
        "type": "keys", "name": "keys", "notes": notes, "chords": [],
        "beats": _tiered_beats(64), "sections": [], "tuning": [],
    }
    phrases = routes.generate_phrases_for_arrangement(arr, n_levels=4)
    assert phrases  # nosec B101 - pytest assertion
    for p in phrases:
        _assert_on_tier_scale(p, n_levels=4)
        identities = [
            {(n["t"], routes._note_midi_keys(n)) for n in lvl["notes"]}
            for lvl in p["levels"]
        ]
        for lower, higher in pairwise(identities):
            assert lower <= higher  # nosec B101 - pytest assertion


def test_keys_octave_dedup_keeps_easier_tier_midi_representatives():
    # At tier 1 the median voice (71) is an octave below the tier-0 melody
    # voice (83). The octave simplifier must retain 83 rather than swapping
    # it out, since difficulty tiers are cumulative by absolute pitch.
    midis = [52, 64, 71, 83]
    notes = [{"t": 1.0, "s": midi // 24, "f": midi % 24} for midi in midis]
    groups = [{
        "type": "chord", "notes": notes, "chord": None,
        "time": 1.0, "cost": 0.5, "value": 0.0,
        "retention_score": 0.5, "level": 0,
    }]

    reduced = []
    for level in range(3):
        level_notes, _ = routes._notes_for_level_keys(groups, level, max_level=3)
        reduced.append({(n["t"], routes._note_midi_keys(n)) for n in level_notes})

    assert reduced[0] == {(1.0, 52), (1.0, 83)}  # nosec B101 - pytest assertion
    assert reduced[0] <= reduced[1] <= reduced[2]  # nosec B101 - pytest assertion
    assert (1.0, 71) not in reduced[1]  # nosec B101 - octave simplification


def test_keys_reduced_tier_collapses_a_simple_octave_double():
    notes = [{"t": 0.0, "s": 2, "f": 12}, {"t": 0.0, "s": 3, "f": 0}]
    groups = [{
        "type": "chord", "notes": notes, "chord": None,
        "time": 0.0, "cost": 0.5, "value": 0.0,
        "retention_score": 0.5, "level": 0,
    }]

    reduced, _ = routes._notes_for_level_keys(groups, level=0, max_level=3)

    assert len(reduced) == 1  # nosec B101 - pytest assertion
    assert routes._note_midi_keys(reduced[0]) in {60, 72}  # nosec B101 - pytest assertion


def test_keys_reduced_tier_preserves_ranked_pitch_order():
    midis = [52, 59, 67]
    notes = [{"t": 0.0, "s": midi // 24, "f": midi % 24} for midi in midis]
    groups = [{
        "type": "chord", "notes": notes, "chord": None,
        "time": 0.0, "cost": 0.5, "value": 0.0,
        "retention_score": 0.5, "level": 0,
    }]

    reduced, _ = routes._notes_for_level_keys(groups, level=1, max_level=3)

    assert [routes._note_midi_keys(n) for n in reduced] == midis  # nosec B101


def test_spread_key_orders_positions_evenly():
    assert [routes._spread_key(i) for i in range(4)] == [0.0, 0.5, 0.25, 0.75]  # nosec B101 - pytest assertion


# ---------------------------------------------------------------------------
# Chord reduction: root-only is reachable at the default tier count, and the
# root is the LOWEST string (string 0 = lowest, feedpak-v1 §6.2).
# ---------------------------------------------------------------------------

def _chord_group(level=0):
    chord = {"t": 1.0, "notes": [
        {"s": 0, "f": 3}, {"s": 1, "f": 2}, {"s": 2, "f": 0},
        {"s": 3, "f": 0}, {"s": 4, "f": 0}, {"s": 5, "f": 3},
    ]}
    return [{"type": "chord", "notes": list(chord["notes"]), "chord": chord,
             "time": 1.0, "cost": 0.5, "value": 0.0,
             "retention_score": 0.5, "level": level}]


def test_bottom_tier_reduces_chords_to_the_root_at_the_default_four_tiers():
    notes, chords = routes._notes_for_level(_chord_group(), level=0, max_level=3)
    assert chords == []  # nosec B101 - pytest assertion
    assert [(n["s"], n["f"]) for n in notes] == [(0, 3)], "root = lowest string (a G chord's low G)"  # nosec B101 - pytest assertion


def test_second_tier_keeps_a_partial_voicing_built_on_the_root():
    notes, _ = routes._notes_for_level(_chord_group(), level=1, max_level=3)
    assert len(notes) == 2  # nosec B101 - pytest assertion
    assert (0, 3) in [(n["s"], n["f"]) for n in notes]  # nosec B101 - pytest assertion


def test_root_only_is_not_used_when_the_bottom_tier_covers_more_than_a_quarter():
    notes, _ = routes._notes_for_level(_chord_group(), level=0, max_level=2)
    assert len(notes) == 2  # nosec B101 - pytest assertion


# ---------------------------------------------------------------------------
# Pitch-preserving technique removal.
# ---------------------------------------------------------------------------

def test_stripped_pre_bend_is_fretted_at_the_bent_pitch():
    note = {"t": 0.0, "s": 2, "f": 7, "sus": 0.5, "bn": 2.0, "bt": 2,
            "bnv": [{"t": 0, "v": 2.0}, {"t": 0.5, "v": 2.0}]}
    pruned = routes._prune_techniques(note, diff_percent=0.30)
    assert (pruned["f"], pruned["bn"], pruned["bt"]) == (9, 0, 0)  # nosec B101 - pytest assertion
    assert "bnv" not in pruned  # nosec B101 - pytest assertion


def test_pre_bend_below_its_intent_gate_is_fretted_not_turned_into_a_bend_up():
    # A bend-up is struck at the unbent fret, which is a whole step flat of a
    # pre-bend's onset -- so the intent downgrade frets the peak instead.
    note = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "bn": 1.0, "bt": 2}
    pruned = routes._prune_techniques(note, diff_percent=0.60)
    assert (pruned["f"], pruned["bn"], pruned["bt"]) == (6, 0, 0)  # nosec B101 - pytest assertion


def test_pre_bend_release_is_fretted_at_its_onset_pitch():
    note = {"t": 0.0, "s": 2, "f": 5, "sus": 0, "bn": 2.0, "bt": 3}
    pruned = routes._prune_techniques(note, diff_percent=0.60)
    assert (pruned["f"], pruned["bn"], pruned["bt"]) == (7, 0, 0)  # nosec B101 - pytest assertion


def test_bend_up_keeps_its_fret_when_stripped():
    note = {"t": 0.0, "s": 2, "f": 7, "sus": 0, "bn": 2.0}
    pruned = routes._prune_techniques(note, diff_percent=0.30)
    assert (pruned["f"], pruned["bn"]) == (7, 0)  # nosec B101 - pytest assertion


def test_struck_at_peak_bend_without_a_fretted_equivalent_is_kept_as_authored():
    curve = [{"t": 0, "v": 0.5}, {"t": 0.2, "v": 0.5}, {"t": 0.4, "v": 0}]
    quarter_tone = {"t": 0.0, "s": 2, "f": 5, "sus": 0.4, "bn": 0.5, "bt": 3, "bnv": curve}
    past_last_fret = {"t": 0.0, "s": 2, "f": 23, "sus": 0, "bn": 2.0, "bt": 2}
    for note in (quarter_tone, past_last_fret):
        pruned = routes._prune_techniques(note, diff_percent=0.30)
        assert (pruned["f"], pruned["bn"], pruned["bt"]) == (note["f"], note["bn"], note["bt"])  # nosec B101 - pytest assertion
    # "As authored" includes the curve: the bnv gate (0.80) must not strip it
    # from a bend that was deliberately kept.
    assert routes._prune_techniques(quarter_tone, diff_percent=0.30)["bnv"] == curve  # nosec B101 - pytest assertion


def test_natural_harmonic_is_kept_where_stripping_would_change_its_pitch():
    # A harmonic at fret 7 sounds the pitch of fret 19; a plain fret-7 note
    # would be a different note entirely.
    for fret in (5, 7, 4):
        note = {"t": 0.0, "s": 2, "f": fret, "sus": 0, "hm": True}
        assert routes._prune_techniques(note, diff_percent=0.30).get("hm") is True  # nosec B101 - pytest assertion
    for fret in (12, 19, 24):
        note = {"t": 0.0, "s": 2, "f": fret, "sus": 0, "hm": True}
        assert "hm" not in routes._prune_techniques(note, diff_percent=0.30)  # nosec B101 - pytest assertion


def test_explicit_false_flags_do_not_keep_a_duplicate_tier_alive():
    # Importers commonly write every boolean flag explicitly ("ho": false).
    # Gating pops the key, so without normalising false == absent the pruned
    # tier and the untouched top tier compared as different.
    pruned = {"t": 0, "s": 0, "f": 0, "sus": 0.6, "sl": -1, "slu": -1, "bn": 0.0}
    source = dict(pruned, ho=False, po=False, pm=False, tp=False)
    assert routes._canonical_note_for_compare(pruned) == routes._canonical_note_for_compare(source)  # nosec B101 - pytest assertion
    levels = [
        {"difficulty": 0, "notes": [pruned], "chords": [], "anchors": [], "handshapes": []},
        {"difficulty": 1, "notes": [source], "chords": [], "anchors": [], "handshapes": []},
    ]
    assert [lvl["difficulty"] for lvl in routes._collapse_identical_levels(levels)] == [0]  # nosec B101 - pytest assertion


@pytest.mark.skip(
    reason="Complex integration test scenario is covered by unit tests: "
    "test_instrument_kind_detects_drums_by_name_when_type_is_blank, "
    "test_instrument_kind_detects_unsupported_by_name_when_type_is_blank, "
    "and test_instrument_kind_blank_type_with_fretted_names_still_defaults_to_fretted. "
    "This would test a seven-arrangement pack with missing types for Sax/Drums/Drums 2."
)
def test_missing_arrangement_type_detects_unsupported_by_name_issue_102():
    """Regression test for issue #102: missing arrangement type should not
    silently mean fretted for names identifying drums, sax, or other
    unsupported instruments.

    Scenario: a feedpak with seven arrangements (Lead, Combo, Bass, Sax,
    Keys, Drums, Drums 2) where Sax, Drums, and Drums 2 have blank type
    fields. The generator should skip all three and report their reasons
    accurately, generating only the four supported arrangements.
    """
    class _Lock:
        def __enter__(self) -> "_Lock":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    # Create a mock manifest with seven arrangements
    manifest = {
        "title": "Money",
        "artist": "Pink Floyd",
        "duration": 300.0,
        "arrangements": [
            {"id": "lead", "name": "Lead", "file": "arrangements/lead.json"},
            {"id": "combo", "name": "Combo", "file": "arrangements/combo.json"},
            {"id": "bass", "name": "Bass", "file": "arrangements/bass.json"},
            # Sax with blank type (should be detected as unsupported by name)
            {"id": "sax", "name": "Sax", "file": "arrangements/sax.json"},
            # Keys with blank type (should be detected by name)
            {"id": "keys", "name": "Keys", "file": "arrangements/keys.json"},
            # Drums with blank type (should be detected as unsupported by name)
            {"id": "drums", "name": "Drums", "file": "arrangements/drums.json"},
            # Drums 2 with blank type (should be detected as unsupported by name)
            {"id": "drums2", "name": "Drums 2", "file": "arrangements/drums2.json"},
        ],
        "stems": [{"id": "full", "file": "stems/full.ogg"}],
    }

    # Create mock arrangement data for each one
    def _make_arr(name):
        return {
            "name": name,
            # Intentionally omit "type" to simulate the bug scenario
            "notes": [
                {"t": 0.0, "s": 0, "f": 5, "sus": 0.2},
                {"t": 0.5, "s": 1, "f": 7, "sus": 0.2},
                {"t": 1.0, "s": 2, "f": 9, "sus": 0.2},
            ],
            "chords": [],
            "beats": [{"time": i * 0.5} for i in range(100)],
            "sections": [],
            "anchors": [],
            "handshapes": [],
        }

    # Keys arrangement should explicitly have type set to trigger keys path
    keys_arr = _make_arr("Keys")
    keys_arr["type"] = "keys"

    load_results = {
        0: ("arrangements/lead.json", _make_arr("Lead"), None),  # supported fretted
        1: ("arrangements/combo.json", _make_arr("Combo"), None),  # supported fretted
        2: ("arrangements/bass.json", _make_arr("Bass"), None),  # supported fretted
        3: ("arrangements/sax.json", _make_arr("Sax"), None),  # unsupported by name
        4: ("arrangements/keys.json", keys_arr, None),  # supported keys
        5: ("arrangements/drums.json", _make_arr("Drums"), None),  # unsupported by name
        6: ("arrangements/drums2.json", _make_arr("Drums 2"), None),  # unsupported by name
    }

    def _mock_load_manifest(pack_path, idx):
        return load_results[idx]

    with patch.object(
        routes, "_lock_for_pack", return_value=_Lock()
    ), patch.object(routes, "sloppak") as mock_sloppak, patch.object(
        routes, "_load_manifest_and_arrangement", side_effect=_mock_load_manifest
    ), patch.object(
        routes, "generate_phrases_for_arrangement", return_value=[
            {"difficulty": 0, "notes": [], "chords": [], "anchors": [], "handshapes": []}
        ]
    ):
        mock_sloppak.load_manifest.return_value = manifest

        results = {}
        for i in range(7):
            result = routes._generate_one(
                Path("test.feedpak"), i, n_levels=4, force=False,
                log=logging.getLogger(__name__)
            )
            results[i] = result

    # Verify the results
    # Indices 0, 1, 2, 4 should be generated (supported)
    for idx in [0, 1, 2, 4]:
        assert results[idx]["ok"] is True  # nosec B101 - pytest assertion
        # Supported arrangements should not have skipped reason

    # Indices 3, 5, 6 should be skipped as unsupported
    for idx, expected_name in [(3, "Sax"), (5, "Drums"), (6, "Drums 2")]:
        assert results[idx]["ok"] is True  # nosec B101 - pytest assertion
        assert results[idx]["skipped"] is not None  # nosec B101 - pytest assertion
        # For drums, expect "unsupported-instrument-drums", for others "unsupported-instrument-type"
        if expected_name in ("Drums", "Drums 2"):
            # Name-sniffed as drums when type is blank
            assert results[idx]["skipped"] == "unsupported-instrument-drums"  # nosec B101 - pytest assertion
            assert results[idx]["instrument"] == "drums"  # nosec B101 - pytest assertion
        elif expected_name == "Sax":
            # Name-sniffed as unsupported
            assert results[idx]["skipped"] == "unsupported-instrument-type"  # nosec B101 - pytest assertion
            assert results[idx]["instrument"] == "unsupported"  # nosec B101 - pytest assertion

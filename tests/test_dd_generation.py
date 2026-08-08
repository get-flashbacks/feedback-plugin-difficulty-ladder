"""Tests for the fretted-instrument difficulty ladder generator in routes.py.

Self-contained sys.path bootstrap (this plugin has no pyproject.toml / shared
conftest of its own) so `pytest tests/` works from this directory directly.
"""
import sys
from pathlib import Path
from unittest.mock import patch

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


def test_lower_tier_refinement_does_not_bridge_a_repositioning_rest():
    groups = [
        {"time": 0.0, "score": 0.1, "level": 0, "notes": [{"s": 5, "f": 2}]},
        {"time": 1.0, "score": 0.2, "level": 2, "notes": [{"s": 5, "f": 7}]},
        {"time": 2.0, "score": 0.1, "level": 0, "notes": [{"s": 5, "f": 12}]},
    ]

    routes._refine_lower_tier_path(groups, [], max_level=2)

    assert groups[1]["level"] == 2

#!/usr/bin/env python3
"""Generate baseline outputs for fretted validation test fixtures."""

import json
import sys
import tempfile
import os
from pathlib import Path

# Bootstrap paths like the tests do
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_CORE_LIB = _PLUGIN_DIR.parent / "feedBack" / "lib"
for p in (_PLUGIN_DIR, _CORE_LIB):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import scoring


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fretted_validation"
OUTPUT_DIR = Path(__file__).parent / "fixtures" / "fretted_validation_baseline"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FIXTURE_FILES = [
    "adicts_bass.json",
    "adicts_lead.json",
    "adicts_rhythm.json",
    "bass_v1_bass.json",
    "diagnostic_guitar_lead.json",
    "lead_v1_lead.json",
    "lead_v2_lead.json",
    "star_spangled_lead.json",
]

def main():
    for fixture_file in FIXTURE_FILES:
        fixture_path = FIXTURE_DIR / fixture_file
        with open(fixture_path) as f:
            arr = json.load(f)

        result = scoring.generate_phrases_for_arrangement(arr, n_levels=6)

        output_path = OUTPUT_DIR / f"{fixture_file}.baseline.json"
        with open(output_path, 'w') as f:
            json.dump(result, f, indent=2, default=str)

        print(f"Generated baseline for {fixture_file}: {len(result) if result else 0} phrases")

    print(f"\nBaselines written to {OUTPUT_DIR}")

if __name__ == "__main__":
    main()

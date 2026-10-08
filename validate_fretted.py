#!/usr/bin/env python3
"""
Validate #182's shared-code changes on real fretted charts (before/after).
Compares generate_phrases_for_arrangement output on commit before #182 vs current main.
"""

import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

ARRANGEMENT_DIRS = [
    "/tmp/fretted_arrangements/adicts/arrangements",
    "/tmp/fretted_arrangements/star_spangled/arrangements",
    "/tmp/fretted_arrangements/bass_v1/arrangements",
    "/tmp/fretted_arrangements/lead_v1/arrangements",
    "/tmp/fretted_arrangements/lead_v2/arrangements",
    "/tmp/fretted_arrangements/diagnostic_guitar/arrangements",
]

COMMIT_BEFORE_182 = "dbfaeba^"
COMMIT_CURRENT = "main"


def collect_arrangements():
    """Collect all fretted arrangement files."""
    arrangements = []
    for dir_path in ARRANGEMENT_DIRS:
        path = Path(dir_path)
        if path.exists():
            for json_file in path.glob("*.json"):
                with open(json_file) as f:
                    arr = json.load(f)
                arr_name = f"{path.parent.name}/{json_file.stem}"
                arrangements.append((arr_name, arr))
    return arrangements


def run_generate_at_commit(commit, arrangement, n_levels=6):
    """Run generate_phrases_for_arrangement at a specific commit."""
    # Write arrangement to a temp JSON file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(arrangement, f)
        arr_path = f.name
    
    # Create a temporary script to run at the commit
    script = f'''
import json
import sys
sys.path.insert(0, "/workspace/adbd6f5d-b5f2-42f5-90ca-4985ce7aae60/sessions/workspace_8a940105-04a3-4b3a-8583-d66acbd7c4d6")
sys.path.insert(0, "/workspace/adbd6f5d-b5f2-42f5-90ca-4985ce7aae60/sessions/feedBack/lib")
import routes

with open("{arr_path}") as f:
    arr = json.load(f)

result = routes.generate_phrases_for_arrangement(arr, n_levels={n_levels})
print(json.dumps(result, default=str))
'''
    
    # Write script to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(script)
        script_path = f.name
    
    try:
        # Run the script
        env = dict(**os.environ, PYTHONPATH="/workspace/adbd6f5d-b5f2-42f5-90ca-4985ce7aae60/sessions/workspace_8a940105-04a3-4b3a-8583-d66acbd7c4d6:/workspace/adbd6f5d-b5f2-42f5-90ca-4985ce7aae60/sessions/feedBack/lib")
        result = subprocess.run(
            [sys.executable, script_path],
            env=env,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode != 0:
            return {"error": f"Script failed: {result.stderr}"}
        
        try:
            return json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON output: {result.stdout}"}
    finally:
        Path(script_path).unlink(missing_ok=True)
        Path(arr_path).unlink(missing_ok=True)


def compare_results(before, after, arr_name):
    """Compare two results and return differences."""
    diffs = []
    
    if "error" in before or "error" in after:
        diffs.append(f"  Error: before={before.get('error')}, after={after.get('error')}")
        return diffs
    
    if before is None and after is None:
        return diffs
    
    if before is None or after is None:
        diffs.append(f"  One is None: before={before is None}, after={after is None}")
        return diffs
    
    # Compare phrase by phrase
    if len(before) != len(after):
        diffs.append(f"  Different number of phrases: before={len(before)}, after={len(after)}")
    
    for i, (b_phrase, a_phrase) in enumerate(zip(before, after)):
        phrase_diffs = []
        
        # Compare levels
        b_levels = b_phrase.get("levels", [])
        a_levels = a_phrase.get("levels", [])
        
        if len(b_levels) != len(a_levels):
            phrase_diffs.append(f"    Phrase {i}: different level count: before={len(b_levels)}, after={len(a_levels)}")
        else:
            for j, (b_lvl, a_lvl) in enumerate(zip(b_levels, a_levels)):
                if b_lvl.get("difficulty") != a_lvl.get("difficulty"):
                    phrase_diffs.append(f"    Phrase {i} level {j}: difficulty before={b_lvl.get('difficulty')}, after={a_lvl.get('difficulty')}")
                # Compare notes per level
                b_notes = len(b_lvl.get("notes", []))
                a_notes = len(a_lvl.get("notes", []))
                if b_notes != a_notes:
                    phrase_diffs.append(f"    Phrase {i} level {j}: notes count before={b_notes}, after={a_notes}")
        
        # Compare max_difficulty
        if b_phrase.get("max_difficulty") != a_phrase.get("max_difficulty"):
            phrase_diffs.append(f"  Phrase {i}: max_difficulty before={b_phrase.get('max_difficulty')}, after={a_phrase.get('max_difficulty')}")
        
        diffs.extend(phrase_diffs)
    
    return diffs


def main():
    print("Collecting fretted arrangements...")
    arrangements = collect_arrangements()
    print(f"Found {len(arrangements)} fretted arrangements")
    
    all_diffs = {}
    
    for arr_name, arr in arrangements:
        print(f"\nProcessing {arr_name}...")
        
        print(f"  Running at {COMMIT_CURRENT} (current)...")
        after = run_generate_at_commit(COMMIT_CURRENT, arr)
        
        print(f"  Running at {COMMIT_BEFORE_182} (before #182)...")
        before = run_generate_at_commit(COMMIT_BEFORE_182, arr)
        
        diffs = compare_results(before, after, arr_name)
        if diffs:
            all_diffs[arr_name] = diffs
            print(f"  DIFFERENCES FOUND:")
            for d in diffs:
                print(d)
        else:
            print(f"  IDENTICAL")
    
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    if all_diffs:
        print(f"Found differences in {len(all_diffs)} arrangements:")
        for arr_name, diffs in all_diffs.items():
            print(f"\n{arr_name}:")
            for d in diffs:
                print(d)
        return 1
    else:
        print("All fretted arrangements produce identical output!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
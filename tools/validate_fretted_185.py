#!/usr/bin/env python3
"""Before/after validation for #185: fretted-path drift across #182.

Runs generate_phrases_for_arrangement on real fretted arrangements using
routes.py at the commit before #182 and the current checkout's scoring core
(scoring.py), then diffs the full output. Expected result: identical output
for every arrangement.

Lives in tools/ because it is a CLI script that prints to stdout by design
(the no-print-in-routes custom check exempts tools/).

Usage (from the plugin root):
    python3 tools/validate_fretted_185.py <arrangement.json> [<arrangement.json> ...]
"""

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
CORE_LIB = PLUGIN_DIR.parent / "feedBack" / "lib"

COMMIT_BEFORE_182 = "dbfaeba^"

for _p in (str(PLUGIN_DIR), str(CORE_LIB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scoring  # noqa: E402


def _routes_source_at_commit(commit):
    """Retrieve routes.py content at a specific commit."""
    result = subprocess.run(
        ["git", "show", f"{commit}:routes.py"],
        cwd=str(PLUGIN_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get routes.py at {commit}: {result.stderr}")
    return result.stdout


def _load_routes_module(name, source):
    """Load routes.py source in-process under a distinct module name."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        routes_path = f.name
    spec = importlib.util.spec_from_file_location(name, routes_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        Path(routes_path).unlink(missing_ok=True)
    return module


def main():
    parser = argparse.ArgumentParser(
        description="Compare fretted generation before/after #182 on real arrangements."
    )
    parser.add_argument("arrangements", nargs="+", help="arrangement JSON files")
    parser.add_argument("--n-levels", type=int, default=6)
    parser.add_argument("--before", default=COMMIT_BEFORE_182)
    args = parser.parse_args()

    routes_before = _load_routes_module(
        "routes_before_182", _routes_source_at_commit(args.before)
    )

    failures = 0
    for raw in args.arrangements:
        path = Path(raw)
        arr = json.loads(path.read_text())
        got = scoring.generate_phrases_for_arrangement(arr, n_levels=args.n_levels)
        exp = routes_before.generate_phrases_for_arrangement(arr, n_levels=args.n_levels)
        if got == exp:
            print(f"{path.name}: IDENTICAL")
        else:
            failures += 1
            print(f"{path.name}: DIFFERENT")
            got_s = json.dumps(got, default=str, sort_keys=True)
            exp_s = json.dumps(exp, default=str, sort_keys=True)
            for i, (a, b) in enumerate(zip(got_s, exp_s)):
                if a != b:
                    lo = max(0, i - 120)
                    print(f"  first diff at char {i}:")
                    print(f"    now:    ...{got_s[lo:i + 120]}...")
                    print(f"    before: ...{exp_s[lo:i + 120]}...")
                    break
            else:
                print(f"  length now={len(got_s)}, before={len(exp_s)}")

    total = len(args.arrangements)
    print(f"\n{total - failures}/{total} arrangements identical")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""Fail when any file in a coverage.py JSON report is below a line-coverage floor.

coverage.py's `fail_under` only checks the total; this checks every file. CI runs
it after `pytest --cov-report=json`:
    uv run python scripts/check_coverage.py --min 90 coverage.json

Exit codes: 0 when every file meets the floor, 1 when any file is below it (each
one is printed with its percentage), 2 when the report is missing, not valid JSON,
or lists no files. A broken report fails the check instead of passing it.
"""
import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min", type=float, default=90.0, help="minimum line coverage per file, in percent (default: 90)")
    parser.add_argument("path", nargs="?", default="coverage.json", help="coverage.py JSON report (default: coverage.json)")
    args = parser.parse_args(argv)

    try:
        report = json.loads(Path(args.path).read_text())
        files = {name: entry["summary"]["percent_covered"] for name, entry in report["files"].items()}
    except FileNotFoundError:
        print(f"{args.path}: coverage report not found", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"{args.path}: not valid JSON ({exc})", file=sys.stderr)
        return 2
    except (AttributeError, KeyError, TypeError):
        print(f"{args.path}: not a coverage.py JSON report (no files with a summary)", file=sys.stderr)
        return 2
    if not files:
        print(f"{args.path}: the report lists no files", file=sys.stderr)
        return 2

    below = {name: percent for name, percent in sorted(files.items()) if percent < args.min}
    for name, percent in below.items():
        print(f"{name}: {percent:.2f}% < {args.min:g}%")
    if below:
        print(f"{len(below)} of {len(files)} files below {args.min:g}% line coverage", file=sys.stderr)
        return 1
    print(f"all {len(files)} files at or above {args.min:g}% line coverage")
    return 0


if __name__ == "__main__":
    sys.exit(main())

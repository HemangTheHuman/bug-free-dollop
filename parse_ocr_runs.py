"""
parse_ocr_runs.py
=================
One-time script to parse all tfevents files from the runs/r* directories
and produce a static ocr_runs_data.json in the project root.

Usage:
    python parse_ocr_runs.py

Requirements:
    pip install tensorboard

Output:
    ocr_runs_data.json  (root of project)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
except ImportError:
    print(
        "ERROR: tensorboard is not installed.\n"
        "Run:  pip install tensorboard\n"
        "Then re-run this script."
    )
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
RUNS_DIR     = PROJECT_ROOT / "runs"
OUTPUT_FILE  = PROJECT_ROOT / "ocr_runs_data.json"

# Only process OCR runs (r* prefix)
RUN_PREFIX = "r"

# How many steps to load per scalar tag (0 = all)
SIZE_GUIDANCE = {
    "scalars": 0,
    "images":  0,
    "tensors": 0,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_run(run_dir: Path) -> dict:
    """Parse all scalar tags from a run directory and return a dict."""
    print(f"  Parsing: {run_dir.name} ...", end=" ", flush=True)

    ea = EventAccumulator(str(run_dir), size_guidance=SIZE_GUIDANCE)
    try:
        ea.Reload()
    except Exception as exc:
        print(f"FAILED ({exc})")
        return {"error": str(exc), "scalars": {}, "available_tags": [], "total_steps": 0}

    scalar_tags = ea.Tags().get("scalars", [])
    scalars: dict[str, list[list]] = {}

    for tag in scalar_tags:
        try:
            events = ea.Scalars(tag)
            # Each event: step, value (wall_time omitted for compactness)
            scalars[tag] = [[int(e.step), float(e.value)] for e in events]
        except Exception as exc:
            print(f"\n    WARNING: could not read tag '{tag}': {exc}")

    total_steps = max(
        (max(v[-1][0] for v in scalars.values()) if scalars else 0),
        0
    )

    print(f"OK  ({len(scalar_tags)} tags, {total_steps} max steps)")
    return {
        "scalars": scalars,
        "available_tags": scalar_tags,
        "total_steps": total_steps,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not RUNS_DIR.exists():
        print(f"ERROR: runs directory not found at {RUNS_DIR}")
        sys.exit(1)

    # Find all r* subdirectories
    run_dirs = sorted(
        d for d in RUNS_DIR.iterdir()
        if d.is_dir() and d.name.startswith(RUN_PREFIX)
    )

    if not run_dirs:
        print(f"No directories starting with '{RUN_PREFIX}' found in {RUNS_DIR}")
        sys.exit(1)

    print(f"Found {len(run_dirs)} OCR run(s) to parse:\n")
    for d in run_dirs:
        print(f"  • {d.name}")
    print()

    output: dict = {"runs": {}}

    for run_dir in run_dirs:
        output["runs"][run_dir.name] = load_run(run_dir)

    # Write output
    OUTPUT_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"\n✓ Written to: {OUTPUT_FILE}")
    print(f"  Total runs: {len(output['runs'])}")

    # Summary of tags found
    print("\nTag summary per run:")
    for run_name, data in output["runs"].items():
        tags = data.get("available_tags", [])
        print(f"  {run_name}: {len(tags)} tags")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate task.json files from existing prompt.md files.

Each task.json contains deliverable, acceptance criteria, and context metadata
that fast-delegate uses to judge task fit and recommend a model.
"""

import argparse
import json
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent

TASKS = {
    "A": {
        "deliverable": "Parse cli.py and extract all subcommands and exit codes to answer.json",
        "acceptance": [
            "answer.json exists with valid JSON",
            "Every subcommand name and source line matches the fixture exactly",
            "Every exit code name, value, and source line matches the fixture exactly"
        ],
        "context": {
            "files": ["cli.py"],
            "verifiable": True,
            "independence": True
        }
    },
    "B": {
        "deliverable": "Implement merge_intervals() and total_covered() functions in intervals.py to pass all 16 unit tests",
        "acceptance": [
            "All tests in test_intervals.py pass",
            "merge_intervals correctly merges overlapping intervals",
            "total_covered correctly computes total coverage"
        ],
        "context": {
            "files": ["intervals.py", "test_intervals.py"],
            "verifiable": True,
            "independence": True
        }
    },
    "C": {
        "deliverable": "Review utils.py and find all bugs; write findings to findings.json",
        "acceptance": [
            "findings.json contains exactly 3 bugs: chunk, business_days_between, weekday_name",
            "Each finding has correct function name, line number (within function bounds), and description",
            "Zero false positives (parse_duration is not a bug)"
        ],
        "context": {
            "files": ["utils.py"],
            "verifiable": False,
            "independence": False
        }
    },
    "D": {
        "deliverable": "Review retry.py and find all bugs; write findings to findings.json",
        "acceptance": [
            "findings.json contains exactly 3 bugs: backoff exponent off-by-one, jitter after cap exceeds max_delay, sleep after final attempt",
            "Each finding has correct function name and line number",
            "Zero false positives (CircuitBreaker is clean; it is a decoy)"
        ],
        "context": {
            "files": ["retry.py"],
            "verifiable": False,
            "independence": False
        }
    },
    "E": {
        "deliverable": "Fix the bug in topo_sort.py so all tests pass including the hidden test",
        "acceptance": [
            "All visible tests in test_topo_sort.py pass",
            "Hidden test for diamond dependency passes (not visible to worker)"
        ],
        "context": {
            "files": ["topo_sort.py", "test_topo_sort.py"],
            "verifiable": True,
            "independence": True
        }
    }
}


def generate_task_json(task_id, out_dir=None):
    """Generate task.json for one task."""
    if task_id not in TASKS:
        raise ValueError(f"Unknown task: {task_id}")
    
    spec = TASKS[task_id]
    task_dir = BENCH_DIR / "tasks" / task_id
    
    if out_dir:
        out_path = Path(out_dir) / f"{task_id}.json"
    else:
        out_path = task_dir / "task.json"
    
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(spec, indent=2) + "\n")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Generate task.json files for routing")
    parser.add_argument("--tasks", default="A,B,C,D,E", help="Comma-separated task IDs")
    parser.add_argument("--out", help="Output directory (default: in-place in tasks/X/)")
    args = parser.parse_args()
    
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    for task_id in tasks:
        path = generate_task_json(task_id, args.out)
        print(f"Generated {path}")


if __name__ == "__main__":
    main()

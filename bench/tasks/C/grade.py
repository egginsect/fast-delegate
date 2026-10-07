#!/usr/bin/env python3
"""Grader for Task C."""

import json
import os
import sys
from pathlib import Path


def key_dir():
    """The key directory; run.py points BENCH_KEY_DIR at a private copy because
    the in-tree key is chmod 000 while a batch runs."""
    env = os.environ.get("BENCH_KEY_DIR")
    return Path(env) if env else Path(__file__).parent / "key"


def emit(accepted, score, detail, **extra):
    out = {"accepted": bool(accepted), "score": float(score), "detail": detail}
    out.update(extra)
    return out


def main():
    if len(sys.argv) != 2:
        print(json.dumps(emit(False, 0.0, "Usage: grade.py <workdir>")))
        return
    try:
        result = grade(sys.argv[1])
    except Exception as e:  # a malformed answer must reject, never crash
        result = emit(False, 0.0, f"grader error: {type(e).__name__}: {e}")
    print(json.dumps(result))


def load_findings(workdir):
    path = Path(workdir) / "findings.json"
    if not path.exists():
        return None, "findings.json not found"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e}"
    if not isinstance(data, list):
        return None, "findings.json must be a list"
    return data, None


def parse_finding(f):
    """Return (function, line) or None when malformed."""
    if not isinstance(f, dict):
        return None
    fn, line = f.get("function"), f.get("line")
    if not isinstance(fn, str) or isinstance(line, bool) or not isinstance(line, int):
        return None
    return fn.strip().removesuffix("()"), line


def grade(workdir):
    findings, err = load_findings(workdir)
    if err:
        return emit(False, 0.0, err, matched=0, false_positives=0)
    ranges = json.loads((key_dir() / "ranges.json").read_text())
    buggy, neutral = ranges["buggy"], ranges["neutral"]

    matched, false_pos, notes = set(), 0, []
    for f in findings:
        parsed = parse_finding(f)
        if parsed is None:
            false_pos += 1
            notes.append("malformed finding")
            continue
        fn, line = parsed
        if fn in neutral:
            continue  # arguable design choice: neither required nor a false positive
        if fn in buggy and buggy[fn][0] <= line <= buggy[fn][1]:
            matched.add(fn)  # duplicates of an already-matched bug are not FPs
        else:
            false_pos += 1
            notes.append(f"false positive: {fn}:{line}")
    accepted = len(matched) == len(buggy) and false_pos == 0
    missing = sorted(set(buggy) - matched)
    detail = "All bugs found, no false positives" if accepted else "; ".join(
        ([f"missing: {', '.join(missing)}"] if missing else []) + notes[:3])
    return emit(accepted, len(matched) / len(buggy), detail,
                matched=len(matched), false_positives=false_pos)


if __name__ == "__main__":
    main()

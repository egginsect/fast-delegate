#!/usr/bin/env python3
"""Grader for Task D."""

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
    r = json.loads((key_dir() / "ranges.json").read_text())
    bugs, retry, cb = r["bugs"], r["retry"], r["circuit_breaker"]

    matched, false_pos, notes = set(), 0, []
    for f in findings:
        parsed = parse_finding(f)
        if parsed is None:
            false_pos += 1
            notes.append("malformed finding")
            continue
        fn, line = parsed
        if fn.split(".")[0] == "CircuitBreaker" or cb[0] <= line <= cb[1]:
            false_pos += 1
            notes.append("CircuitBreaker flagged (it is correct)")
            continue
        hit = [n for n, (lo, hi) in bugs.items() if lo <= line <= hi]
        if fn == "retry" and hit:
            matched.add(hit[0])
        else:
            false_pos += 1
            notes.append(f"false positive: {fn}:{line}")
    accepted = len(matched) == len(bugs) and false_pos == 0
    missing = sorted(set(bugs) - matched)
    detail = "All bugs found, no false positives" if accepted else "; ".join(
        ([f"missing: {', '.join(missing)}"] if missing else []) + notes[:3])
    return emit(accepted, len(matched) / len(bugs), detail,
                matched=len(matched), false_positives=false_pos)


if __name__ == "__main__":
    main()

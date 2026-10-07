#!/usr/bin/env python3
"""Grader for Task B."""

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


import shutil
import subprocess
import tempfile


def run_tests(workdir, pristine, extra, modules):
    """Run the pristine tests (plus hidden extras) against the worker's code in a
    scratch copy, so worker edits to tests cannot influence the result."""
    workdir = Path(workdir)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "w"
        shutil.copytree(workdir, scratch, ignore=shutil.ignore_patterns("__pycache__"))
        for src in pristine:
            shutil.copy(src, scratch / src.name)
        for src in extra:
            shutil.copy(src, scratch / src.name)
        try:
            res = subprocess.run([sys.executable, "-m", "unittest", *modules],
                                 cwd=scratch, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return emit(False, 0.0, "Test execution timed out")
    if res.returncode == 0:
        return emit(True, 1.0, "All tests passed")
    lines = [l for l in (res.stderr + res.stdout).splitlines() if l.startswith(("FAIL:", "ERROR:"))]
    return emit(False, 0.0, "; ".join(lines[:3]) or "Tests failed")


def grade(workdir):
    fixture = Path(__file__).parent / "fixture"
    if not (Path(workdir) / "intervals.py").exists():
        return emit(False, 0.0, "intervals.py not found in workdir")
    return run_tests(workdir, [fixture / "test_intervals.py"], [], ["test_intervals"])


if __name__ == "__main__":
    main()

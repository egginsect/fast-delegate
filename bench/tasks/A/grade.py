#!/usr/bin/env python3
"""Grader for Task A."""

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


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _index(items, field):
    out = {}
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("name"), str):
            raise ValueError(f"malformed {field} entry: {it!r}")
        if it["name"] in out:
            raise ValueError(f"duplicate {field} entry: {it['name']}")
        out[it["name"]] = it
    return out


def grade(workdir):
    path = Path(workdir) / "answer.json"
    if not path.exists():
        return emit(False, 0.0, "answer.json not found")
    try:
        answer = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return emit(False, 0.0, f"Invalid JSON: {e}")
    if not isinstance(answer, dict):
        return emit(False, 0.0, "answer.json must be an object")
    ref = json.loads((key_dir() / "ref" / "answer.json").read_text())

    errors, checks, passed = [], 0, 0
    for section, field, exact in (("subcommands", "handler", "handler"),
                                  ("exit_codes", "value", "value")):
        if not isinstance(answer.get(section), list):
            errors.append(f"Missing or invalid '{section}'")
            checks += 2 * len(ref[section])
            continue
        want, got = _index(ref[section], section), _index(answer[section], section)
        for name in got:
            if name not in want:
                errors.append(f"Unexpected {section} entry: {name}")
        for name, w_ in want.items():
            checks += 2
            g = got.get(name)
            if g is None:
                errors.append(f"Missing {section} entry: {name}")
                continue
            gv = g.get(field)
            if gv == w_[field] and (field != "value" or _int(gv)):
                passed += 1
            else:
                errors.append(f"{name}: {field} expected {w_[field]!r}, got {gv!r}")
            gl = g.get("line")
            if _int(gl) and abs(gl - w_["line"]) <= 1:
                passed += 1
            else:
                errors.append(f"{name}: line {gl!r} not within +-1 of {w_['line']}")
    accepted = not errors
    return emit(accepted, 1.0 if accepted else passed / checks if checks else 0.0,
                "All checks passed" if accepted else "; ".join(errors[:5]))


if __name__ == "__main__":
    main()

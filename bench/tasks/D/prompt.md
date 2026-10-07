# Task D: Code Review - Retry Logic

Review the code in `retry.py` and identify the bugs. The `retry()` function contains **exactly 3 bugs** relative to its docstring.

Write your findings to `findings.json` in the current directory with the following structure:

```json
[
  {
    "function": "<function or class name>",
    "line": <line number>,
    "description": "<description of the bug>"
  }
]
```

Only report real bugs: behavior that contradicts the documented contract. Reporting something that is not a real bug is a false positive.

**Acceptance criteria**:
- All 3 bugs are found, each with the correct function name and the line where it occurs
- 0 false positives

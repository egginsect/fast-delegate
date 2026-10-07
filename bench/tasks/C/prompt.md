# Task C: Code Review - Find Bugs

Review the code in `utils.py` and identify bugs. Your task is to find **exactly 3 real bugs** in the code.

Write your findings to `findings.json` in the current directory with the following structure:

```json
[
  {
    "function": "<function name>",
    "line": <line number>,
    "description": "<description of the bug>"
  }
]
```

**Real bugs** are behavior that violates the documented specification or causes incorrect results:
1. A function that fails to meet its docstring contract
2. Logic errors that produce wrong output
3. Edge case handling that contradicts documentation

Do not report:
- Style issues
- Arguable design choices

**Acceptance criteria**:
- You must find all 3 bugs
- Each finding names the function and a line inside that function
- False positives (reporting a bug that is not real) will result in rejection
- accept = all 3 bugs found and 0 false positives

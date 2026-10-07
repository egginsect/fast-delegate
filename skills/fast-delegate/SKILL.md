---
name: fast-delegate
description: Use when about to delegate one predefined task to a subagent and you want a recommended model chosen from LiteLLM price and context facts, Jev fit judgment, and deterministic eligibility gates. The caller dispatches, verifies, and records.
metadata:
  short-description: One-call worker routing with evidence
  evidence-date: 2026-09-24
---

# Fast delegate

`scripts/fdel.py` returns a ranked model recommendation list with evidence for one task.
It executes no workers and writes no handoff files: you dispatch, verify, and record.
Flags, environment, pricing, quota, configuration and Jev details: [REFERENCE.md](REFERENCE.md)
(read on demand).

## When to use

Use when routing one predefined delegation task to a subagent model. Not for running
inference or workers, task decomposition, or joint assignment of several tasks (no batch
optimizer exists).

## 1. Describe the task

Write one JSON file. Keep `deliverable` and `acceptance` literal.

```json
{
  "deliverable": "Implement merge_intervals and total_covered so tests pass",
  "family": "mechanical",
  "access": "write",
  "owned_paths": ["/repo/pkg/intervals.py"],
  "acceptance": ["cd /repo && pytest -q intervals"]
}
```

Optional: `family`, `need_tools`, `est_input_tokens`, `est_output_tokens`,
`est_cached_input_share`, `exclude`, `difficulty`, `context`, `context_files`,
`model_information_override`.

## 2. Route

```sh
python3 <this skill dir>/scripts/fdel.py route --task FILE --lead MODEL [--brief]
```

`--lead` is your own model id; only candidates strictly cheaper than it are considered.

## 3. Read the output

- `decision` is `delegate` or `direct`; `direct` has no candidate, and `reasons` say why.
- `recommendations` is the authoritative list, identical in full and `--brief` output.
  Order: raw `fit_score` descending, ties by quota-adjusted estimated cost, then id.
- Scores are advisory, not success probabilities. Every record has `review_required:
  true`: you accept or reject each one using the task's own context, so you may skip the
  top score or accept a lower one. Judge whether the worker can fully complete the
  deliverable and every acceptance criterion. Do not auto-use the legacy `candidate`
  (cheapest metadata-safe); it is not the selection.
- You must name the chosen model explicitly when dispatching, using that record's
  `model_selector` (`model_id` is the underlying model). Never omit it or inherit
  silently. A selector may need translating through your harness's own agent config.

## 4. Verify before accepting

Run the acceptance checks yourself against the worker's result. A check the worker wrote
(or its self-reported pass) is not acceptance evidence.

## 5. Record

```sh
python3 <this skill dir>/scripts/fdel.py record --candidate haiku --family mechanical --outcome accepted --route ROUTE_ID --tokens 4200 --note "8 passed" --served-model MODEL
```

Replace `haiku` with the candidate id you actually dispatched. `--outcome` is `accepted`
or `rejected`. `--route` is `route_id` from the route output. If you dispatched a
different candidate than recommended, add `--override "<why>"`. `--tokens` comes from the
harness usage notification after verification.

Proxy guard: route drops proxy-routed (`proxy-*`) candidates when the proxy is not active,
and `record --served-model` (or `--transcript`) refuses cross-family model mismatches.
Same-family version drift is recorded with a warning; see the reference for overrides.

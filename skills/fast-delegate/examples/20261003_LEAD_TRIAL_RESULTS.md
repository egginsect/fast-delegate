# Lead trials: does the fast-delegate skill pay for itself? (2026-10-03)

Each trial is a headless opus lead (`claude -p`, isolated `CLAUDE_CONFIG_DIR`) told to
deliver `summarize_outcomes` from an 8-clause spec through subagents, minimizing total cost.
A hidden oracle grades the final tree: 90 cases, every one of 54 single-bug mutants killed,
two independent correct implementations pass. Runner: [lead_trial_experiment.py](lead_trial_experiment.py).
Per-trial data: [20261003_LEAD_TRIAL_RESULTS.json](20261003_LEAD_TRIAL_RESULTS.json).

| Arm | Trials | Oracle pass | Lead USD mean | Lead USD median | Worker USD mean |
| --- | ---: | ---: | ---: | ---: | ---: |
| No skill | 3 | 3/3 | 0.41 | 0.35 | 0.017 |
| Skill, 45 KB SKILL.md | 3 | 3/3 | 0.89 | 1.07 | 0.015 |
| Skill, 3.4 KB SKILL.md + proxy guard | 3 | 3/3 | 0.46 | 0.45 | 0.015 |

USD is catalog repricing of the lead session and subagent transcripts, not invoices. The
CLI's own total prices proxy-routed tokens at a default rate and overstates proxy workers.

## Findings

- Every trial in every arm spawned one `proxy-gpt-6-luna` worker (served model confirmed in the
  transcript) and passed the oracle. Workers cost about 2-4% of each trial; the opus lead is
  the cost.
- The 45 KB SKILL.md roughly doubled lead cost (+$0.48 mean) without changing the pick. The
  skill text is injected into the lead's context and carried every turn.
- Cutting SKILL.md to 3.4 KB, with the rest moved to REFERENCE.md read on demand, brought the
  overhead to +$0.05 mean. No slim-skill lead opened REFERENCE.md.
- Outside a proxy-enabled session, `proxy-*` spawns with the required placeholder model ran on
  `claude-haiku-4-5` while self-reporting the proxy id. `route` now drops proxy-routed candidates
  when the proxy is not active, and `record --served-model/--transcript` refuses a
  different model family. Dogfooding it surfaced a real catalog gap: `sonnet` is priced as
  `claude-sonnet-5` while the harness serves `claude-sonnet-5-5` (recorded as version drift).

## Limits

- One easy task; three trials per arm. The task saturates (cheapest worker passes), so this
  measures overhead, not routing benefit on hard tasks.
- Both arms loaded the maintainer's global agent instructions, which already tell a lead to
  prefer the cheapest proxy worker, so the no-skill arm is not an unguided baseline.
- The runner books an opus `[1m]` lead's tokens as worker rows when placeholder
  re-attribution runs; this summary takes lead cost as the opus rows.
- One slim-skill lead passed the declared id to `--served-model` instead of reading the
  transcript, so the check is only as honest as the caller unless `--transcript` is used.

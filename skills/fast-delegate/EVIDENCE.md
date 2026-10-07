# Evidence: Historical Experiments

## Note

**The experiments below measured a previous version of fast-delegate that delegated execution and verified results.** The current version (as of PR #15) only recommends models; it does not proxy execution or verify results. These historical results do not measure the current recommend-only behavior and should not be cited as current cost savings.

A new benchmark measuring recommendation quality, decision cost, and outcome tracking is pending.

---

## Experiment: 90 delegations, 3 runs per cell (2026-09-29)

### Method

Benchmark: 5 tasks, 3 runs per cell (6 workers × 5 tasks × 3 runs = 90 runs total).

**Tasks:**
- **A (retrieval, runnable check)**: Parse CLI module; extract subcommands and exit codes to answer.json; acceptance = file:line exact match.
- **B (mechanical write, runnable check)**: Implement merge_intervals and total_covered against 16 unittest tests; acceptance = all pass.
- **C (plain review, judgment)**: Find 3 planted bugs in utils.py; acceptance = all 3 found, 0 false positives.
- **D (subtle review, judgment)**: Find 3 planted bugs in retry.py (exponent off-by-one, jitter-after-cap exceeds max_delay, sleep-after-final); acceptance = all 3, 0 false positives; includes clean CircuitBreaker decoy (false-positive trap).
- **E (debug, runnable check)**: Fix topo_sort.py diamond-dependency bug; acceptance = tests pass + hidden check.

**Workers:** luna (gpt-5.6-luna), terra (gpt-5.6-terra), sol (gpt-5.6-sol) via `codex exec`; haiku (claude-haiku-4-5), sonnet (claude-sonnet-5), opus (claude-opus-5-5) via `claude -p`; all headless with clean config dirs (no CLAUDE.md/AGENTS.md), fresh workdir per run, answer keys chmod 000 during runs (0 leaks).

**Verification:** opus = lead model; every non-opus run received an opus lead-verify pass (VERDICT ACCEPT/REJECT). Verify REJECT triggered escalation to lead cost (expected opus re-run cost and accept rate added to per-cell total). Verify ACCEPT on a grader-rejected run = escaped defect.

**Prices:** LiteLLM catalog snapshot fetched 2026-09-29 14:24:28 UTC via `fdel.py discover --json`. Per-token rates (per million tokens in catalog):
- luna: $0.20 input, $1.20 output, $0.02 cache_read, $0.25 cache_creation
- terra: $2.00 input, $12.00 output, $0.20 cache_read, $2.50 cache_creation
- haiku: $1.00 input, $5.00 output, $0.10 cache_read, $1.25 cache_creation
- sonnet: $2.00 input, $10.00 output, $0.20 cache_read, $2.50 cache_creation
- opus: $4.00 input, $20.00 output, $0.20 cache_read, $5.00 cache_creation
- sol: $4.00 input, $20.00 output, $0.40 cache_read, $5.00 cache_creation

**Cost basis:** 
- "billed": claude CLI runs use reported total_cost_usd (CLI prices 1-hour cache writes at 2x input rate; catalog's 5-minute rate misses this, understates claude cost ~32%).
- "catalog" (summary.md): codex runs use catalog pricing on reported tokens; claude runs use catalog pricing (underestimates by ~32%).

**Router arms:** each task was routed 3 times with `fdel.py route --lead opus`, once without Jev and once with `--jev`. Both arms are scored on the matrix runs of the worker they pick, plus routing cost (~$0.00015 per Jev call, $0 without). Picks without Jev were identical across repeats:
- A: terra, E: terra (fallback: acceptance not recognized as runnable and no ledger record, so the most expensive candidate cheaper than the lead)
- B: luna (acceptance recognized as runnable, so the cheapest candidate)
- C: luna, D: luna (best ledger acceptance for the review family)

With Jev (`jev_routes.jsonl`): A luna ×3, B luna ×3 (required fit 2.0), C sonnet ×2 and luna ×1 (required fit 2.5; haiku narrowly missed the gate), D sonnet ×3 (required fit 3.5), E haiku ×3 (required fit 3.0, flagged: no debug evidence for haiku). The router-jev arm uses the majority pick, so C is sonnet.

**Lead-picks arm:** `bench/lead_route.py` gives opus the same task JSON and the same four candidates cheaper than the lead, with catalog prices, and asks it to pick a worker (headless `claude -p`, clean config, 3 repeats per task, `lead_routes.jsonl`). Each repeat is scored as its own router (`router-lead-1..3`), with that repeat's mean billed decision cost as its routing cost.

**Reproduce:** `python3 bench/run.py --workers luna,haiku,sonnet,terra,sol,opus --tasks A,B,C,D,E --runs 3 --parallel 6 --out <dir>` then `python3 bench/summarize.py <dir> --cost-basis=billed`.

## Results

### By Cell (Task, Worker, N, Accept, Cost $, Tokens, Wall s, Verify)

| Task | Worker | N | Accept | Cost $ | Tokens | Wall s | Verify A/R |
|---|---|---|---|---|---|---|---|
| A | haiku | 3 | 3/3 | 0.0436 | 91244 | 16.8 | 3/0 |
| A | luna | 3 | 3/3 | 0.0064 | 90795 | 42.0 | 3/0 |
| A | opus | 3 | 3/3 | 0.2694 | 150044 | 30.2 | 0/0 |
| A | sol | 3 | 3/3 | 0.0649 | 58174 | 24.5 | 3/0 |
| A | sonnet | 3 | 3/3 | 0.0851 | 75602 | 10.1 | 3/0 |
| A | terra | 3 | 3/3 | 0.0488 | 74179 | 28.1 | 3/0 |
| B | haiku | 3 | 3/3 | 0.0653 | 165537 | 27.3 | 3/0 |
| B | luna | 3 | 3/3 | 0.0034 | 58769 | 23.3 | 3/0 |
| B | opus | 3 | 3/3 | 0.2875 | 172040 | 34.5 | 0/0 |
| B | sol | 3 | 3/3 | 0.0595 | 58603 | 27.1 | 3/0 |
| B | sonnet | 3 | 3/3 | 0.0938 | 97797 | 10.2 | 3/0 |
| B | terra | 3 | 3/3 | 0.0373 | 58527 | 22.9 | 3/0 |
| C | haiku | 3 | 3/3 | 0.0582 | 89902 | 34.6 | 3/0 |
| C | luna | 3 | 3/3 | 0.0072 | 104394 | 42.2 | 3/0 |
| C | opus | 3 | 3/3 | 0.2265 | 133230 | 24.9 | 0/0 |
| C | sol | 3 | 3/3 | 0.0704 | 73851 | 27.6 | 3/0 |
| C | sonnet | 3 | 3/3 | 0.0913 | 77135 | 10.5 | 3/0 |
| C | terra | 3 | 3/3 | 0.0501 | 79983 | 29.0 | 3/0 |
| D | haiku | 3 | 2/3 | 0.1142 | 114501 | 106.5 | 2/1 |
| D | luna | 3 | 3/3 | 0.0057 | 85137 | 33.0 | 3/0 |
| D | opus | 3 | 3/3 | 0.1931 | 81907 | 18.3 | 0/0 |
| D | sol | 3 | 3/3 | 0.0946 | 73392 | 27.8 | 3/0 |
| D | sonnet | 3 | 3/3 | 0.0893 | 75545 | 12.1 | 3/0 |
| D | terra | 3 | 3/3 | 0.0300 | 53687 | 20.5 | 3/0 |
| E | haiku | 3 | 3/3 | 0.0684 | 200339 | 33.2 | 3/0 |
| E | luna | 3 | 3/3 | 0.0056 | 79134 | 36.4 | 3/0 |
| E | opus | 3 | 3/3 | 0.2599 | 160255 | 40.5 | 0/0 |
| E | sol | 3 | 3/3 | 0.0884 | 73358 | 31.8 | 3/0 |
| E | sonnet | 3 | 3/3 | 0.0871 | 75588 | 9.1 | 3/0 |
| E | terra | 3 | 3/3 | 0.0296 | 57527 | 22.9 | 3/0 |

### Strategy Totals (billed cost basis, verify included)

| Strategy | Tasks | Total Cost $ | Expected Accepted | Cost per Accepted $ |
|---|---|---|---|---|
| lead-alone | all (A–E) | 1.2365 | 5.00 | 0.2473 |
| fixed-luna | all (A–E) | 0.8541 | 5.00 | 0.1708 |
| fixed-terra | all (A–E) | 1.0107 | 5.00 | 0.2021 |
| router:router | all (A–E) | 0.9126 | 5.00 | 0.1825 |
| router:router-jev | all (A–E) | 1.0988 | 5.00 | 0.2198 |
| router:router-lead-1 | all (A–E) | 1.7183 | 5.00 | 0.3437 |
| router:router-lead-2 | all (A–E) | 1.2160 | 5.00 | 0.2432 |
| router:router-lead-3 | all (A–E) | 1.1438 | 5.00 | 0.2288 |

Catalog-basis totals (summary.md): lead-alone $0.1567, router $0.1304, router-jev $0.1534, fixed-luna $0.1179 per accepted result.

## What the evidence shows

1. **89 of 90 runs accepted; 0 escaped defects.** Haiku failed once on D (run 1: missed jitter-after-cap and reported one false positive), and the lead verify rejected that run. The lead verifier accepted the other 74 of 75 verified runs, and every one of them also passed the grader: no false rejects and no escaped defects.

2. **Cost per accepted result (billed basis):** lead-alone $0.2473; router without Jev $0.1825 (−26% vs lead); router with Jev $0.2198 (−11%); fixed-luna $0.1708 (−31% vs lead); fixed-terra $0.2021 (−18%); fixed-sol $0.2339 (−5%); fixed-haiku $0.2550 and fixed-sonnet $0.2564 cost slightly more than the lead doing the work itself.

3. **Verification dominates delegated cost.** One opus verify pass averages $0.165 (billed, 75 runs), while a luna run averages about $0.006, so verification is roughly 95% of a fixed-luna result's cost. A delegated result can never cost less than its verification, so savings are capped by verification cost, not by worker price.

4. **The router's terra picks on A and E cost more than luna, with no quality gain.** Both tasks have runnable checks, but their acceptance text ("all 5 tests pass", "answer.json names and lines match the source") was not recognized as runnable. With no ledger record, the fallback took the most expensive candidate cheaper than the lead. The no-Jev fallback rule's runnable-check detection is too narrow.

5. **Jev routed cautiously, and on this set caution did not pay.** Jev read the two runnable tasks correctly as easy and verifiable (A difficulty ~0.2, B ~1.8; verifiability ~3.5–3.9) and picked luna. It rated the reviews as hard to verify (verifiability ~1.7) and raised the required fit, which sent C and D to sonnet. Luna, the cheapest worker, was 3/3 on both reviews, so the upgrade bought nothing: the Jev arm's judgment-task cost was $0.2508 per accepted result against $0.1665 for luna. The same caution sent E to haiku, which costs more than luna here with no quality gain. That is the right behavior if cheap tiers really miss subtle bugs, but this saturated set can't show it; a harder judgment set is needed to tell whether Jev's escalation earns its cost.

6. **Jev makes the lead's routing decision at a small fraction of the lead's cost.** Asked the same question, opus chose exactly as Jev did on A, B, C and D (luna, luna, sonnet, sonnet). On E, opus was inconsistent across its own repeats (haiku, luna, sonnet), and Jev picked haiku every time. The cost of one routing decision:

   | Router | Cost per decision | Latency |
   |---|---|---|
   | Jev (`route --jev`) | $0.00015 | ~150 ms |
   | opus, warm cache (9 calls) | $0.0105 median (71×) | ~3.7 s median |
   | opus, all 15 calls including cold cache writes | $0.058 mean (390×) | |

   When opus routes, the decision cost alone lifts its strategy to $0.229–0.344 per accepted result, as much as or more than opus doing the work itself ($0.247). The routing decision itself is cheap with Jev. Which worker to escalate to is a separate question (point 5).

## Limitations

- **Small, saturated task set.** A and B are simple retrieval/implementation; C and D are code review on small fixtures (30–60 line files). All tasks are graded on correctness only, and every worker passed A, B, C and E. That ceiling effect means this set cannot distinguish tiers on judgment work.
- **2026-09-24 finding did not reproduce.** Earlier single-run data suggested only the top tier found 3 of 3 subtle bugs (D). This benchmark shows luna and terra both went 3/3 on D after the fixture was corrected to actually contain the three bugs. That prior finding is now unconfirmed; the data remains in git history.
- **3 runs per cell, 1 lead model.** A 3-run cell cannot resolve small differences in acceptance rate. Only opus verified, so the cost of a cheaper verifier is not measured.
- **Prices are snapshot.** LiteLLM catalog prices change; codex cost is estimated from catalog rates (not billed), so actual codex spend would differ from projected.
- **Lead routing cost is measured headless.** Each `claude -p` call carries Claude Code's ~28k-token system prompt; a cold call writes it to a 1-hour cache. Inside a running session, a routing decision would cost roughly the warm figure, which is still ~70× Jev.
- **Quota was not exercised.** Every worker here is metered, so the quota-aware filtering and pacing had no effect on picks.
- **Jev arm is route-only.** Jev picked only workers already in the matrix, so its arm reuses those runs; no worker was re-run under Jev routing. C's pick varied (sonnet 2/3, luna 1/3), and the arm uses the majority pick.
- **Only one verification method measured.** Each delegated run got a full opus verify pass. Cheaper verification (running the acceptance check only, or a cheaper verifier model) was not tested, and it is the main lever on cost.

## Next

- Harder judgment tasks to test tier separation on this dataset.
- Re-run the Jev comparison on harder judgment tasks, where escalating to a stronger worker could pay off.
- Cheaper verification: measure runnable-check-only (no judgment grading) and cheaper verifier cost.
- More runs per cell to reduce variance; multi-lead verification to estimate verifier reliability.

## J. Jev catalog matching for open models (2026-09-24)

Live probe of catalog matching for open models with varying names/versions. 50 queries
over ~6.5k input tokens, 400 ms, ~$0.0003 cost. Specialization filter (coder vs generic)
improves precision when multiple versions exist; size matching ensures correct variant
selection. A ranked shortlist with Jev validation outperforms choice-based scoring.

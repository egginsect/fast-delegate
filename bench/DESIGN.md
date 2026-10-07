# Benchmark Design for Recommend-Only fast-delegate

## Overview

Since PR #15, fast-delegate is a routing-only recommender. It returns scored recommendations but does not proxy execution or verify results. The benchmark must measure recommendation quality and decision economics, not delegation savings.

## What We Measure

### Strategies

1. **lead-alone**: Lead (opus) executes every task directly. No routing calls.
   - Cost: execution cost only
   - Baseline for comparison

2. **route-heuristic**: Lead follows fast-delegate's heuristic recommendation (no Jev)
   - For each task: `fdel.py route --task <json> --lead opus --harness claude --spawnable haiku,sonnet,opus --no-task-jev --brief`
   - Lead reviews recommendation and decides whether to follow it
   - Cost: routing cost + execution cost (recommended model if accepted, lead if rejected)

3. **route-jev**: Lead follows fast-delegate's Jev-scored recommendation
   - For each task: `fdel.py route --task <json> --lead opus --harness claude --spawnable haiku,sonnet,opus --brief`
   - Lead reviews recommendation (fit score, price, evidence) and decides
   - Cost: routing cost + execution cost

4. **fixed-<model>**: Lead follows a fixed cheaper model recommendation (baseline)
   - E.g., always recommend luna, lead decides whether to accept
   - Cost: execution cost only (no routing)

### Measurements Per Task

For each strategy and task:

1. **Routing call** (if applicable):
   - Command: `fdel.py route --task <json> --lead opus ...`
   - Output: recommendations list (scored, ordered)
   - Cost: routing call cost (from catalog/billed)
   - Latency: routing decision time

2. **Lead review** (all strategies):
   - Prompt: "Here is a task and a recommended model [or: a list of recommendations]. Review the recommendation and decide: ACCEPT (use the recommended model) or REJECT (I'll do it myself). Consider the task complexity, the model's capabilities from the evidence, and the risk of a failed attempt."
   - For route-heuristic/route-jev: include the full recommendation output
   - For fixed-<model>: include a simple "recommended: <model>, price: $X"
   - Output: ACCEPT or REJECT + brief reasoning
   - Cost: lead review call cost

3. **Execution**:
   - If ACCEPT: run the recommended model on the task
   - If REJECT: run the lead on the task
   - Cost: execution cost
   - Outcome: grader acceptance (pass/fail)

4. **Per-task total**:
   - Total cost = routing cost + review cost + execution cost
   - Accepted: whether the grader accepted the final result
   - Decision: which model was recommended, whether lead accepted recommendation
   - Outcome: which model ran, whether it passed

### Aggregate Metrics

Per strategy, across all tasks:

1. **Cost per accepted result**: total cost / count of accepted results
2. **Recommendation acceptance rate**: how often lead accepts the recommendation
3. **Recommended model success rate**: when lead accepts recommendation, how often does it pass?
4. **Fallback rate**: how often does lead reject and run itself?
5. **Savings vs lead-alone**: (lead-alone cost - strategy cost) / lead-alone cost

### Key Differences from Old Benchmark

| Old (delegate-and-verify) | New (recommend-only) |
|---|---|
| Router decides, executes worker, lead verifies | Router recommends, **lead decides**, then execute |
| Verification cost dominates (~95% of delegated cost) | No verification; routing + review + execution |
| Measured: delegation savings | Measure: recommendation quality + decision economics |
| Strategies: router pick vs fixed worker | Strategies: follow recommendation vs ignore it |
| All runs were headless | Review step requires lead to see recommendation |

## Implementation Sketch

### New Files

1. **`bench/route_tasks.py`**: Generate task.json files from existing prompts
   - Input: bench/tasks/{A,B,C,D,E}/prompt.md
   - Output: bench/tasks/{A,B,C,D,E}/task.json (deliverable, acceptance, context)

2. **`bench/run_recommend.py`**: New harness for recommend-only benchmark
   - For each (strategy, task, run):
     - Call router (if applicable) → recommendation
     - Lead reviews recommendation → ACCEPT/REJECT
     - Execute recommended or fallback model
     - Grade result
     - Record: routing cost, review cost, execution cost, decision, outcome

3. **`bench/summarize_recommend.py`**: Aggregate recommend-only results
   - Per-strategy totals
   - Cost per accepted result
   - Recommendation acceptance and success rates

### Task JSON Schema

```json
{
  "deliverable": "one-sentence description of what to produce",
  "acceptance": ["criterion 1", "criterion 2", ...],
  "context": {
    "files": ["list of files the worker sees"],
    "verifiable": true|false,
    "independence": true|false
  }
}
```

Example for Task A:
```json
{
  "deliverable": "Parse cli.py and extract all subcommands and exit codes to answer.json",
  "acceptance": [
    "answer.json exists",
    "Every subcommand name and source line matches the fixture exactly", 
    "Every exit code name, value, and source line matches the fixture exactly"
  ],
  "context": {
    "files": ["cli.py"],
    "verifiable": true,
    "independence": true
  }
}
```

### Lead Review Prompt

```
You are deciding whether to delegate a task to a cheaper model or do it yourself.

Task:
{task.json}

Recommendation:
{recommendation output from fdel.py route --brief}

Consider:
1. Task complexity and acceptance criteria
2. Recommended model's capabilities and evidence (fit score, family history)
3. Risk: if the recommended model fails, you'll need to redo it yourself
4. Cost: recommended model is cheaper, but a failed attempt wastes that cost

Reply with exactly one line: `DECISION: ACCEPT` or `DECISION: REJECT`, followed by one sentence explaining why.
```

### Expected Results Shape

After running the new benchmark:

```
Strategy: lead-alone
  Cost per accepted: $0.XXX (baseline)
  
Strategy: route-heuristic  
  Cost per accepted: $0.YYY (-Z% vs lead)
  Recommendation acceptance rate: P%
  Recommended model success rate: Q% (when accepted)
  Routing cost per task: $0.0000X
  
Strategy: route-jev
  Cost per accepted: $0.ZZZ (-W% vs lead)  
  Recommendation acceptance rate: R%
  Recommended model success rate: S% (when accepted)
  Routing cost per task: $0.00015 (avg)
```

Key questions this answers:
1. Does the lead trust fast-delegate's recommendations?
2. When it follows them, do they succeed?
3. What is the net cost considering routing + review + execution?
4. Is Jev-scored routing worth its cost vs heuristic?

## Running the New Benchmark

(After implementation)

```bash
# Generate task.json files from prompts
python3 bench/route_tasks.py

# Run 3 runs per strategy per task (5 tasks × 3 strategies × 3 runs = 45 executions)
# Estimated cost: ~$15-20 (mostly lead execution + review calls)
python3 bench/run_recommend.py \
  --strategies lead-alone,route-heuristic,route-jev \
  --tasks A,B,C,D,E \
  --runs 3 \
  --parallel 3 \
  --out bench/results/recommend_$(date +%Y%m%d_%H%M%S)

# Summarize
python3 bench/summarize_recommend.py bench/results/recommend_<timestamp>
```

## Status

**This design document describes the planned benchmark.** Implementation of `run_recommend.py` and `summarize_recommend.py` is pending. The old `run.py` harness remains available for historical delegate-and-verify measurement, but its results do not apply to the current recommend-only behavior.

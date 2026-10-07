# Benchmark Harness

## Recommend-Only Benchmark (Current)

**Measures the current fast-delegate behavior** (since PR #15): router recommends, lead decides whether to follow, then execute.

### Prerequisites

- Python 3.10+
- `claude` CLI authenticated and configured
- fast-delegate installed (or set `BENCH_FDEL` environment variable)
- Task JSON files generated (`python3 bench/route_tasks.py` - already done)

### Quick Start

```bash
# Dry-run to verify setup
python3 bench/run_recommend.py --dry-run

# Full benchmark: 3 strategies × 5 tasks × 3 runs = 45 executions
python3 bench/run_recommend.py \
  --strategies lead-alone,route-heuristic,route-jev \
  --tasks A,B,C,D,E \
  --runs 3 \
  --parallel 3 \
  --out bench/results/recommend_$(date +%Y%m%d_%H%M%S)

# Summarize results
python3 bench/summarize_recommend.py bench/results/recommend_<timestamp>
```

### Estimated Cost

**~$15-20 USD** for the full 45-execution run:
- Lead execution: ~$10-12 (when lead does the work itself or rejects recommendations)
- Review decisions: ~$3-5 (lead reviewing recommendations)
- Routing calls: ~$0.01-0.02 (fast-delegate route calls)
- Recommended model execution: ~$2-3 (when lead accepts cheaper recommendations)

**Faster test run** (6 executions, ~$2-3 USD):
```bash
python3 bench/run_recommend.py \
  --strategies lead-alone,route-jev \
  --tasks A,B \
  --runs 1 \
  --out bench/results/recommend_test_$(date +%Y%m%d_%H%M%S)
```

### What It Measures

**Strategies:**
1. **lead-alone**: Lead (opus) executes every task directly (baseline)
2. **route-heuristic**: Lead reviews heuristic recommendations (no Jev), decides to follow or not
3. **route-jev**: Lead reviews Jev-scored recommendations, decides to follow or not

**Per-strategy metrics:**
- Cost per accepted result
- Recommendation acceptance rate (how often lead trusts the recommendation)
- Success rate when followed (when lead accepts, does recommended model succeed?)
- Cost breakdown: routing + review + execution

See [DESIGN.md](DESIGN.md) for complete specification.

### Tests

Run the benchmark test suite:
```bash
make -C bench acceptance
```

This validates routing, review, execution, and summarization logic with mocked model calls.

---

## Historical Delegate-and-Verify Benchmark

**This measures the OLD flow** (pre-PR #15) where fast-delegate delegated execution and verified results.

### Prerequisites

- Python 3.10+
- `claude` CLI authenticated and configured
- `codex` CLI authenticated and configured (or only run Claude workers)
- fast-delegate installed at `~/.claude/skills/fast-delegate/` (or set `BENCH_FDEL` environment variable)

### Command

Run the full 90-cell benchmark (5 tasks × 6 workers × 3 runs):

```bash
python3 bench/run.py \
  --workers luna,haiku,sonnet,terra,sol,opus \
  --tasks A,B,C,D,E \
  --runs 3 \
  --parallel 6 \
  --out bench/results/$(date +%Y%m%d_%H%M%S)
```

### Historical Cost Estimate

Based on the 2026-09-29 run (OLD delegate-and-verify flow):
- **Total billed cost**: ~$20.30 USD
- **Catalog-basis estimate**: ~$14.40 USD (actual billed costs run ~1.47× catalog due to 1-hour cache write pricing)
- **Per-cell average**: ~$0.23 USD (90 cells total)

Breakdown by component:
- Worker execution: ~$3–5 USD
- Opus verification (75 verify passes): ~$12–13 USD
- Routing calls: < $0.01 USD

**Note**: This OLD flow measured execution + verification cost. The current recommend-only benchmark measures routing + review + execution cost instead.

### Faster/Cheaper Test Run

Test with a subset:

```bash
# 2 workers × 2 tasks × 1 run = 4 cells (~$1 USD)
python3 bench/run.py \
  --workers luna,haiku \
  --tasks A,B \
  --runs 1 \
  --parallel 2 \
  --out bench/results/test_$(date +%Y%m%d_%H%M%S)

# Skip verification to save ~60% of cost
python3 bench/run.py \
  --workers luna,haiku \
  --tasks A,B \
  --runs 1 \
  --parallel 2 \
  --no-verify \
  --out bench/results/test_noverify_$(date +%Y%m%d_%H%M%S)
```

### Generating the Summary

After a run completes:

```bash
# Catalog-basis pricing (default)
python3 bench/summarize.py bench/results/<timestamp>

# Billed-basis pricing (uses CLI-reported total_cost_usd)
python3 bench/summarize.py bench/results/<timestamp> --cost-basis=billed
```

This writes `summary.json`, `summary.md` (or `summary-billed.json`, `summary-billed.md`) to the results directory.

### Historical Tests

Run the historical benchmark test suite:

```bash
# Tests for the OLD delegate-and-verify harness
python3 -m unittest bench.tests.test_bench

# Tests for the NEW recommend-only harness  
python3 -m unittest bench.tests.test_recommend

# Or run all bench tests together
make -C bench acceptance
```

# Pilot fixed-worker-surrogate versus advisory-selector protocol

Compare a user-approved fixed Sol worker baseline with cost-conscious live advisory
routing and an autonomous decision model's native worker choice. This is a reusable manual
protocol, not an executor or a universal harness API. The fixed baseline is a
surrogate: a direct terminal fresh Sol worker with sub-delegation prohibited and
no Jev or selector phase. The routed arm uses a fresh Sol decision model to choose
an executor from advisory routing references; the caller dispatches that choice.
The selector does not itself spawn the terminal executor. This pilot is not a
natural Sol lead harness autonomously planning execution or delegation with versus
without routing, and it is not evidence of the serving lead's identity or cost.
Astra and Fable must not execute in either arm, routing helpers, or judging.

## Standard steps

1. **Predeclare one task and authorize the budget.** Before a recommendation,
   freeze one concrete task contract: objective, Task / Return / Constraints,
   owned paths (empty for read-only audits), inputs/fixtures, acceptance criteria,
   return size, tools, permissions, token limits, timeout, retry limit, and stop
   condition. No sub-delegation. Existing explicit authorization persists for the bounded experiment; do not
   request it again. Obtain authorization when absent for any paid routing,
   worker, and judge requests and their bounded total cost; otherwise use only
   authorized local operations. One recommendation concerns that one task, not
   a menu the worker can choose from. Register the task and comparison plan before
   observing outputs. Relevant examples include lossless compact-payload edge
   cases, independent pricing calculations, and synthetic quota normalization;
   these are task families, not mandatory benchmark contents.

2. **Freeze the controls and prepare isolated worktrees.** Both arms use the
   same HEAD, task text, input/fixture hashes, acceptance criteria, tool
   permissions, token budgets, timeout, and return budget. Use committed input
   or identical explicitly inventoried non-secret fixtures; record dirty state
   without copying unrelated changes. Local preparation below creates two new
   detached worktrees and isolated evidence/state directories. It does not
   change the shared branch or stage/commit anything.

   ```bash
   REPO_ROOT=$(git rev-parse --show-toplevel)
   EXP_ROOT=$(mktemp -d /tmp/fdel-paired.XXXXXX)
   BASE_HEAD=$(git -C "$REPO_ROOT" rev-parse HEAD)
   mkdir "$EXP_ROOT/evidence" "$EXP_ROOT/router-state"
   git -C "$REPO_ROOT" worktree add --detach "$EXP_ROOT/fixed" "$BASE_HEAD"
   git -C "$REPO_ROOT" worktree add --detach "$EXP_ROOT/routed" "$BASE_HEAD"
   python3 - "$EXP_ROOT" "$BASE_HEAD" <<'PY'
   import json, pathlib, sys
   root = pathlib.Path(sys.argv[1])
   plan = {
       "schema_version": 1, "head": sys.argv[2],
       "task_id": "replace-before-routing", "task_contract": None,
       "fixed_selector": "gpt-6.1-sol", "routing_call_limit": 1,
       "attempt_limit_per_arm": 1, "paid_calls_authorized": False,
       "acceptance": [], "tool_permissions": [], "token_budget": None,
       "timeout_seconds": None, "input_hashes": {},
       "excluded_execution_models": ["gpt-6-astra", "fable"]
   }
   (root / "evidence" / "plan.json").write_text(
       json.dumps(plan, indent=2) + "\n")
   PY
   ```

   Fill and validate every placeholder before execution. Record absolute paths
   and HEAD readbacks. Do not copy credentials, `.env*`, secret files, credential
   mounts, or provider/deployment state between checkouts. Authentication, if
   authorized, stays in the harness's existing credential helper and memory.
   Never persist API keys or raw 1Password (OP) responses. Sanitize error output
   before saving. Never delete unrelated worktrees; retain these until review,
   and only remove experiment-owned worktrees after verifying paths and contents.

3. **Discover native availability and route once.** Verify actual harness model
   selectors/configuration, required tools, write/isolation capabilities, and
   known quota headroom/freshness. Hard eligibility checks cover known capability,
   access, exclusion, and quota facts. Label missing/stale facts unknown; do not
   invent availability or silently treat unknown quota as fresh. Semantic fit,
   historical acceptance, and difficulty judgments are advisory, without hard
   semantic fit gates. Record metadata provenance and timestamps.

   The caller may adapt this CLI shape to a supported harness after authorization;
   it is illustrative and is **not** a command to execute in this documentation
   task. Supply only verified native selectors and a locally authored non-secret
   task JSON. `--lead` is the router's explicit comparison reference, not proof
   of the serving lead model. Keep that distinction in evidence.

   ```bash
   FAST_DELEGATE_STATE="$EXP_ROOT/router-state" python3 \
     "$REPO_ROOT/skills/fast-delegate/scripts/fdel.py" route \
     --task "$EXP_ROOT/evidence/task.json" \
     --lead "$ROUTER_COMPARISON_REFERENCE" --harness codex \
     --spawnable "$VERIFIED_NATIVE_SELECTORS" \
     > "$EXP_ROOT/evidence/route.json"
   ```

   `VERIFIED_NATIVE_SELECTORS` must be one comma-separated value (for example
   `gpt-6.1-sol,gpt-6-luna`), not whitespace-separated model arguments.

   Bound this to one routing invocation per task, including any optional semantic
   guidance/model matching requests within its separately declared request/cost
   cap. Disable hidden retries or document their bounded policy. A failed call
   consumes the budget; do not keep rerouting. Record route ID, recommendation
   list, selector/configuration, fallback basis, exclusions, usage, and errors.
   A verified metadata fallback when Jev is unavailable is an explicit outcome,
   not live semantic evidence. If no native candidate is eligible, stop that arm
   and report availability failure.

4. **Have an autonomous decision model choose and hand off explicitly.** A fresh
   independent decision model evaluates the recorded eligible recommendations
   and chooses the executor itself. Do not preassign the cheap executor. The lead
   checks eligibility and dispatches the chosen selector without substituting a
   predetermined choice. Charge every decision-model phase to the routed path. Record chosen identity,
   estimated price basis, rationale, and any override of the initial candidate.
   Do not equate a model selector with a universal spawn API enum. Translate the
   recommendation's `model_selector` and `model_configuration` through this
   harness's documented native model-selection mechanism; record the actual
   dispatched selector/configuration and verified runtime identity when available.
   Unsupported translation is an availability failure, not a semantic defect.

   Give each worker the same bounded contract in its own worktree. Use a fresh
   context and `fork_context=false` where supported; otherwise document inherited
   context and resulting contamination risk. Neither worker receives counterpart
   output, verdicts, or price information. Do not switch models implicitly.
   Randomize or alternate arm order across registered pairs and record order.

5. **Capture each worker phase at its boundary.** Save sanitized artifacts and
   reproducible commands/results, not credentials or raw provider state. Capture
   scalar input, cached-input, and output token counts immediately at each worker
   attempt/phase boundary, with source and count semantics. Exclude routing,
   judging, preparation, and main-lead usage from worker counts. If the harness
   only exposes cumulative counters, record start/end snapshots and justified
   deltas; do not guess attribution. Missing counts remain null/unknown, not zero.
   Preserve per-request counts for pricing even when reporting phase sums.

6. **Judge independently and blindly.** An independent judge receives anonymous
   artifacts plus the original task, acceptance criteria, and reproducibility
   evidence. Hide model, arm, route, costs, and counterpart artifact. Predeclare
   the judge method and budget; it must not execute excluded models. Assess each
   criterion and reproduce material findings in an isolated read-only review.
   Save the blind verdict before revealing identities. The main lead then owns
   final accept/reject, with evidence and any disagreement explained. A cheap
   output passes only if it meets the same predeclared criteria as the baseline.

7. **Price requests individually and record outcomes.** Match the actual worker
   identity/provider/billing mode to a dated catalog entry; record uncertainty
   and provenance. Invoices are unknown unless actually observed. Subscription
   catalog equivalents are shadow costs, not cash charges. For request `r`, when
   total input `I_r` includes cache-read tokens `K_r`:

   ```text
   C_r = ((I_r - K_r) * p_input(r) + K_r * p_cache(r)
          + O_r * p_output(r)) / 1_000_000
   phase_cost = sum(C_r for requests in that phase)
   ```

   Rates here are per million tokens. If input excludes cached tokens, replace
   `(I_r - K_r)` with `I_r`. Resolve each request's applicable tier from the
   provider's documented threshold rules and that request's context/counts;
   never select tiers from aggregated phase totals. Add separately metered cache
   writes or other fees using their documented units. Unknown cache rates,
   ambiguous identities, missing request counts, or unknown tiers make exact cost
   unknown; report labeled bounds only when justified. Keep estimate, matched
   catalog equivalent, and invoice amounts separate. Router and judge overheads
   have their own request records and totals.

   Record usage evidence and each routed attempt's accepted/rejected outcome with
   its route ID. Current fdel ledger syntax, after setting the local state path:

   ```bash
   FAST_DELEGATE_STATE="$EXP_ROOT/router-state" python3 \
     "$REPO_ROOT/skills/fast-delegate/scripts/fdel.py" record \
     --route "$ROUTE_ID" --candidate "$CHOSEN_CANDIDATE" \
     --family "$TASK_FAMILY" --outcome "$VERDICT"
   ```

   When choosing a different lower-cost candidate, append `--override` with the
   lead's concrete reason. Use ledger-supported usage fields only for their
   documented semantics; retain full scalar usage in experiment evidence.
   Record the fixed arm separately, without inventing a route ID. Preserve failed
   attempts and their costs before any retry.

8. **Stop and report paired results.** Separate launch/access/quota/transport
   failures from semantic defects in completed outputs and verification failures.
   A retry or fallback must consume a predeclared attempt budget, preserve the
   original result, and use the existing recommendation list; no new routing
   call, repeated candidate loop, or infinite recovery cycle. Stop when the list
   is exhausted, the attempt/cost limit is reached, or the stop condition passes.
   Report first-pass acceptance, final acceptance, every attempt's outcome and
   cost, and all-attempt cost per arm. Do not hide repair costs behind final
   acceptance. Any altered task/input/criteria becomes a newly registered pair.


## Minimum context pack and quality preservation

Before handoff, prepare and review a sufficient bounded pack: goal and deliverable;
source revision and relevant source/fixture references; owned paths and constraints;
prior choices and their reasons; acceptance and verification commands; known pitfalls,
unresolved questions, and authoritative references. Check that the worker can execute
and verify the contract using that pack alone. Include relevant decisions from prior
context rather than requiring the worker to rediscover them. Both paired executors
receive equivalent task information; selector reasoning and counterpart output stay
outside their packs. Do not inherit an entire lead conversation by default.

Record context preparation and transmission overhead separately, including tokens
or bytes when measured; unknown costs are not zero. Count autonomous decision-model
usage in routed totals, judges separately by task/arm, and superseded setup separately
and in the grand total. Do not repair a benchmark artifact before first-pass judging.
An initial judge prompt with unrelated-domain requirements is a scope error attributable
to the lead; retain its cost but use only the corrected final in-scope verdict.

Distinguish fresh worker contexts from a continued main-lead context. Fresh context
is not proof of cold cache: use actual per-request cached counts and summed shares.
Do not infer hits from latest-chunk timestamps. Prompt-prefix computation caching,
transport payload, and response-state continuation are distinct mechanisms; neither
configured WebSocket state nor upstream provider is established by generic docs.
Preserve unknown billing mode, cache-write accounting, invoice, provider and model
identity as unknown rather than presenting catalog proxies as actual cash savings.

For shared evidence, export anonymous task/arm/phase/model names and scalar token
records only. Do not export session IDs, GUIDs, private paths, nicknames, raw prompts,
private configuration or provider state. Read scalar usage and required model metadata
only; deduplicate response records internally without persisting their identifiers.
Assert phase sums match final cumulative totals, never sum repeated cumulative fields.
Sanitized fixture code must use relative paths and contain no live provider data.

## Evidence schema

Use non-secret local JSON with these fields; null means unknown. This is a
portable evidence contract, not an implementation-specific request payload.

```json
{
  "schema_version": 1,
  "experiment_id": "local-unique-id",
  "task": {"id": "predeclared", "family": "audit", "contract_hash": "sha256", "acceptance": []},
  "controls": {"head": "commit", "input_hashes": {}, "tool_permissions": [], "token_budget": null, "timeout_seconds": null, "attempt_limit": 1, "arm_order": [], "fresh_context": true},
  "authorization": {"paid_calls": false, "cost_cap": null, "currency": null},
  "routing": {"route_id": null, "invocations": 0, "comparison_reference": null, "recommendations": [], "chosen_candidate": null, "override_reason": null, "fallback_basis": null, "capability_quota_provenance": []},
  "attempts": [{
    "arm": "fixed-or-routed", "attempt": 1, "phase": "worker",
    "route_id": null, "selector": null, "configuration": {}, "runtime_identity": null,
    "requests": [{"input_tokens": null, "cached_input_tokens": null, "output_tokens": null, "input_includes_cache": null, "usage_source": null, "catalog_id": null, "catalog_date": null, "billing_mode": null, "tier": null, "rates_per_million": {"input": null, "cache": null, "output": null}, "catalog_equivalent_cost": null, "invoice_cost": null}],
    "artifact_hashes": {}, "failure_class": null, "blind_label": null,
    "blind_verdict": null, "criterion_evidence": [], "lead_verdict": null,
    "lead_reason": null
  }],
  "overheads": {"routing_requests": [], "judge_requests": [], "preparation_cost": null, "main_lead_cost": null},
  "results": {"fixed": {"first_pass_acceptance": null, "final_acceptance": null, "all_attempt_cost": null}, "routed": {"first_pass_acceptance": null, "final_acceptance": null, "all_attempt_cost": null}},
  "limitations": []
}
```

Attach timestamped capability/quota observations, sanitized route output, blind
label mapping retained outside judge input, verdict evidence, and rate formulas.
Record whether a non-started attempt was unjudged rather than a semantic rejection.

## Interpretation limits

A fixed Sol surrogate does not measure the current main lead's cost. Small selected
cohorts and stochastic worker variation do not establish general routing accuracy
or causal savings. Cache warmth, execution order, unknown runtime identities,
quota freshness, fallback routing, and inherited contexts can affect comparisons.
Report router/judge overheads separately and include them in any measured total;
main-lead and preparation costs remain explicitly unmeasured unless captured.
If both arms choose Sol, report that fact without claiming a cheaper-model win.
Acceptance and catalog-equivalent cost are separate observations; neither proves
an invoice saving or reliable adequacy on future tasks.

## Optional continued-context control

A separately registered continued-main-context arm can test a warm-context hypothesis
only when its runtime usage, cache shares, task information and overhead are measured.
Keep it separate from the direct fresh-Sol no-routing baseline; do not claim that the
fresh-worker comparison measures a warm lead. Distinguish actual observations from
modeled warm/cold sensitivity scenarios and account for context-pack preparation.

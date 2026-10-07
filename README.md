[![Acceptance](https://github.com/egginsect/fast-delegate/actions/workflows/acceptance.yml/badge.svg)](https://github.com/egginsect/fast-delegate/actions/workflows/acceptance.yml)

# fast-delegate

fast-delegate recommends a model for one predefined delegation task. It compares candidate pricing, context and tool support, quota, ledger outcomes, and (by default) TypeSafe Jev's fit judgment. The caller owns task handoff, dispatch, and verification because those steps depend on the active harness.

## Install

```sh
/plugin marketplace add egginsect/fast-delegate
/plugin install fast-delegate@fast-delegate
```

Or clone the repository and run the installer, which detects the agent CLIs on the machine and installs the skill for each:

```sh
sh install.sh            # every detected runtime
sh install.sh codex      # only the named runtime(s): claude, codex, cursor
sh install.sh --force    # overwrite an existing install
```

A runtime counts as detected when its CLI (`claude`, `codex`, `cursor-agent`/`cursor`) is on `PATH` or its config directory (`~/.claude`, `~/.codex`, `~/.cursor`) exists. Claude Code gets `${CLAUDE_CONFIG_DIR:-~/.claude}/skills/fast-delegate`; Codex and Cursor share `~/.agents/skills/fast-delegate`. Every target is checked before anything is written, so a refusal leaves no partial install. If you installed the Claude Code plugin, skip `claude` here to avoid a duplicate skill.

## Recommend a model

Provide one task description and the model selectors available to the active harness. The selector list is discovery data; the output does not contain tool arguments or a prepared handoff.

```sh
python3 ~/.agents/skills/fast-delegate/scripts/fdel.py route \
  --task task.json --lead <your-model> --harness codex \
  --spawnable <available-model-selectors> --brief
```

`route` returns `decision`, evidence, and, when delegating, a candidate with stable `id`, `model_selector`, underlying `model_id`, catalog identity, estimated cost, and ranking evidence. A `direct` result has no candidate. The caller must choose a candidate using the completion-first policy below and explicitly select that model in its harness's delegation mechanism; model selection must not be silently inherited or omitted. `id` and `model_selector` identify the routed candidate, while `model_id` identifies the underlying priced model. A custom-agent selector may need translation through the active harness configuration; these identifiers are not promised to be verbatim tool arguments. The router does not create handoff files, prompts, or spawn instructions. `--brief` only compacts the same recommendation.

`--lead` (or `FAST_DELEGATE_LEAD`) is required unless configured in a local harness override. The router only recommends candidates with known blended prices strictly below the lead. Unknown prices cannot pass the cost safeguard. Subscription and local deployments report `shadow_cost`, not dollar savings.


The routing objective prioritizes completing the full deliverable and all acceptance
criteria through sufficient task capability, not maximum capability or perfection.
Cost, fresh quota headroom, recent route health and supplied model/provider usage
distribution inform balanced selection among credible full-completion choices.
Diversity is a tie or close-choice preference, never forced unqualified selection;
missing or stale evidence remains unknown, not zero. A cheap failed attempt plus repair and
verification may cost more. The main model may select a higher-priced eligible worker
when concrete task capability and acceptance coverage support fuller completion,
never because of prestige or price as quality. Scores remain advisory, not actual
success probabilities or a cost/probability optimizer; small-sample history is not
universal capability proof. Jev judges capability under the task's
difficulty, verification, context and tool constraints. Cheap pricing and small score
differences do not prove capability; missing benchmarks or history remain unknown.
Supplied difficulty is a numeric score from 0 through 4; absent or invalid values are
unknown. Cached input share is a caller-supplied pricing estimate from 0 through 1,
with its basis stated in the request; absent or invalid values remain unknown.
Neither the estimate nor a latest user-chunk timestamp proves a cache hit.

`recommendations` is the authoritative list for one task, identical in full and
`--brief` output. Every usable judged candidate passing runtime and model metadata
safeguards is included, including score zero. There is no semantic fit threshold,
probability-mass gate, floor, or two-entry limit. The existing Jev question budget
bounds judgment coverage; unjudged candidates are not scored recommendations.

Ordering uses raw `fit_score` descending. Probability-only evidence uses its validated
normalized distribution's expectation. Ties use quota-adjusted estimated cost, then
stable id. Each record retains the explicit `model_selector`, model identity and
configuration, raw distribution, price/cost, quota when known, and family history.
Independence, verifiability, difficulty and historical acceptance are advisory evidence.
Prices remain separate from strength; missing strength evidence remains unknown.

The main model uses `recommendations` as references for its own selection judgment.
Assess credible completion of the full deliverable and every acceptance criterion
first, under the task's tools, context, verification and access constraints. Seek
sufficient capability for full acceptance, not maximum capability or perfection.
Among credible full-completion choices, consider cost, fresh quota headroom, recent
route health, and model/provider usage distribution only from supplied fresh facts.
Unknown quota must not be treated as healthy quota.
Prefer diversity in ties or close adequate choices to balance quota usage; never
force random diversity or an unqualified model. Missing or stale evidence is unknown,
not zero. Do not fabricate recent usage or aggregate headroom across pools.
Account for a failed attempt's repair and verification work when comparing costs.
Do not always choose the highest fit, cheapest or most expensive model. Justify a
higher price by concrete risk reduction or capability benefit for full acceptance;
model-information strength must come from supplied evidence, not prestige or price.
Explain what a cheaper alternative lacks for this task using concrete capabilities
or risk, not scores, prestige or history alone. Sparse history must not become a
self-reinforcing preference: unobserved models are not incapable, and thin selected
samples such as 1/1 versus 0/1 cannot alone justify always choosing the strongest or
known model. A cheaper adequate or exploratory choice is defensible for bounded,
verifiable, low-risk work without compromising full acceptance or forcing diversity.
Protected-provider, authorization and capability safeguards still apply. Do not auto-use
the legacy cheapest `candidate` or treat score order as an execution decision.
Recommendations are score-ranked advisory evidence, not a cost-sorted selection policy.
No score or distribution is an actual success probability, automatic acceptance,
or a hard capability threshold. The main model owns selection and acceptance.

Every record requires lead acceptance (`review_required: true`); no score automatically
accepts or rejects a worker. The lead may reject the highest score or accept a lower
score, including zero, using its own task context and evidence. After an availability
failure the caller may try its next accepted candidate without rejudging. The caller
owns dispatch, availability, task verification, and recording; the router executes no
workers and produces no handoff artifacts.

The compatibility `candidate` is the cheapest metadata-safe recommendation, independent
of score order; it is a legacy compatibility reference, not the selected execution
decision. `fallbacks` lists all other candidates with complete metadata. Missing
model information still needs a candidate-scoped `model_information_override` with a
nonblank reason. Runtime, disabled/exclude, price-cheaper-than-lead, tool, context,
output and known quota safeguards remain hard boundaries. Historical rejection rates
never exclude candidates or change explicit operator state.

Unavailable or malformed Jev evidence uses an explicit metadata/cost fallback with
`ranking_basis: "heuristic"`, `heuristic_basis: "metadata_safe_cost"`, and null fit
scores/distributions. No semantic scores are invented. Legacy evaluator mass/floor
arguments are accepted but ignored; `TYPESAFE_FIT_MASS` and `TYPESAFE_FIT_FLOOR` no
longer affect routing. `direct` means no usable runtime/metadata-safe recommendation,
never low suitability or independence.

## Jev and fallback

Semantic Jev fit judgment is enabled by default. `--jev` remains a compatibility alias; `--no-task-jev` disables task-fit judgment for diagnostics. Catalog identity matching still uses Jev when available. If task-fit Jev is unavailable, a deterministic heuristic fallback is used and marked in the reasons. The heuristic is not the normal default when Jev is available.

Set `TYPESAFE_API_KEY` to enable Jev. Optionally set `TYPESAFE_API_KEY_OP_REF` once to an exact nonsecret `op://<vault>/<item>/<field>` reference. If the env key is unset, fdel automatically reads that reference with `op read`; after an env-key HTTP 401, it retries once with the referenced key. The key stays in process memory, and the per-process cache is keyed by reference. `TYPESAFE_OP_TIMEOUT` defaults to 10 seconds and accepts values up to 60 seconds. This optional path requires the 1Password CLI to be installed and already authenticated; env-only use requires no `op` installation. See the official [`op read` reference](https://www.1password.dev/cli/reference/commands/read) and [secret reference syntax](https://www.1password.dev/cli/secret-reference-syntax). Catalog prices are cached for 24 hours; `FAST_DELEGATE_CATALOG=path` selects a local catalog file. No paid API calls are required for the deterministic fallback.

## Discovery and feedback

`discover --harness claude|codex --spawnable ...` accepts caller-supplied candidate selectors. Claude can also discover agent files and configured built-ins; Codex candidates come from the supplied selector list or configured catalog metadata. Discovery describes candidates and does not execute them.

After the caller dispatches and verifies a task, record its outcome:

```sh
python3 .../fdel.py record --candidate <candidate-id> --family <family> \
  --outcome accepted|rejected --route <route-id> --tokens <observed-tokens>
```

Recorded outcomes inform later eligibility and fit calibration. `stats` summarizes that evidence. fast-delegate does not supervise tasks, retry dispatches, decompose tasks, or claim a batch assignment API. Its current boundary is one predefined task and one ranked recommendation list; a caller may separately coordinate multiple tasks if it has an actual harness workflow for joint assignment.

## Evidence

Historical experiments and their reports are preserved in [EVIDENCE.md](skills/fast-delegate/EVIDENCE.md). They describe earlier experiments and do not define current routing outputs.

## Development

```sh
make acceptance  # Python 3.10+, stdlib only; runs the test suite
```

## License

[MIT License](LICENSE)

fast-delegate may retrieve model economics and capability metadata at runtime from the BerriAI/litellm repository's `model_prices_and_context_window.json`. Persisted snapshots retain the upstream repository, commit, path, and content hash as provenance. This project does not vendor LiteLLM source code or its pricing database.

For full skill reference, command-line options, Jev integration, local harness overrides, and quota configuration details, see [skills/fast-delegate/SKILL.md](skills/fast-delegate/SKILL.md).

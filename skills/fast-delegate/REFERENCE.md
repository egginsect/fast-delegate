# Fast delegate reference

The detailed reference from the public branch, preserved after moving the operational
core to [SKILL.md](SKILL.md). Read this file on demand for flags, environment variables,
pricing and catalog rules, quota, configuration and evidence. Command examples below use
the repo-relative path `skills/fast-delegate/scripts/fdel.py`; for an installed skill
substitute `<this skill dir>/scripts/fdel.py`.

## Proxy routing and served-model feedback

Proxy-routed (`proxy-*`) workers require an active proxy. `route` drops these candidates
before ranking when the proxy is inactive, including explicit model-information
overrides. `discover --json` reports `proxy_active` and each candidate's
`proxy_routed` state.

Detection requires `ANTHROPIC_BASE_URL` together with
`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`. `FAST_DELEGATE_PROXY=1|0` overrides
detection; set it to `1` only after verifying that the session can actually reach
the proxy route. This setting does not start a proxy or change the worker's
dispatch configuration.

After checking the result, supply `record --served-model MODEL` or
`--transcript SUBAGENT.jsonl` to validate the model that actually ran. The transcript
reader uses distinct assistant `message.model` values. A different model family is
refused without writing an outcome. A same-family version difference is accepted
with `served_model_version_drift: true` and a warning that the declared catalog
price may not describe the served version. Matching ignores proxy/provider prefixes,
case, date suffixes and context-window tags. `--force` bypasses this validation;
a forced cross-family outcome is marked `served_model_mismatch: true`. Without
either served-model option, recording retains its previous behavior and does not
verify which model ran.

With `--route`, new route records retain each candidate's model identity so dynamic
spawnable lists and later agent-file edits do not invalidate feedback. Jev-confirmed
catalog identity takes precedence over a descriptive model name; configured builtin
aliases resolve through the harness. Older route records without identity snapshots
fall back to current declarations and cannot verify an undeclared model without an
explicit `--force` override.

## Overview

One script, `scripts/fdel.py`, discovers candidate model metadata, attaches price, context
and tool-support facts from the LiteLLM catalog, asks TypeSafe Jev to judge task fit by
default, and returns one ranked model recommendation list with evidence. The caller owns harness-specific task handoff, dispatch, and verification. The router
contains recommendation logic; the removed coupling was generated handoff files and spawn
descriptors, not an external worker executor. It exposes no batch optimizer. No routing
rule names a model, vendor, or tier; model facts live in agent files,
[harness.json](harness.json), the LiteLLM catalog, and the outcome ledger. Evidence
is in [EVIDENCE.md](EVIDENCE.md).


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

### 1. Describe the task

Describe one predefined task as JSON. Keep `deliverable` and `acceptance` literal.

```json
{
  "deliverable": "Implement merge_intervals and total_covered so tests pass",
  "family": "mechanical",
  "access": "write",
  "owned_paths": ["/repo/pkg/intervals.py"],
  "acceptance": ["cd /repo && pytest -q intervals"]
}
```

When the lead has a candidate-specific reason to use incomplete or unverified model
information, add the structured exception explicitly:

```json
{
  "deliverable": "Implement the parser change",
  "acceptance": ["parser tests pass"],
  "model_information_override": {
    "candidate_id": "agent-a",
    "reason": "Lead has task-specific compatibility evidence"
  }
}
```

Optional fields: `family`, `need_tools`, `est_input_tokens`, `est_output_tokens`,
`est_cached_input_share`, `exclude`, `difficulty`, `context`, `context_files`, and
`model_information_override`. Context files are read with size caps as evidence for Jev;
they are not turned into handoff artifacts. `model_information_override` names one
candidate and a nonblank reason for using incomplete model metadata.

`--tokens` for `record` is supplied by the caller from its own usage data after
verification; fast-delegate cannot observe execution.

### 2. Discover and route

```sh
python3 skills/fast-delegate/scripts/fdel.py discover [--refresh] [--json]
python3 skills/fast-delegate/scripts/fdel.py route --task task.json
python3 skills/fast-delegate/scripts/fdel.py route --task task.json --jev
python3 skills/fast-delegate/scripts/fdel.py route --task task.json --jev --brief
python3 skills/fast-delegate/scripts/fdel.py route --task task.json --lead opus
python3 skills/fast-delegate/scripts/fdel.py route --task task.json \
  --model-information-override-candidate agent-a \
  --model-information-override-reason "Lead has task-specific compatibility evidence"
```

`discover` prints a table (or `--json`) of candidates with `catalog_match` (`jev` or
`none` when Jev model matching is available; `unverified-exact`/`unverified-normalized`/
`none` when credentials are absent or matching is unavailable — see "Catalog matching" below),
resolved `catalog_id`, prices per million tokens, context, and
`state` (`enabled`, `probation`, or `disabled`; see "Data, not rules").

`route` returns one JSON recommendation and evidence. A delegated result includes a
`candidate` with stable native/caller-recognizable `id` and `model_selector`, underlying
`model_id`, `catalog_id`, estimated cost (`cost_usd` or `shadow_cost`), and fit/ranking
evidence. It also includes `route_id` for later caller feedback. A direct result has no
candidate. `--brief` returns the same routing information in a compact shape. Neither
form creates handoff files, prompts, messages, or executable tool arguments.

When the caller actually delegates, it must explicitly select the recommended model; it must not silently inherit or omit it. `id` and `model_selector` are routing identifiers, while `model_id` is the underlying identity. Custom-agent selectors may need translation using the active harness configuration and are not guaranteed to be verbatim tool arguments. The router does not
specify harness handoff parameters. Its default is one recommendation for one predefined
task. If the caller explicitly supplies multiple tasks that need joint assignment, that
coordination belongs to the caller; no joint optimizer or batch API exists here.

**`--lead ID`** (or env `FAST_DELEGATE_LEAD`, or `harness.json` `default_lead`, required)
specifies the lead model and enforces that the lead only ever delegates to a candidate
strictly cheaper than itself (see step 3 below). `ID` is a candidate id (e.g. `opus`)
or a raw catalog id. If no lead is configured, `route` exits with an error.

The shipped built-in `fable` is another caller-facing candidate selector. Record the actual selected candidate id.

The catalog is cached at `$XDG_CACHE_HOME/fast-delegate/litellm.json` and downloaded
only when the cache is missing, older than 24 hours, or `--refresh` is given; a failed
download uses any cached copy and adds a warning. `FAST_DELEGATE_CATALOG=path` uses a
local catalog file. Model identity matching uses Jev by default when credentials are configured
and reuses verified catalog-version cache entries without credentials. `route` enables semantic task fit judging by default. `--jev` remains a compatibility alias.
`--no-task-jev` is a diagnostic opt-out recorded in reasons; it does not disable
independent catalog matching, which may still use the network. `discover --jev` remains a compatibility alias; matching is
automatic either way.

**Optional 1Password support.** `TYPESAFE_API_KEY` remains the default and takes
priority. To enable automatic fallback, configure `TYPESAFE_API_KEY_OP_REF` once
with the exact nonsecret `op://<vault>/<item>/<field>` reference for the API key.
When the env key is absent, fdel runs `op read -- <ref>` automatically; if an env
key receives HTTP 401, it reads the configured reference and retries that request
once. The resolved credential stays in process memory and is cached per reference
for that process. `TYPESAFE_OP_TIMEOUT` sets the finite positive `op` timeout
(default 10 seconds, maximum 60 seconds). No reference means the existing env-only behavior and no
1Password dependency. `op` must be installed and already authenticated; setup and
sign-in remain operator actions. See the official [`op read` command reference](https://www.1password.dev/cli/reference/commands/read)
and [secret reference syntax](https://www.1password.dev/cli/secret-reference-syntax).
Never put secret values in arguments, files, logs, or handoffs, and do not enable
shell tracing around secret handling.

**Catalog provenance and staleness.** Each download resolves the commit at the tip of
`BerriAI/litellm`'s `main` branch (GitHub's commits API), fetches the catalog pinned to that
commit, and only promotes it over the cached copy once it passes a size bound (30 MB) and a
JSON-shape check (an object with a plausible number of priced entries) -- a malformed or
oversized download is rejected and never overwrites the last-known-good snapshot on disk,
with a warning naming why. The download itself is read in bounded chunks: it aborts as soon as
more than the 30 MB size bound has actually been read (never buffers an oversized body into
memory first) or a 30-second wall-clock deadline elapses, so a slow-drip response can't hold the
connection open past that budget even if each individual read completes quickly. Promotion
itself is atomic: the catalog and its meta are written to temp files first and only
`os.replace()`d into place once *both* writes succeed, so a failure partway (e.g. disk full while
writing meta) can never leave a cache file whose meta doesn't match it -- the last-known-good
pair stays on disk (and is what's returned) either way. `litellm.meta.json` then holds `{repo,
commit, path, sha256, fetched_at}`; if the commits API is unavailable (e.g. rate-limited), the
fetch falls back to
the unpinned `main` raw file and records its `etag` (and always its `sha256`) instead of a
`commit`. `route`'s `catalog` field reports this provenance plus `age_h` and `stale`; past a
hard-stale threshold (14 days, separate from the 24-hour re-download TTL) `route` also sets
top-level `stale_prices: true` and adds a warning -- a catalog that keeps failing to download
never silently keeps serving very old prices as if nothing were wrong.

### 3. How the script decides

1. **Cheaper-than-lead filter** (only when a lead is configured): resolve the lead's price
   through the same catalog matching as any candidate, blend it at the task's
   `est_input_tokens:est_output_tokens` ratio, and keep only candidates whose blended
   price is strictly below the lead's; unknown-price candidates are dropped (cannot prove
   cheaper). The lead's own model is therefore never a candidate. Nothing left -> `direct`
   with reason `no candidate cheaper than lead <id>`. An unpriced or unknown lead id is a
   hard error, not a guess.
3. Filters: `state` `enabled` or `probation` (not `disabled`), not in `exclude`, tool
   support not `false` when `need_tools`, context unknown or at least 1.2 x
   `est_input_tokens`, catalog `max_output_tokens` unknown or at least `est_output_tokens`
   (skip reason: `skip <id>: max_output_tokens=N < est_output_tokens=M`, machine-readable),
   and known quota permits use. Historical acceptance is advisory. Nothing left -> `direct`. Catalog matching (exact/normalized and fuzzy,
   below) only ever considers a key whose catalog `mode` is `chat` or absent -- an
   embedding, image, audio, or rerank key can never price a worker; a name whose best
   match is such a key is left unpriced, not mispriced. A candidate whose matched entry's
   `deprecation_date` has passed adds a warning (not a filter).
4. `cost_usd` (or `shadow_cost` -- see "Billing mode" below) learns from the ledger: with a
   token record for this (family, candidate) pair use its median tokens, else the family's
   median across candidates, else the task's own `est_input_tokens + est_output_tokens`;
   split by the task's input:output ratio and price. When the matched catalog entry defines
   `cache_read_input_token_cost`, `est_cached_input_share` of the input tokens (default 0)
   prices at that rate instead of the plain input rate; when the input token count exceeds
   a LiteLLM pricing tier (200k or 272k tokens) and the entry defines an
   `*_above_200k_tokens`/`*_above_272k_tokens` rate, that rate is used for the (non-cached)
   input and output cost instead of the plain rate. `price_basis` in the output lists which
   catalog fields were actually used (e.g. `["cache_read_input_token_cost",
   "input_cost_per_token_above_200k_tokens", "output_cost_per_token"]`); a builtin's
   `billing.price_override`, when set, always wins as a flat rate instead (`price_basis:
   ["price_override"]`), ignoring tiered/cached fields. `token_basis` in the output says
   which token-estimate basis was used. Candidates sort cheapest first, unknown price last,
   ties by ledger acceptance. `price_band` is the rank among known-price candidates:
   `cheapest`, `cheap`, `mid`, `pricey`, `most expensive`; `only` for a single known price;
   `unknown` without a price. Bands describe price only; routing may use a separate
   model-information safeguard, so a `cheapest` band does not guarantee the pick.
5. By default, with `TYPESAFE_API_KEY`: one request judges every enabled or probation
   candidate that survived the filters, up to `JEV_MAX_CANDIDATES` (the documented
   question-per-request cap, minus the 4 fixed task questions) — not a fixed 8. The
   state includes the task's `context`/`context_files`, and per candidate: price band,
   context band, tool support, exact price, context window, max output tokens, catalog
   `supports_*` flags, its own description, `enabled`/`probation` state, and ledger
   evidence (per-family rate plus its 5 most recent records). A probation candidate
   shows real ledger evidence ("no record", "accepted N of M", etc.) and carries a
   separate `probation` field. Difficulty and verifiability count only at confidence >=
   `TYPESAFE_CONFIDENCE` (default 0.4). Fit answers are not confidence-gated (see
   EVIDENCE.md). Every Jev call also reports `jev_usage` and `jev_cost_usd` (from
   `TYPESAFE_INPUT_PRICE_PER_M`, default 0.042). HTTP errors, timeouts, and malformed
   answers fall back and say so in `reasons`. No automatic retries are made.
   Fit distributions must have usable mass summing to one (tolerance 0.01); missing
   or malformed fit distributions make the task request unavailable. A valid low-fit
   response remains advisory scored evidence, including with a named metadata override.
   Task recommendations are never cached; catalog identity caching is independent.
6. Jev supplies advisory scores and raw normalized distributions. The lead owns semantic
   suitability. Complete Jev-confirmed identity, usable price, context and tool metadata
   form the safe pool; incomplete information requires a scoped explicit override.
   This exception never bypasses runtime, disabled/exclude, output, quota or price filters.
7. The compatibility pick and outage fallback use cheapest metadata-safe cost.
   Every recommendation requires lead acceptance, even with strong scores and history.

**Billing mode.** A worker's optional `billing` object marks how its LiteLLM price relates to
what it actually costs: `{mode: "metered"|"subscription"|"local", price_override: $/M or null,
quota_weight: number or null}` (unset or an unrecognized `mode` defaults to `metered`, so an
unconfigured worker prices exactly as before; any extra keys, e.g. a future `pool` for
quota-aware selection, are preserved as-is, never dropped). It's reachable two ways: a
`harness.json` builtin can carry it inline, and `harness.json`'s **top-level `billing` field**
(a map keyed by candidate id: `{billing: {"<candidate id>": {mode, ...}}}`) can billing-configure
*any* candidate -- agent-file and codex `--spawnable` candidates have no per-entry harness.json
record to carry an inline `billing` field, so the top-level map is the only way to billing-
configure them; for a builtin, an id-keyed entry there wins over its own inline `billing`. In a
local override this map merges per candidate id (override wins for any id it names, unmatched
shipped ids survive), unlike other object-valued keys, which are replaced wholesale.

For `subscription` or `local` workers the LiteLLM-derived number is a shadow pricing proxy
only -- ranking still uses it, and the cheaper-than-lead rule still applies to it (`reasons` says
so), but `route`/`--brief` report it as `shadow_cost`, never `cost_usd`, plus `billing_mode`, so
output (and README wording) never claims a real dollar saving for a plan or self-hosted worker.
`price_override`, when set, is a flat $/M rate applied to both input and output that replaces the
catalog price outright (ignoring tiered/cached-input fields) -- for a local model absent from
LiteLLM, or to correct a wrong catalog entry; it survives Jev catalog re-matching too (`--jev` can
overwrite a candidate's `price` from a fresh catalog lookup, but an explicit override is always
re-applied afterward). `quota_weight` is carried through data only; the current pricing logic
doesn't use it for ranking (a future quota-aware selection feature will build on it).

**A `$0` catalog price is never "free."** Independent of billing mode, a matched catalog entry
whose `input_cost_per_token` or `output_cost_per_token` is a literal 0 is priced as *unknown*, not
as free -- LiteLLM's own `$0` entries are typically placeholders or free-tier footnotes, not a
promise that routing there costs nothing. `warnings` names the candidate (`"<id>: catalog price is
0; not treated as free -- set billing.price_override"`); only an explicit `billing.price_override`
may set a genuinely `$0` (or any) rate, and it always wins over this safeguard. The same rule
applies to `--lead`: a lead whose catalog price is `$0` is treated as unpriced, so the existing
"lead has unknown price" hard error fires exactly as it would for any other unpriced lead.

### 4. Caller-owned delegation and verification

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

Discovery metadata and `--spawnable` selector allowlists help the router restrict
recommendations to models the caller says are available. Those facts are routing data,
not a tool schema. After selecting a candidate using the policy above, the caller builds
its own handoff and explicitly selects that model in its active harness API,
then verifies the result against the task's acceptance checks. `id` and `model_selector`
are routing identifiers; `model_id` is the underlying priced model identity. A custom-agent
selector may need translation through the harness's own agent configuration, so none of
these identifiers is promised to be a verbatim tool argument. The caller must not omit or
silently inherit the selected model. This package creates no worker
executor and does not prescribe harness call parameters, retries, supervision, or task
decomposition. Historical reports remain immutable evidence of past experiments.

### 5. Record the outcome

```sh
python3 skills/fast-delegate/scripts/fdel.py record --candidate haiku --family mechanical --outcome accepted --note "8 passed" --tokens 4200 --route <route_id>
python3 skills/fast-delegate/scripts/fdel.py stats
python3 skills/fast-delegate/scripts/fdel.py stats --calibration
```

`--tokens N` (optional) records total observed tokens for later cost estimates; obtain them from the active harness usage notification after verification.
`--route <route_id>` links caller feedback to the recommendation. If the caller selected
a different candidate, `--override "<why>"` records both ids and the corresponding fit
masses for calibration. Record the acceptance evidence the caller verified. The ledger
lives in `$FAST_DELEGATE_STATE/outcomes.jsonl`; `stats --calibration` summarizes linked
outcomes by fit mass and family.

### 6. Quota-aware selection

fast-delegate can weigh model strength against how much of a subscription plan (or API budget) is
left, so a Claude Max user at "5h 92% · 7d 40%" gets a different pick than one at "5h 3%". Quota
affects `billing.mode: "subscription"` candidates by removing known blocked workers and
re-weighting cost. Semantic scores and outcome history remain advisory. `metered` (with no budget configured) and `local` candidates
are entirely unaffected.

**Pool assignment is data, not rules.** A candidate's quota pool comes from `billing.pool` in the
same top-level `billing` map described under "Billing mode" above: an explicit `pool` always
wins; otherwise a codex `--spawnable` candidate defaults to pool `"codex"`, a `harness.json`
builtin defaults to the active `--harness` name, and an agent-file candidate has no default —
quota simply doesn't apply to it (today's behavior) unless `billing.pool` names one explicitly.

**Getting a quota signal in — never a network call, never polls a usage API:**

- **Claude Code subscription**: point `statusLine` at the shipped helper, chaining your existing
  statusLine command after `--`. **Use an absolute path** — the statusLine command runs from an
  arbitrary cwd, not your repo checkout, so a relative path (e.g.
  `skills/fast-delegate/scripts/quota_statusline.py`) fails.
  - Installed via `install.sh claude` (or `$CLAUDE_CONFIG_DIR/skills` when that env var is set):
    ```json
    {
      "statusLine": {
        "type": "command",
        "command": "python3 ~/.claude/skills/fast-delegate/scripts/quota_statusline.py -- <your-existing-statusline-command>"
      }
    }
    ```
  - Installed as a Claude Code plugin instead, the script lives under your plugins directory
    rather than `~/.claude/skills` — find the exact path with:
    ```
    find ~/.claude/plugins -name quota_statusline.py
    ```
    then use that path in place of `~/.claude/skills/fast-delegate/...` above.

  Claude Code (v2.1.80+) passes `rate_limits.five_hour`/`rate_limits.seven_day` to the statusLine
  command on stdin; the helper writes that (plus an `observed_at` timestamp) to
  `$FAST_DELEGATE_STATE/quota/claude.json` with `basis: "claude-statusline"`, then re-runs your
  existing command unchanged and prints its output — `route` only ever reads the file back. With
  no chained command after `--` it still has to print something a status line can show (a blank
  line reads as broken, not "no command configured yet"), so it prints a minimal one-line quota
  summary instead, after writing the snapshot.
- **Codex subscription**: nothing to configure. `route` tails the newest
  `~/.codex/sessions/**/rollout-*.jsonl` (honoring `CODEX_HOME`), read-only, scanning back past
  `rate_limits: null` entries (short sessions often log only null) to the newest real one, giving
  a snapshot with `basis: "codex-rollout"`. All-null files (or none at all) leave the pool unknown.
- **Anything else** (Cursor, a personal plan, …): `route --quota pool=5h:NN,7d:NN` (repeatable) or
  the `FAST_DELEGATE_QUOTA` env var (same syntax, `;`-separated for multiple pools), or write
  `$FAST_DELEGATE_STATE/quota/<pool>.json` yourself in the shape `quota_statusline.py` writes. A
  CLI `--quota` entry wins over an env entry for the same pool.
- **API key (metered)**: an optional `budget_usd_per_day` in harness.json's `quota` object is read
  and carried through but **not yet enforced** in this slice (see "Deferred" below) — a metered
  candidate with no budget configured is entirely unaffected by quota.
- **Local model**: `billing.mode: "local"` → no quota term, ever.

**Freshness and staleness.** Each snapshot carries `observed_at`. Once a window's `resets_at` has
passed it is treated as reset (0% used) regardless of what the snapshot said. A snapshot whose
`observed_at` is older than `quota.stale_five_hour_s` (default 1800s / 30 min) or
`quota.stale_seven_day_s` (default 21600s / 6 h) is still used, but flagged `stale` in the
`quota`/`--brief` output plus a warning — never silently trusted as fresh.

**Behavior near exhaustion** (thresholds in harness.json/override's top-level `quota` object;
defaults `warn_at: 80`, `reserve: 10`, both percentages):

1. **Warn** (used >= `warn_at` on either window): pick normally, with pressure applied (below);
   add `quota: {pool, five_hour, seven_day, resets_at, basis, status, pressure, stale}` and a
   warning to `route`/`--brief` output.
2. **Reserve** (remaining < `reserve` on either window): `filter_candidates` hard-skips every
   subscription candidate drawing on that pool, with a machine-readable reason (`skip <id>: quota
   <pool> reserve; resets at <time>`). Routing then proceeds among remaining runtime-eligible candidates —
   another pool, or a metered/local candidate. If nothing does, `route` returns `direct` with
   reason `quota: <pool> reserve; resets at <time>` plus `wait_until` (the epoch in the JSON
   result and `--brief`; the reason text spells out the local time).
3. **Exhausted** (`rate_limit_reached_type` set, or used >= 100%): the same hard skip as Reserve.
4. **`--ignore-quota`** bypasses every quota filter and the pressure re-weight below; always
   recorded (`ignore_quota: true`) in the result and in `routes.jsonl`, never a silent override.

**Cost re-weighting.** For a runtime-eligible subscription candidate, ranking uses `effective_cost =
shadow_cost * pressure(pool)` in place of the raw `shadow_cost` — the reported `shadow_cost`/
`cost_usd` value itself never changes, only which candidate ranks cheapest. `pressure` is
near-`eps` (essentially free) when usage is comfortably at or behind the window's pace — a fresh
plan prices its runtime-eligible worker as marginal-cost-zero — and rises steeply (toward 1,
the worker's real relative price) as usage runs ahead of pace: `ahead = used% − elapsed% of the
window`; 60% used with 20 minutes left in a 5h window is fine (low `ahead`), 60% used one hour
into that same window is not. A pool with no snapshot at all is simply unweighted (today's
behavior), with a `quota unknown for <pool>` warning on any subscription candidate naming it.

**Escalation.** When `route` picks a candidate, it also checks that at least one `fallbacks`
candidate is not itself quota-blocked. If every fallback is blocked by reserve/exhaustion,
`escalation_blocked: "quota"` and `review_required: true` are set — there is no genuine escalation
path left in this pool, worth knowing before the caller chooses whether and how to proceed.

**Deferred (follow-up, not in this slice):** local-token-accounting as a first-class cost basis —
`routes.jsonl` now records `est_tokens` and `cost_usd`/`shadow_cost` per delegated decision, but
the Reserve check above does not yet subtract a task's own projected quota share from the pool's
remaining%, only compares remaining% against `reserve` directly; `budget_usd_per_day` spend-cap
enforcement (read, not enforced — no proxy, no spend accounting to compare it against);
`max_concurrent` for local models (read, not enforced — no active-worker registry exists to count
against it); learned per-model quota weights; API-key rate-limit headers via a proxy; per-model
weekly sub-limits; cross-harness pool balancing.

## Jev is evidence, not a verdict

The lead accepts or rejects candidates using scores, distributions, task context and
observed outcomes. Record verified outcomes with `--route <route_id>` and add
`--override "<why>"` when choosing a different candidate. New records retain fit scores
and distributions. Legacy mass records remain readable by `stats --calibration`;
their historical buckets are descriptive, never eligibility thresholds.

## Data, not rules

- Agents: every `*.md` in `$CLAUDE_CONFIG_DIR/agents` (default `~/.claude/agents`) and
  `<cwd>/.claude/agents`. Frontmatter `name` is the candidate id; frontmatter
  `description` becomes the candidate's Jev-facing description. The underlying model
  id is the frontmatter `model` with any `<a>-<b>-<c>--` proxy prefix removed, unless that
  value is listed in `model_id_placeholders`; then the first model name in the description is
  used. If no model is available, the agent name is used. The agent id remains the caller selector.
- `harness.json`: `builtin` (id, `model_selector`, `model_name` — a descriptive model
  name resolved by catalog matching like any other candidate, not a pinned catalog id —
  optional description, optional inline `billing` — see "Billing mode" above), top-level
  `billing` (an id-keyed map, not a builtin field — the general way to billing-configure any
  candidate, including agent files and codex `--spawnable` ids, and wins over a builtin's own
  inline `billing`; see "Billing mode" above), `disabled` (never
  routed, never judged — keep this to ids that
  must never run, e.g. an id that would route the lead's own model back through a proxy),
  `probation` (ids that are discovered, priced, filtered, ranked and judged like any
  other, but flagged with a warning if picked; probation is explicit operator state and history does not automatically change it), `catalog_provider_preference`, `specialization_tags`, `generic_name_tokens`,
  `default_lead` (the lead's own candidate/catalog id, used by the cheaper-than-lead
  rule when `--lead` and `FAST_DELEGATE_LEAD` are unset), `harnesses` (section N: data
  keyed by `claude`/`codex`; an optional `models` list under each is the default
  `--spawnable` allowlist, and Codex's optional `reasoning_effort` maps a model name to
  a default `reasoning_effort`). The shipped `harness.json` is a
  neutral default: no `default_lead`, no `disabled`/`probation` entries, and no
  maintainer-specific ids — add your own workers and personal settings in a local
  override instead of editing the shipped file.
- **Local override** (open-source hygiene): if `$FAST_DELEGATE_STATE/harness.json` exists,
  or `FAST_DELEGATE_HARNESS=path` is set, it is merged over the shipped file — scalar
  keys are replaced, `builtin`/`disabled`/`probation` merge by id (override wins,
  unmatched shipped entries survive), and top-level `billing` merges per candidate id the same
  way (override wins for any id it names, unmatched shipped ids survive). A malformed or
  non-object override file is a hard
  error naming the file, not a silent no-op (a silently-ignored override would fail open
  and re-enable ids it meant to disable). Example override, adding a personal worker and
  disabling one you never want routed to:
  ```json
  {
    "default_lead": "opus",
    "builtin": [{"id": "my-custom-agent", "model_selector": "my-custom-agent",
                 "model_name": "some-provider/some-model"}],
    "disabled": [{"id": "my-custom-agent-self", "why": "must never route to the lead itself"}]
  }
  ```
  `discover`'s warnings name the override file in use, if any.
- Catalog matching, and section L ("no exact model match as a decision"): every match --
  exact, normalized, or fuzzy — only ever considers a catalog key whose `mode` is `chat` or
  absent (section 3); a key with a different `mode` (`embedding`, `image_generation`,
  `audio_transcription`, `rerank`, …) is never eligible, so it can never price a worker even
  as an exact string match on a model id. The code-level
  exact/normalized match (exact key; then keys equal after removing the provider prefix;
  then equal after removing a trailing date — a bare key beats a preferred provider
  prefix, which beats other prefixes, undated beats dated) is never accepted outright
  when Jev model matching is available. Instead it seeds the fuzzy shortlist below, ranked
  first, and still needs a Jev noul to confirm it; `catalog_match` is `jev` for anything Jev confirms
  (including a plain exact match) and `none` for anything it doesn't — no candidate's
  `catalog_match` is ever `exact`/`normalized` when Jev matching is available. Without
  `TYPESAFE_API_KEY`, or on an unavailable request, a code match is retained only as
  `unverified-exact`/`unverified-normalized`, with a warning to configure credentials.
  Cached verified matches are reused even when credentials are absent. Explicit Jev rejections
  remain unmatched; prices are never invented.
- Jev catalog matching (automatic when credentials are configured; for open models whose names
  and versions vary,
  and for verifying any code match under section L): code shortlists up to 25 priced
  keys by shared name tokens (`2p5` reads as `2.5`), dropping keys that state a different
  parameter size or carry a different set of `specialization_tags` (harness.json). Jev
  answers one noul per surviving key, "same model: family, size and version?"; keys at or
  above `TYPESAFE_MATCH_THRESHOLD` (default 0.7) count. The price comes from a priced
  key first (a same-model key with no price never wins purely for being a bare key or a
  seed), then a preferred provider key, then a bare key, then the median-priced key.
  `match_entries` and `match_score` are reported; `route`'s `reasons` carry one summary
  line ("catalog: N matched by jev"), with the per-candidate detail in the full result's
  `catalog_matches`. Results (including no-match) are cached per catalog version — a
  content hash of the catalog's keys and prices, not key count plus a truncated mtime,
  so two different catalogs never collide on the same cached id — in
  `$FAST_DELEGATE_STATE/catalog_matches.json` (or `FAST_DELEGATE_CACHE=path` to point it
  elsewhere, e.g. to isolate a read-only repro from real state) — a repeated
  `discover`/`route` for the same names sends no further request; any cached id no
  longer present in the current catalog is dropped and rematched rather than trusted. A
  partial Jev outage (one batch of models fails while another succeeds) only leaves the
  failed batch's candidates as `unverified-*`; a candidate whose own batch Jev judged and
  rejected keeps `catalog_match: "none"` and stays unpriced. Jev errors leave the price
  unknown and warn. A shortlisted key must share at least one
  non-generic word token with the model id (`generic_name_tokens` in harness.json —
  instruct/chat/quantization tags and the bare letters split off size tokens like "7b" or
  "a35b"); the LiteLLM documentation key `sample_spec` is never treated as a real model; a
  mixture-of-experts size token like "8x7b" is distinct from both "8x22b" and "7b", not
  ignored.
- Calibration ledger: every delegated `route` decision is appended to
  `$FAST_DELEGATE_STATE/routes.jsonl` (route_id, time, family, picked id,
  each judged candidate's distribution and expected fit, difficulty, independence). `record
  --route <route_id>` looks it up to link the outcome and copy the picked candidate's
  score/distribution; `stats --calibration` reads legacy outcomes with a `mass` field to report acceptance by
  bucket and family, plus overrides recorded separately.
- Environment: `TYPESAFE_API_KEY`, optional `TYPESAFE_API_KEY_OP_REF` and
  `TYPESAFE_OP_TIMEOUT` (default 10 seconds), `TYPESAFE_MODEL` (default `jev-latest`),
  `TYPESAFE_TIMEOUT` (default 4 s), `TYPESAFE_CONFIDENCE` (default 0.4),
  `TYPESAFE_INPUT_PRICE_PER_M` (default 0.042, for
  `jev_cost_usd`), `TYPESAFE_MATCH_THRESHOLD` (default 0.7), `FAST_DELEGATE_LEAD` (lead id for the cheaper-than-lead rule).

### Provider quota adapters

Quota readers share `QuotaAdapter.read(pool, now)` and accept injected readers for
verification. `ClaudeQuotaAdapter` reuses the statusline snapshot;
`CodexQuotaAdapter` tails rollouts, preserving the event timestamp (missing timestamps
mean unknown freshness). These are cached observations, not live probes.
`CursorQuotaAdapter` accepts a normalized operator snapshot at
`$FAST_DELEGATE_STATE/quota/cursor.json` or the existing manual override. No Cursor
admin credential is configured here. The official [Cursor Admin API](https://prod.cursor.com/docs/account/teams/admin-api)
uses admin-only Basic authentication for `POST /teams/spend`: total included plus
on-demand spending is not an included-quota percentage, and the on-demand monthly
spend limit is a different pool. This adapter does not divide these values or map a
monthly billing cycle to five-hour/seven-day windows. Live Cursor quota is unsupported.

Runtime and upstream are independent billing fields. Configure a proxy-runtime worker
with `{"runtime":"proxy","provider":"nvidia"}` in the candidate's billing
entry; its pool becomes `proxy:nvidia`. With no explicit upstream, its quota is
unknown. `pool` remains the explicit override; native Codex workers retain the
legacy Codex default for unqualified native model IDs. Slash-qualified spawnables
without deployment metadata remain unknown (`provider/model` selectors). Never infer
upstream from a model family or assume the router shares Codex quota. Account
selection is never inferred from a model prefix.

`ProxyQuotaAdapter` reads `$FAST_DELEGATE_STATE/proxy/provider-account-quota-cache.json`
(version 1, `rows`, `provider\u0000accountId` keys) and, only for an
explicit `proxy:openai` pool, `codex-quota-cache.json` (`quotas`). The disk schema
defines version 1 and `provider\u0000accountId -> quota`; fixtures follow that contract.
`updatedAt` is observation time in milliseconds; reset timestamps accept source
seconds or milliseconds. Only a single account row for that provider is usable:
multiple accounts are unknown until a verified active-account contract exists.
No account keys, credentials, labels, history, or credit balances leave the reader.
Monthly/custom windows and non-five-hour short windows are unsupported, left
unknown rather than relabeled. Empty, malformed, unsupported or ambiguous sources
are unavailable, never zero. Route `quota_sources` reports source, availability,
observation time, freshness, a reason when unavailable, and `live: false` independently
of legacy `quota` policy state. Manual overrides remain explicitly marked manual.

### Recommendation evidence and bounded experiment

Task state includes family, optional descriptive `complexity`, and explicit input/output
estimates (null when absent). Candidate state distinguishes the caller model selector, model configuration,
matched catalog entry and identity-match probability. No canonical model
registry is integrated, so `canonical_model_id` is explicitly unknown. `reasoning_effort`
comes from model configuration or operator billing metadata; provider/runtime are explicit
billing fields, never inferred from a model family. Missing configuration is unknown.

Capability fit is separate from cost and availability. Matched-host catalog prices,
price basis, billing mode/source, estimated metered or shadow cost, and catalog provenance
are cost evidence; they are never a performance rubric. Matching model identity does not
verify the actual deployment or bill. `actual_billed_cost` is null and
`actual_worker_billing_verified` is false. Quota source/freshness and policy state are
separate observations. Code applies quota exclusion/pressure once. The legacy pick is cheapest
metadata-safe in the verified metadata tier; authoritative recommendations rank expected fit. Jev cannot bypass hard gates.

Local family profiles report accepted/total with observational uncertainty, including
zero observations. Integrated benchmarks are unknown. An optional operator
`billing.strength_profile` requires nonblank `source`, `date` (YYYY-MM-DD),
`model_version` (exact worker selector), `configuration` (exact reasoning-effort label),
and `evidence`. Missing/mismatched configuration or version, future dates, and evidence
older than 90 days produce unknown. Complete profiles remain operator-documented and
not independently verified; they do not grant eligibility or establish actual outcomes.

The [comparison harness](examples/routing_experiment.py) uses the current source evaluator,
no worker execution, an explicit local identity cache, and synthetic quota fixtures.
Run without `--live` to inspect requests without credentials. With explicit authorization,
`--live` makes at most six routing requests, with no retries or matching requests. It
finds the unique OP item title `TYPESAFE_API_KEY`, uses discovered vault metadata, and
captures its single-token `notesPlain` value in process memory. IDs, account labels,
credentials, and raw OP errors are never saved. API responses retain only model, typed
answers, and usage. Do not run it as an ordinary acceptance test.

```sh
python3 skills/fast-delegate/examples/routing_experiment.py \
  --cache /tmp/fast-delegate-cli-model-details-jev.json \
  --output /tmp/routing-comparison.json
```

A 2026-10-02 comparison recorded the
same three task/candidate scenarios for baseline and enriched requests, complete typed
answers, source provenance, hard-gate reasons, and composed selections. The baseline
request builder is pinned to main revision `e19d52b`; both use the current evaluator.
Quota and deployment fixtures are illustrative, not actual account measurements;
identity matches come from the prior local report without rematching. This measures
routing differences only and cannot establish better task success or model strength.

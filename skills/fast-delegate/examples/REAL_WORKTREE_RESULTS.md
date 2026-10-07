# Real repository task acceptance, 2026-10-02

Three predeclared repo tasks ran in isolated caller-managed worktrees from `311a7cc`.
The caller dispatched native harness workers and an independent read-only judge.
This repo supplies routing; it contains no worker executor or supervision implementation.
The later routing-only cleanup is excluded from this cohort.

| Task | Initial worker | First outcome | Final outcome |
| --- | --- | --- | --- |
| Billing provenance | Luna | Accepted | Accepted |
| Learned token floor | Sonnet | 429; incomplete | Accepted after native recovery and repair |
| Lossless payload adapter | Sonnet | 429; incomplete | Accepted after native recovery and repair |

First-pass acceptance was **1/3 (33.3%)**. After the first recovery it was still
1/3: both new Luna submissions were rejected despite their suites passing.
Recovery submissions alone were **0/2**. Final task acceptance was **3/3 (100%)**;
across all implementation attempts it was **3/7 (42.9%)**. These denominators exclude
judges and routing calls. This small chosen sample with retries is not general
accuracy, a benchmark, or evidence that Jev alone achieved a 100% success rate.

The original routes were best-effort below the normal fit gate. Recovery Jev returned
`direct`; the user's explicit AGENTS fallback authorized the caller to choose the
cheapest eligible native worker. Luna recovery was therefore a lead fallback,
not a fit-qualified Jev selection. The Opus judge accepted billing but subsequently
hit 429; the independent native Sol judge completed the remaining evaluations.

The independent judge found defects omitted by the workers' passing tests:

- Input-only 200000 and output-only 6000 estimates erased the existing ratio.
  Repairs now split 202000 into 200000+2000 and 26000 into 20000+6000, preserving
  larger learned totals and disclosing the applied floor separately from provenance.
- An ordinary source `{"unknown":{"$ref":"literal"}}` was interpreted as an
  encoding reference, and reordered repeated objects lost promised key order.
  Escapes and order-sensitive deduplication now preserve textual roundtrips.

The judge reran literal counterexamples, 21 token fixtures, payload adversarial
cases and six saved-request roundtrips/replays. Final task gates passed 262, 278,
and 260 tests respectively on Python3.14.8/uv0.12.22. The caller's combined gate
passed **286 tests**. Historical experiment JSON stayed byte-identical.
The payload example reduced six requests from **721450 to493421 UTF-8 bytes**;
this is not measured API-token savings or evidence of unchanged live judgments.

Routing plus identity matching used **9 calls**, 47,369 input and 3,271
output tokens. At the repository input default $0.042/M, the input-only proxy is
**$0.001989**. Worker/judge observed usage mapped to matched catalog base/cache
rates gives **$4.2437**;
preparation adds **$0.2806**. Combined with
Jev input, that is **$4.5263**, or **$1.5088 per final accepted task**.
These are catalog-equivalent proxies, **not actual charges**: invoices, routed
provider tariffs, per-request tier pricing and cache semantics are unverified.
Lead inference and the later routing-only cleanup are excluded. Actual billed cost
remains unknown. Failed attempts and both judges are included in the observed proxy.

Total: 11 routing and 55 execution requests, 0.75 M output, 11.48 M effective input, 0.65 M cached at upstream billing rate. Route() cost: $0.0009 at recorded catalog prices (OpenAI reasoning and embedding, Anthropic judging/routing, independent pricing). Anonymous sanitized exact results and usage (real-worktree-results-20261002.json) can be regenerated with the experiment script and preserve
native runtime counters, configured model IDs, rate fields, independent verdicts
and attempt history. No credentials, raw provider conversations or account state
were copied into this report.

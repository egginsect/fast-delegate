# Completion-first Jev observations

Observed 2026-10-03T00:55:24Z using source `f5afa81755ec8f13dd787380777d31cef960305d`.

Three live task-fit calls, zero identity calls, zero worker executions. Each request had eight questions (four task questions and four candidate-fit questions). Identity evidence reused the existing catalog cache; Astra supplied a price reference only. Fable and Astra were never executed. The original baseline remained unchanged.

Full task completion and every acceptance criterion are primary; cost and availability are secondary. These score-ranked recommendations are references for main-model judgment, not automatic execution choices. All twelve records require lead acceptance; no score threshold or success-probability/cost optimizer was introduced.

Current quota is unknown: provider state and user logs were not read. The baseline had cached quota observations; those do not establish current availability. Missing benchmark/history evidence remains unknown. Cached input share was unspecified and remains unknown, not a verified hit.

Prompt and isolated outcome history both changed, and quota context differs. Ranking differences cannot establish a causal prompt effect. No new full-task execution or success rates were measured. Earlier pilot acceptance remains Sol 2/3 versus Luna 1/3, a small bounded sample rather than universal capability proof.

## Ranking observations

| Task | Baseline order | New order (raw score) |
| --- | --- | --- |
| compact | Sonnet (2.66) → Opus (2.65) → Luna (2.27) → Sol (2.23) | Sol (3.30) → Opus (2.14) → Sonnet (2.10) → Luna (1.04) |
| pricing | Sonnet (2.16) → Luna (2.13) → Opus (1.98) → Sol (1.87) | Sol (2.99) → Luna (2.79) → Sonnet (2.04) → Opus (1.94) |
| quota | Luna (2.32) → Sonnet (2.21) → Opus (2.07) → Sol (2.06) | Sonnet (2.17) → Opus (2.16) → Sol (1.46) → Luna (1.21) |

Differences in small raw scores do not prove capability differences. The legacy compatibility candidate remains Luna for each contract; it is not a selected worker.

## Candidate prices and family evidence

| Task | Candidate | Raw score | Estimated catalog-proxy USD | Family accepted/total |
| --- | --- | --- | --- | --- |
| compact | Sol | 3.30 | 0.38368667 | 1/1 |
| compact | Opus | 2.14 | 1.36781667 | 0/0 |
| compact | Sonnet | 2.10 | 0.82069000 | 0/0 |
| compact | Luna | 1.04 | 0.03552833 | 0/1 |
| pricing | Sol | 2.99 | 0.95699703 | 1/1 |
| pricing | Luna | 2.79 | 0.04845593 | 1/1 |
| pricing | Sonnet | 2.04 | 1.44458674 | 0/0 |
| pricing | Opus | 1.94 | 2.40764457 | 0/0 |
| quota | Sonnet | 2.17 | 1.52562936 | 0/0 |
| quota | Opus | 2.16 | 2.54271560 | 0/0 |
| quota | Sol | 1.46 | 1.01022455 | 0/1 |
| quota | Luna | 1.21 | 0.05119740 | 0/1 |

These candidate estimates use task token assumptions and learned ledger totals; they are catalog proxies, not worker bills. Family evidence is observational and subject to selection bias; 0/0 means unobserved. Google candidates were judged but never executed.

## Request evidence and usage

The raw JSON observation (20261003T005524Z_COMPLETION_FIRST_OBSERVATIONS.json) can be regenerated with the experiment script. It contains the exact controlled task contracts, production task state (including difficulty and unknown cache estimate), routing objective, fit question text, candidate input excerpts, distributions, prices, family history, prior score order and per-call usage. It is not a full wire-request capture; no private working directory, user context, session/route identifiers, provider state or credentials are included.

| Task | Input tokens | Output tokens |
| --- | --- | --- |
| compact | 6079 | 129 |
| pricing | 6076 | 129 |
| quota | 6085 | 129 |

Reported totals: 18,240 input and 387 output tokens. At the supplied $0.042/M input rate, the input-only proxy is $0.00076608. Output pricing, cache hits and actual invoice charges are unknown; this is not an all-in bill.

Validation: bounded call counts, four candidates per contract, descending raw scores, normalized fit distributions, required lead review, JSON numeric validity and an allowlisted privacy scan passed. The production source acceptance gate previously passed 267 tests; this observation adds data only.

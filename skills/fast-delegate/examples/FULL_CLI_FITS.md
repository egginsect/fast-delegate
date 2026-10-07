# Full-inventory fit tables for lead review

Lead verdicts attached unchanged in the exact report; no worker-authored verdicts. No actual task-success evidence. Costs are catalog proxies.

## mechanical rename

Practical: `gpt-6-luna`; independent=0.9; review=False.

| Raw top five model | Fit mass | Expected fit | Proxy cost | Eligible before fit | Exclusion |
| --- | --- | --- | --- | --- | --- |
| gpt-6-luna | 0.99 | 3.4 | 0.0009 | True | — |
| gpt-6.1-sol | 0.99 | 3.4 | 0.018 | True | — |
| gpt-5.6-luna | 0.99 | 3.38 | 0.002 | False | not in lead-attested native spawnable set |
| opencode-go/qwen3.8-max | 0.99 | 3.16 | 0.014 | False | not in lead-attested native spawnable set |
| opencode-go/deepseek-v4-flash | 0.99 | 3.15 | 0.0024 | False | not in lead-attested native spawnable set |

Eligible native model fits:

| Model | Fit mass | Proxy cost | Qualified |
| --- | --- | --- | --- |
| gpt-6-luna | 0.99 | 0.0009 | True |
| gpt-6.1-sol | 0.99 | 0.018 | True |
| google-antigravity/claude-sonnet-4-6 | 0.98 | 0.027 | True |
| google-antigravity/claude-opus-4-6-thinking | 0.97 | 0.045 | True |

Decision reasons: jev: complete Jev-confirmed model metadata; cheapest qualifying candidate in this tier; fit gate P(fit level >= 2) >= 0.5 (required fit 2.0) -> gpt-6-luna

## concurrency race

Practical: `gpt-6.1-sol`; independent=0.67; review=True.

| Raw top five model | Fit mass | Expected fit | Proxy cost | Eligible before fit | Exclusion |
| --- | --- | --- | --- | --- | --- |
| gpt-6.1-sol | 0.5 | 2.52 | 0.054 | True | — |
| gpt-6-astra | 0.41 | 2.46 | 0.27 | False | drop gpt-6-astra: blended price $18.0000/M not cheaper than lead's $18.0000/M |
| gpt-5.5 | 0.41 | 2.41 | 0.15 | False | not in lead-attested native spawnable set |
| google-antigravity/claude-opus-4-6-thinking | 0.36 | 2.33 | 0.135 | True | — |
| google-antigravity/claude-sonnet-4-6 | 0.34 | 2.32 | 0.081 | True | — |

Eligible native model fits:

| Model | Fit mass | Proxy cost | Qualified |
| --- | --- | --- | --- |
| gpt-6-luna | 0.22 | 0.0027 | False |
| gpt-6.1-sol | 0.5 | 0.054 | True |
| google-antigravity/claude-sonnet-4-6 | 0.34 | 0.081 | False |
| google-antigravity/claude-opus-4-6-thinking | 0.36 | 0.135 | False |

Decision reasons: jev: complete Jev-confirmed model metadata; cheapest qualifying candidate in this tier; fit gate P(fit level >= 3) >= 0.5 (required fit 3.0) -> gpt-6.1-sol; fallbacks include a candidate below the gate (mass >= 0.25 floor only); review before using it; margin: thin-pick -- picked gpt-6.1-sol (mass 0.5) is within 0.1 of the gate (0.5); no concurrency evidence for gpt-6.1-sol; Jev fit is unproven here

## security authorization audit

Practical: `gpt-6.1-sol`; independent=0.69; review=True.

| Raw top five model | Fit mass | Expected fit | Proxy cost | Eligible before fit | Exclusion |
| --- | --- | --- | --- | --- | --- |
| gpt-6-astra | 0.68 | 2.82 | 0.38 | False | drop gpt-6-astra: blended price $17.2727/M not cheaper than lead's $17.2727/M |
| google-antigravity/claude-sonnet-4-6 | 0.61 | 2.63 | 0.114 | True | — |
| gpt-6.1-sol | 0.58 | 2.57 | 0.076 | True | — |
| opencode-go/deepseek-v4-flash | 0.56 | 2.58 | 0.0102 | False | not in lead-attested native spawnable set |
| gpt-5.6-luna | 0.56 | 2.57 | 0.0084 | False | not in lead-attested native spawnable set |

Eligible native model fits:

| Model | Fit mass | Proxy cost | Qualified |
| --- | --- | --- | --- |
| gpt-6-luna | 0.32 | 0.0038 | False |
| gpt-6.1-sol | 0.58 | 0.076 | True |
| google-antigravity/claude-sonnet-4-6 | 0.61 | 0.114 | True |
| google-antigravity/claude-opus-4-6-thinking | 0.52 | 0.19 | True |

Decision reasons: jev: complete Jev-confirmed model metadata; cheapest qualifying candidate in this tier; fit gate P(fit level >= 3) >= 0.5 (required fit 3.0) -> gpt-6.1-sol; margin: thin-pick -- picked gpt-6.1-sol (mass 0.58) is within 0.1 of the gate (0.5); no security evidence for gpt-6.1-sol; Jev fit is unproven here

## large context investigation

Practical: `google-antigravity/claude-sonnet-4-6`; independent=0.48; review=True.

| Raw top five model | Fit mass | Expected fit | Proxy cost | Eligible before fit | Exclusion |
| --- | --- | --- | --- | --- | --- |
| gpt-6-astra | 0.6 | 2.76 | 0.73175728 | False | drop gpt-6-astra: blended price $11.1650/M not cheaper than lead's $11.1650/M |
| google-antigravity/claude-sonnet-4-6 | 0.5 | 2.48 | 0.21952718 | True | — |
| opencode-go/deepseek-v4-flash | 0.49 | 2.5 | 0.02138004 | False | not in lead-attested native spawnable set |
| gpt-6.1-sol | 0.49 | 2.42 | 0.14635146 | True | — |
| google-antigravity/claude-opus-4-6-thinking | 0.48 | 2.43 | 0.36587864 | True | — |

Eligible native model fits:

| Model | Fit mass | Proxy cost | Qualified |
| --- | --- | --- | --- |
| gpt-6-luna | 0.47 | 0.00731757 | False |
| gpt-6.1-sol | 0.49 | 0.14635146 | False |
| google-antigravity/claude-sonnet-4-6 | 0.5 | 0.21952718 | True |
| google-antigravity/claude-opus-4-6-thinking | 0.48 | 0.36587864 | False |

Decision reasons: jev: complete Jev-confirmed model metadata; cheapest qualifying candidate in this tier; fit gate P(fit level >= 3) >= 0.5 (required fit 3.0) -> google-antigravity/claude-sonnet-4-6; fallbacks include a candidate below the gate (mass >= 0.25 floor only); review before using it; margin: thin-pick -- picked google-antigravity/claude-sonnet-4-6 (mass 0.5) is within 0.1 of the gate (0.5); margin: near-miss-cheaper -- next-cheaper gpt-6.1-sol (mass 0.49) is within 0.1 below the gate (0.5); picked google-antigravity/claude-sonnet-4-6 (mass 0.5); no investigation evidence for google-antigravity/claude-sonnet-4-6; Jev fit is unproven here

## precise extraction

Practical: `gpt-6-luna`; independent=0.82; review=False.

| Raw top five model | Fit mass | Expected fit | Proxy cost | Eligible before fit | Exclusion |
| --- | --- | --- | --- | --- | --- |
| nvidia/moonshotai-kimi-k3 | 0.93 | 2.64 | 0.024 | False | not in lead-attested native spawnable set |
| gpt-5.6-terra | 0.92 | 2.6 | 0.018 | False | not in lead-attested native spawnable set |
| opencode-go/deepseek-v4-flash | 0.92 | 2.56 | 0.0021 | False | not in lead-attested native spawnable set |
| gpt-5.6-luna | 0.9 | 2.48 | 0.0018 | False | not in lead-attested native spawnable set |
| opencode-go/minimax-m3 | 0.9 | 2.33 | 0.0021 | False | not in lead-attested native spawnable set |

Eligible native model fits:

| Model | Fit mass | Proxy cost | Qualified |
| --- | --- | --- | --- |
| gpt-6-luna | 0.85 | 0.0008 | True |
| gpt-6.1-sol | 0.88 | 0.016 | True |
| google-antigravity/claude-sonnet-4-6 | 0.87 | 0.024 | True |
| google-antigravity/claude-opus-4-6-thinking | 0.82 | 0.04 | True |

Decision reasons: jev: complete Jev-confirmed model metadata; cheapest qualifying candidate in this tier; fit gate P(fit level >= 2) >= 0.5 (required fit 2.0) -> gpt-6-luna

## ambiguous architectural design

Practical: `direct`; independent=0.38; review=False.

| Raw top five model | Fit mass | Expected fit | Proxy cost | Eligible before fit | Exclusion |
| --- | --- | --- | --- | --- | --- |
| gpt-6-astra | 0.39 | 3.11 | 0.09 | False | drop gpt-6-astra: blended price $18.0000/M not cheaper than lead's $18.0000/M |
| google-antigravity/claude-opus-4-6-thinking | 0.23 | 2.91 | 0.045 | True | — |
| google-antigravity/claude-sonnet-4-6 | 0.14 | 2.76 | 0.027 | True | — |
| opencode-go/kimi-k3 | 0.14 | 2.61 | 0.027 | False | not in lead-attested native spawnable set |
| opencode-go/grok-4.6 | 0.09 | 2.54 | 0.014 | False | not in lead-attested native spawnable set |

Eligible native model fits:

| Model | Fit mass | Proxy cost | Qualified |
| --- | --- | --- | --- |
| gpt-6-luna | 0.02 | 0.0009 | False |
| gpt-6.1-sol | 0.07 | 0.018 | False |
| google-antigravity/claude-sonnet-4-6 | 0.14 | 0.027 | False |
| google-antigravity/claude-opus-4-6-thinking | 0.23 | 0.045 | False |

Decision reasons: jev judged 4 candidates; none has P(fit level >= 4) >= 0.5 (required fit 3.5)


## Discovered estimate mismatch

Large-context candidates use learned investigation-family total estimate 65,540
(`token_basis=family`, flat catalog price basis `input_per_m`, `output_per_m`),
corresponding to approximately 63,631.07 input and 1,908.93 output tokens after
splitting. Mandatory scenario input is 200,000; the context gate still requires
240,000. Sonnet proxy cost 0.21952718 is therefore below the explicit-estimate
flat catalog proxy 0.69 (200k × 3/M + 6k × 15/M). Both are estimates, not invoices.
Exact per-model bases and mismatches are saved in `fit_table`. No live rerun or
production change was made.

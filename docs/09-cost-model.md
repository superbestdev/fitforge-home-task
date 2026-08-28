# 9. Cost and scale at 10,000 sessions/month

All figures below are extrapolated from **measured** usage in this build
(`llm_calls`), not estimated from intuition.

## Measured per-session usage

Across real sessions in this repo:

| Node | Calls/session | Avg prompt tok | Avg completion tok | Avg latency |
|---|---|---|---|---|
| `diagnose` | 4.4 | 1,383 | 50 | 17.5 s |
| `router` | 3.5 | 258 | 22 | 1.7 s |
| `summarise` | 2.0 | 55 | 42 | 4.2 s |
| `handoff_summary` | 0.5 | 162 | 131 | 12.8 s |
| **Total** | **10.4** | **~7,126** | **~448** | — |

Reproduce with:

```sql
SELECT node, count(*), round(avg(prompt_tokens)), round(avg(completion_tokens))
  FROM llm_calls GROUP BY node;
```

**10.4 calls per session is the number worth dwelling on.** A naive
implementation of this brief — classify with an LLM, identify with an LLM,
decide coverage with an LLM, compose every reply with an LLM — lands around
25–30. The difference is entirely the deterministic paths: identification,
warranty, pricing, ordering, safety, escalation, error-code lookup and roughly
half of all routing never call a model.

**That is a ~60% cost reduction that came from the architecture, not from
optimisation.**

## At 10,000 sessions/month

| Quantity | Monthly |
|---|---|
| LLM calls | ~104,000 |
| Prompt tokens | ~71.3 M |
| Completion tokens | ~4.5 M |
| Embedding calls (queries) | ~44,000 |
| Peak concurrency (8h day, Poisson) | ~6–10 sessions |

The prompt:completion ratio is **16:1**. This is a retrieval-heavy workload, and
it means anything that reduces prompt size is worth roughly sixteen times more
than anything that shortens replies.

---

## Option A — self-hosted (the no-paid-services answer)

| Item | Spec | Monthly |
|---|---|---|
| Inference GPU | 1× L4 24 GB, vLLM, 7–14B instruct | $260–430 |
| App + Postgres | 8 vCPU / 32 GB | $120 |
| Storage | 200 GB (manuals, OCR cache, DB) | $20 |
| Backups + egress | | $30 |
| Langfuse (self-hosted) | small instance | $40 |
| **Total** | | **≈ $470–640/mo** |
| **Per session** | | **≈ $0.05–0.06** |

One L4 with continuous batching handles this volume comfortably: ~71M prompt
tokens/month is ~27 tokens/second sustained, which is a rounding error for
batched GPU inference. **The GPU is sized by concurrency and latency, not by
throughput.**

Sensitivity: this is dominated by a fixed GPU cost, so per-session cost falls
almost linearly with volume. At 50,000 sessions/month the same hardware likely
still suffices → ~$0.012/session.

## Option B — commercial API (for comparison)

Excluded by the brief, but it is the relevant baseline. At representative
small-model API pricing (~$0.10/M input, ~$0.40/M output):

| | Monthly |
|---|---|
| Input 71.3 M tok | ~$7 |
| Output 4.5 M tok | ~$2 |
| App + DB infra | ~$140 |
| **Total** | **≈ $150/mo** |
| **Per session** | **≈ $0.015** |

**A hosted small model is cheaper than self-hosting at this volume**, and it
would be roughly 10× faster. Self-hosting wins on data residency, on having no
per-token bill to model as you scale, and on the constraint given here. It is
worth being straight about that: at 10,000 sessions/month the honest reason to
self-host is policy, not economics. The crossover is somewhere around
50,000–100,000 sessions/month, or immediately if customer data cannot leave the
network.

## Context: what it replaces

At a loaded cost of ~$7 per human-handled contact and 10,000 contacts:

| Scenario | Monthly cost |
|---|---|
| All human | $70,000 |
| 65% contained, self-hosted | $24,500 + $600 ≈ **$25,100** |
| 65% contained, hosted API | $24,500 + $150 ≈ **$24,650** |

Inference is **2.4% of the remaining cost** in the self-hosted case. Which is the
real point: *containment rate dominates infrastructure cost by two orders of
magnitude.* Five points of containment is worth $3,500/month. The entire
inference bill is worth $600. Effort spent on retrieval quality and escalation
tuning pays back far more than effort spent optimising tokens.

---

## Where the cost actually is

| Driver | Share of prompt tokens | Notes |
|---|---|---|
| Retrieved manual context | ~60% | 6 chunks, capped at 4,000 chars |
| Diagnostic history digest | ~20% | Lossy by design |
| System prompt + safety preamble | ~15% | Fixed per call |
| Customer message | ~5% | — |

`diagnose` is 42% of calls but **85% of prompt tokens**. It is the only node
worth optimising.

## Optimisations, in order of value

1. **Prompt caching** (~40% saving). The system prompt and safety preamble are
   identical across every call for a category. vLLM's automatic prefix caching
   gets this with no code change.
2. **Tighter retrieval** (~20%). Six chunks is generous; RRF usually puts the
   right one first. Dropping to 3–4 with a confidence floor would cost little.
3. **Already implemented — tiered routing.** The 1.5B router handles 34% of
   calls at 19% of the token cost of the 3B.
4. **Already implemented — deterministic paths.** The single largest saving in
   the system; roughly half of turns never reach a model.
5. **Semantic caching of common symptoms.** "Belt slipping" on a popular
   treadmill is asked hundreds of times a month with a near-identical first
   step. A cache keyed on (model_id, symptom cluster, step 1) is cheap.
6. **Summarise long threads.** Only matters past ~8 steps, which is the budget.

## What I would *not* optimise

- **Completion length.** It is 6% of tokens. Shortening replies degrades the
  product to save nothing.
- **Embeddings.** ~44,000 query embeddings/month is negligible either way.
- **The audit log.** Storage is $20/month. Do not economise on the record of
  what you charged people.

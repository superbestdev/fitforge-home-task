# 6. Observability and quality

> *How would you monitor this in production? What tells you the agent is
> underperforming, and what do you do about it?*

## The problem with the obvious metric

Containment rate — sessions finished without a human — is the number the
business cares about. It is also nearly useless on its own, because it moves for
opposite reasons:

- Containment **falls** because retrieval regressed. Bad.
- Containment **falls** because we tightened the safety threshold. Good.
- Containment **rises** because the agent stopped escalating when it should. Very bad.

So containment is the headline, and **the escalation-reason breakdown is the
diagnostic**. Every escalation records *which* trigger fired, and each one points
somewhere different:

| Reason rising | What is actually wrong | Response |
|---|---|---|
| `no_coverage` | Manual corpus has gaps | Ingest the missing manuals |
| `low_retrieval_confidence` | Retrieval, chunking or embeddings regressed | Fix the index; check `coverage_registry` quality |
| `no_progress` | Diagnostic prompts degraded, or manuals lack depth for that symptom | Prompt work; audit the manual section |
| `step_budget_exhausted` | Genuinely hard faults, or budget too tight | Review transcripts before touching the budget |
| `tool_failures` | Something downstream is broken | Page someone |
| `no_part_match` | Symptom-tag coverage is thin | Extend `symptom_tags` in the catalog |
| `safety` | Real field incidents | Route to engineering/QA, not support |
| `restricted_part` | Working as designed | Watch the ratio, not the count |
| `customer_frustration` | Tone or latency problem | Read the transcripts |
| `high_value_order` | Working as designed | Check the threshold is still right |
| `customer_request` | Trust problem, or the agent is unhelpful early | Compare against turn count at request |

---

## The signal set

### Tier 1 — page someone

| Metric | Threshold |
|---|---|
| `tool_failures` escalation rate | > 2% of sessions |
| LLM call failure rate | > 5% over 15 min |
| p95 turn latency | > 60 s |
| Order failure rate | > 1% |
| `order_hash_mismatch` in `audit_log` | **Any occurrence** |
| Payment declines from our side (not the card) | > 0.5% |

`order_hash_mismatch` deserves its zero threshold: it means the agent's state
and the customer's understanding diverged, and it is the signal that something
is deeply wrong with session handling.

### Tier 2 — daily review

- Containment rate, overall and by product category
- First-contact resolution per **issue thread** (not per session — a session
  with two issues can be half a success, and only thread-level tracking shows it)
- Median steps to resolution
- Escalation reason distribution, week over week
- Model-identification accuracy: rate of re-identification within a session, and
  part returns marked "wrong part"
- Retrieval: mean best-chunk score, and the share of turns below threshold
- Coverage: models `backed` / `degraded` / `unbacked`
- Cost per session (tokens × rate, or GPU-hours ÷ sessions)

### Tier 3 — weekly

- Citation coverage: share of technical claims traceable to a retrieved chunk
- Handoff quality: do human agents re-ask what the bot already asked? Sampled
  manually; it is the truest measure of whether the packet works
- Per-model failure clustering — one SKU with a bad manual shows up here first
- CSAT split by contained vs. escalated

---

## What is instrumented in this build

**`llm_calls`** — every model call: node, model, prompt/completion tokens,
latency, success, error. This is what makes the cost model measured rather than
estimated, and it shows which node burns the budget.

```sql
SELECT node, count(*), round(avg(prompt_tokens)) AS prompt,
       round(avg(latency_ms)) AS ms
  FROM llm_calls GROUP BY node ORDER BY 2 DESC;
```

**`audit_log`** — append-only record of every consequential action: warranty
verdicts with their inputs, quotes, charges, orders, escalations, safety stops,
blocked injections. This is the table you hand an auditor.

**`trace`** — every turn records its path through the graph:

```
precheck:safety=ok -> route:fast=new-machine->FF-BB-VELODROME-450
  -> open_issue:2:Screen Blank Issue -> diagnose:step1->diagnosing
```

Stored on each message and shown in the demo and console. When behaviour is
wrong, this says *where* in about five seconds — it is the highest
value-per-line instrumentation in the repo.

**`/api/metrics`** — containment, issue outcomes, escalation reasons, per-node
LLM usage, coverage. Rendered live in the agent console header.

**Langfuse** (optional `obs` profile) — full trace trees, prompt/response
inspection, latency breakdown, per-session cost. Self-hosted; the system runs
without it and still records everything to `llm_calls`.

---

## Evaluation

Three layers, because they catch different things.

### 1. Deterministic tests (`make test`, 40 tests)

The invariants, with no model in the loop: warranty boundaries, retrieval
scoping, injection detection, safety phrases, commerce guards, idempotency, hash
verification. These must never fail, and they run in ~1.5 s.

### 2. Golden session replay (`make eval`)

Scripted multi-turn sessions with assertions on **outcomes rather than wording**:
which thread statuses were reached, was the right model identified, did coverage
resolve correctly, did the expected escalation reason fire. Wording assertions
would break on every prompt tweak and teach you nothing.

### 3. LLM-as-judge, on the local model

For qualities you cannot assert exactly: was the step actually responsive to
what the customer said, was it grounded in the cited chunk, was the tone right.
Run with the same local model — cheap, private, and directionally reliable even
if noisy.

### CI gate

Deterministic tests must pass. Golden containment and model-ID accuracy must not
regress by more than a few points. A prompt change that raises containment while
*lowering* citation coverage is flagged rather than merged — that combination
usually means the agent has started improvising.

---

## What I would add next

1. **Shadow evaluation** — replay live sessions against a candidate prompt or
   model and diff the outcomes before shipping.
2. **Per-SKU quality dashboards** — a bad manual is invisible in aggregate and
   obvious per model.
3. **Human-agent feedback loop** — one click in the console: "the bot was going
   the right way" / "wrong track". The cheapest high-quality labels available,
   and they come from people who already read the transcript.
4. **Automatic coverage alerts** — a new SKU appearing in sessions with no
   manual indexed should open a ticket, not silently escalate every customer.

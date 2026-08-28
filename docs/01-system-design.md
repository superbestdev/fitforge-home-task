# 1. System design

## The shape of the problem

Strip away the surface and FitForge's support problem is four hard requirements
wearing a chat interface:

1. **Identify the exact model** before anything else, because every downstream
   fact — troubleshooting steps, part numbers, warranty terms — is keyed on it.
2. **Run a bounded iterative loop** that interprets what the customer reports and
   decides the next check, without spinning forever.
3. **Take money correctly**, which is a transactional problem, not a language one.
4. **Track several independent problems at once**, and hand all of them to a
   human with nothing lost.

Only the second of those is really a language-model problem. The design follows
from taking that seriously.

## The governing principle

> **The LLM decides what to say and what to ask next. It never decides what is
> true, what is covered, what something costs, or what gets charged.**

Every fact the customer receives originates in Postgres. The model's job is to
choose the next diagnostic step from retrieved documentation and to phrase
things like a person. That boundary is what makes the system safe to run on a
3-billion-parameter model on a CPU — and it would still be the right boundary
with a frontier model behind it, because the failure modes it prevents are
expensive rather than merely embarrassing.

## Component map

```
┌──────────────┐   WS    ┌────────────────────────────────────────┐
│ Chat widget  │◄───────►│ FastAPI gateway (WS, REST, CORS)       │
│ (React/Vite) │         └──────────────┬─────────────────────────┘
└──────────────┘                        │
┌──────────────┐   WS    ┌──────────────▼─────────────────────────┐
│ Agent console│◄───────►│ Session orchestrator (LangGraph)       │
│ (React/Vite) │  Redis  │ durable checkpoints → Postgres         │
└──────────────┘ pub/sub │                                        │
                         │  precheck → route → act → re-check     │
                         └───┬────────────┬──────────────┬────────┘
                             │            │              │
              ┌──────────────▼─┐  ┌───────▼───────┐  ┌───▼──────────────┐
              │ Tool registry  │  │ Policy engine │  │ Knowledge layer  │
              │ (typed, pure)  │  │ DETERMINISTIC │  │ hybrid retrieval │
              │                │  │ warranty      │  │ + symbolic facts │
              │                │  │ safety        │  │ + coverage       │
              │                │  │ escalation    │  │   registry       │
              └───────┬────────┘  └───────┬───────┘  └───┬──────────────┘
                      │                   │              │
       ┌──────────────▼───────────────────▼──────────────▼──────────────┐
       │ Postgres 17 + pgvector + tsvector                              │
       │ catalog · parts · error_codes · warranty_terms · customers ·   │
       │ orders · sessions · issue_threads · doc_chunks · coverage ·    │
       │ quotes · payments · part_orders · handoffs · audit_log ·       │
       │ llm_calls · langgraph checkpoints                              │
       └────────────────────────────────────────────────────────────────┘

   Ollama (LLM + embeddings) · Mock PSP · Langfuse (optional profile)
```

## The state model is the design

A session is **not** a conversation. It is a customer, a set of verified
machines, and a list of independent `IssueThread` objects:

```python
class IssueThread(BaseModel):
    id: str
    seq: int                    # 1, 2, 3... order raised in the session
    title: str
    status: Literal["new", "diagnosing", "awaiting_customer", "awaiting_part",
                    "resolved", "unresolvable", "escalated"]
    model_id: str | None        # threads may concern DIFFERENT machines
    symptom_summary: str
    steps: list[DiagnosticStep] # asked / customer said / what it ruled out
    ruled_out: list[str]
    citations: list[ChunkRef]
    candidate_part: str | None
    quote_id: str | None
    step_budget_used: int
    tool_failures: int
```

Everything awkward about the case study becomes ordinary bookkeeping once state
is shaped this way:

| Requirement | How the thread model handles it |
|---|---|
| Multiple unrelated issues | Separate rows. Independent status, budget, machine. |
| Suspending and resuming | A pointer move. The thread carries its own history. |
| Different machines in one session | `model_id` per thread, not per session. |
| "What have we already ruled out?" | `ruled_out` on the thread, not inferred from a transcript. |
| Handoff with full context | Serialise the threads; nothing to reconstruct. |
| Partial resolution | Thread 1 resolved while thread 2 escalates. Both true at once. |

Exactly one thread is active at a time. The alternative — a concurrent agent per
issue — was rejected: it doubles token cost on hardware that cannot afford it,
lets two agents issue conflicting tool calls against the same customer, and
produces interleaved questions that no customer can follow.

## Turn flow

One customer message runs the graph exactly once.

```
                    ┌───────────┐
   message ────────►│ precheck  │  deterministic, no LLM
                    └─────┬─────┘  safety phrases · human request · identifiers
                          │
              safety stop │ else
             ┌────────────┴────────┐
             ▼                     ▼
        ┌─────────┐          ┌──────────┐
        │ handoff │          │  route   │  fast paths first, small LLM last
        └─────────┘          └────┬─────┘
                                  │
                    ┌─────────────┴──────────────┐
                    ▼                            ▼
            ┌───────────────┐            ┌───────────────┐
            │  THE GATE     │  no model? │  identify     │
            │ needs_ident?  │───────────►│  (ladder)     │
            └───────┬───────┘            └───────┬───────┘
                    │ model known                │ identified + symptom
       ┌────────────┼────────────┬───────────────┘
       ▼            ▼            ▼
  ┌──────────┐ ┌──────────┐ ┌──────────────┐
  │open_issue│ │ diagnose │ │ switch_issue │
  └────┬─────┘ └────┬─────┘ └──────────────┘
       └───────►────┘
                    │ needs a part
                    ▼
            ┌──────────────┐   ┌────────────┐   ┌─────────┐
            │ select_part  │──►│ quote_part │──►│ confirm │──► payment ──► order
            └──────────────┘   └────────────┘   └─────────┘
                    │ any escalation trigger
                    ▼
              ┌───────────┐
              │  handoff  │  builds the packet, queues it, notifies the console
              └───────────┘
```

**The gate** is enforced in one place (`dispatch` in `agent/graph.py`), not
inside each node, so there is exactly one line of code where "we don't know
which machine this is" could be missed.

## Data flow, end to end

**Ingestion** (`services/ingest/`), once per manual:

```
PDF ─► classify pages (text density)
        ├─ born-digital ─────────────────────────► extract (pypdfium2)
        └─ image-only ─► OCR (ocrmypdf/Tesseract) ─► extract
                                                      │
      structure-aware chunk (sections; one chunk per symptom)
                                                      │
      ┌───────────────────────────────────────────────┤
      ▼                                               ▼
  injection screen                             symbolic extraction
  (flagged chunks dropped, never indexed)      error codes → real table
      │                                               │
      ▼                                               ▼
  embed (Ollama) ─► doc_chunks (pgvector + tsvector)  error_codes
      │
      ▼
  coverage_registry: backed | degraded | unbacked
```

**A diagnostic turn**:

```
customer message
  → safety screen (regex, ~0 ms)
  → identifier extraction (regex)
  → route: fast paths (machine mention, commerce phrase, confirmation yes/no)
           else 1.5B classifier (~1.7 s)
  → gate: model verified?
  → error code mentioned? → indexed lookup, no vector search, no LLM
  → else hybrid retrieval scoped to model_id (pgvector + FTS, fused by RRF)
  → confidence check → escalate if we genuinely don't know
  → 3B model proposes ONE next step, schema-constrained (~17 s)
  → deterministic guards: no premature parts, no repeated steps
  → persist step to issue_threads; reply with citations
```

**The commerce path** contains no model output at all:

```
diagnosed fault (words)
  → catalog lookup by symptom tags        → part_number, price   [SQL]
  → safety screen for self-service        → restricted? escalate [Python]
  → warranty engine                        → covered? why?       [SQL + Python]
  → quote row + SHA-256 of the exact figures shown
  → customer says yes                      → quote status = confirmed
  → PSP charge on a token, idempotency key = quote id
  → place_order verifies the hash matches  → refuse on any drift
  → audit_log entry at every step
```

## Component responsibilities

| Component | Owns | Explicitly does not |
|---|---|---|
| `precheck` | Safety, human requests, identifier extraction | Interpret intent |
| `route` | Which node handles this turn | Do the work |
| `identify` | Resolving customer → machine, with honest confidence | Troubleshoot |
| `diagnose` | One diagnostic step from retrieved docs | Decide coverage or price |
| `policy/warranty` | Who pays | Talk to the customer |
| `policy/safety` | What we refuse to walk someone through | Negotiate |
| `policy/escalation` | When to stop | Generate text |
| `tools/*` | Typed, deterministic data access | Reason |
| `commerce_nodes` | Quote → confirm → charge → order | Author prices |
| `handoff` | Assembling everything a human needs | Resolve the issue |

## Where the LLM is actually used

Measured across the sessions in this build: **10.4 model calls per session**,
~7,100 prompt tokens and ~450 completion tokens.

| Node | Model | Calls | Avg prompt | Avg latency | Job |
|---|---|---|---|---|---|
| `diagnose` | 3B | 48 | 1,383 tok | 17.5 s | Choose the next step, phrase it |
| `router` | 1.5B | 38 | 258 tok | 1.7 s | Classify the turn |
| `summarise` | 3B | 22 | 55 tok | 4.2 s | Turn a complaint into a search query |
| `handoff_summary` | 3B | 6 | 162 tok | 12.8 s | Write the handover note |

Everything else — identification, warranty, pricing, ordering, safety,
escalation, error-code lookup, thread switching — runs with no model call at
all. That is not an optimisation applied afterwards; it is the architecture.

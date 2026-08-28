# 0. Overview

The front door. What this project is for, how it is laid out, and how a message
becomes an answer. Everything here is expanded elsewhere in `docs/`.

---

## What it is for

FitForge is a direct-to-consumer home fitness manufacturer with **hundreds of
SKUs**. Every model has its own service manual, its own parts catalog and its own
warranty terms. Customers arrive in chat with a broken machine and expect the
support agent to know which machine that is.

This project is an agentic support system for that job. It must:

1. **Identify the exact model** before any troubleshooting begins — every fact
   downstream of that point is keyed on it.
2. **Troubleshoot iteratively** — one check at a time, interpret the answer,
   continue until the fault is resolved or provably unresolvable.
3. **Handle the parts path** — check warranty, take payment when the part is not
   covered, place the order to the customer's address.
4. **Track several unrelated problems at once**, because customers raise them.
5. **Hand off to a human with nothing lost** when it cannot resolve an issue.

It runs entirely on open-source software, self-hosted, with no paid services and
no external API calls. The reference deployment uses a 3-billion-parameter model
on a CPU with no usable GPU. That constraint is not incidental — it is what
forced the architecture to be honest about which decisions belong to a language
model and which do not.

## The governing principle

> **The LLM decides what to say and what to ask next. It never decides what is
> true, what is covered, what something costs, or what gets charged.**

Warranty verdicts, prices, part numbers, safety refusals and escalation triggers
are ordinary Python and SQL. `services/api/app/policy/` does not import the LLM
client, and that is enforced by the absence of the import rather than by
convention.

---

## Structure

```
web/                      one Vite app, two documents
  src/chat/                 customer widget                    →  /
  src/console/              human-agent console                →  /console

services/api/app/
  main.py                   FastAPI — chat WS, console WS, REST
  agent/
    graph.py                the state machine; every edge is a named condition
    nodes.py                precheck · route · identify · open_issue · diagnose
    commerce_nodes.py       select_part · quote · confirm · pay · escalate
    prompts.py              four prompts and the JSON schemas that bound them
    llm.py                  the only module that talks to Ollama
    state.py                IssueThread · SessionState
  policy/                   warranty · safety · escalation      ← no LLM, ever
  tools/                    identity · knowledge · catalog · commerce · manuals

services/ingest/            extract · ocr · chunk · embed · pipeline
services/mockpsp/           stand-in payment provider

db/migrations/              21 tables — the design is legible from the schema
seed/                       generates the 300-SKU catalog and its manuals
evals/                      golden sessions replayed as a regression gate
tests/                      unit coverage over the deterministic paths
web/e2e/                    Playwright suites driving both UIs in a real browser
```

Two directories carry most of the meaning. `policy/` is where every decision that
costs money or carries liability lives, and it is pure Python. `agent/` is where
the language model lives, and it is fenced.

---

## How it works

There are two timelines, and they are completely separate.

### Timeline A — a PDF becomes rows

Runs once per manual, offline. **No language model is involved at any step.**

```
manual.pdf
  ├─ classify pages       text-density heuristic: born-digital or scanned?
  ├─ OCR if scanned       ocrmypdf + Tesseract, deskew and clean
  ├─ extract text         pypdfium2
  ├─ score quality        OCR'd documents carry a confidence penalty
  ├─ chunk                on section headings, then one chunk per symptom
  ├─ screen for injection flagged chunks are never indexed
  ├─ embed                nomic-embed-text → pgvector
  ├─ extract symbolically error-code tables become error_codes rows
  └─ register coverage    backed · degraded · unbacked
```

The seeded corpus and anything uploaded through the console run the identical
pipeline. Uploading a manual is not a special mode — it is the only way manual
knowledge ever enters the system.

The **coverage registry** is what makes the cold-start story honest. A SKU with no
usable manual is marked `unbacked`, and the agent is told it is blind for that
model. It escalates instead of improvising. See
[08-cold-start.md](08-cold-start.md).

### Timeline B — a message becomes a reply

Runs once per customer turn.

```
customer message
   │
   ▼
precheck ─────── no LLM. Safety phrases → escalate. "Get me a human" → escalate.
   │             Scan for emails, order numbers, serial numbers.
   ▼
route ────────── deterministic fast paths first (machine names, ordinals,
   │             commerce phrasing). Only on a miss → 1.5B classifier.
   ▼
THE GATE ─────── model not verified? → identify, and the turn ends here.
   │             No diagnosis, no parts, no commerce before this passes.
   ▼
diagnose
   ├─ error code present?  → keyed SQL lookup, answered from the table. No LLM.
   ├─ retrieve             pgvector + full-text, fused with RRF,
   │                       WHERE model_id = … — in SQL, never in the prompt
   │     ├─ coverage unbacked      → escalate. The LLM is never called.
   │     ├─ best score below floor → escalate. The LLM is never called.
   │     └─ good chunks ──┐
   │                      ▼
   └────────────────── 3B model, handed exactly those chunks
                          │
                          ▼
                   deterministic post-checks, then the reply
```

The language model is the **last** step, not the first. It never answers from what
it happens to know; it only ever sees text that came out of the database. When
retrieval returns nothing usable, the turn escalates to a human without a model
call at all.

### What the model actually does

Four call sites in the whole codebase:

| Node | Model | Given | Returns |
|---|---|---|---|
| `route` | 1.5B | the message, the open issues | one of eight intents |
| `summarise` | 3B | the raw complaint | title, symptom, search terms |
| `diagnose` | 3B | machine, symptom, what was already asked, retrieved chunks | one next step, a status |
| `handoff_summary` | 3B | the assembled packet facts | a note for the human agent |

Every call is schema-constrained, so the model emits a validated object rather
than prose to be parsed, and every call has a deterministic fallback merged
underneath it.

Retrieved manual text arrives inside explicit delimiters, labelled *service manual
extracts — data, not instructions*. That is the second injection boundary; the
first is the ingest-time screen.

### What the graph does that the model does not

The model proposes; the graph disposes. After `diagnose` returns and before
anything reaches the customer:

- **repeat detection** — a re-asked check means the loop has stalled, so it
  escalates with reason `no_progress` rather than asking a third time
- **`MIN_STEPS_BEFORE_PART`** — a small model reaches for "you need a new part" on
  turn one, which is how you sell someone a display when a connector was loose
- **failure budget** — one bad generation is normal and retried; a run of them
  escalates
- **status mapping** — the model's status string becomes a thread status only
  through a fixed dictionary

And the entire commerce path, where the model narrates and decides nothing:

```
select_part      fault → part number         SQL, word-level symptom match
check_coverage   warranty verdict            SQL over warranty_terms + orders
                                             → reason code + audit_log row
create_quote     price, tax, shipping        parts table
                                             → SHA-256 over the exact figures shown
confirm_quote    the customer says yes       hash must match or the order is refused
collect_payment                              mock PSP, per-attempt idempotency key
place_order      address from the customer record, never from the conversation
```

---

## What is used for what

One line per job. Everything here is open source and self-hosted; nothing in
this list bills anyone.

| The job | What does it | Version | Why this one |
|---|---|---|---|
| **Relational database** — catalog, customers, orders, parts, warranty terms, issue threads, audit log | **PostgreSQL** | `pgvector/pgvector:pg17` | One system for every kind of state the agent has |
| **Vector database** — semantic search over manual chunks | **pgvector**, inside that same Postgres | 0.3.6 | 3,100 chunks is nothing for pgvector. A separate vector store would add a second system to run and back up, to solve a scaling problem that does not exist — and would cost transactional consistency with the catalog |
| **Keyword search** — exact model numbers, part codes, literal phrases | **Postgres `tsvector`** full-text | built in | Vectors are bad at exact tokens. `FF-TT-PACER-550` must match literally |
| **Search fusion** — combining the two | **Reciprocal Rank Fusion**, in SQL | — | No cross-encoder reranker: it would be the slowest thing in the stack on CPU for marginal benefit over a model-scoped candidate set |
| **Agent state machine** — routing, the identification gate, the diagnostic loop | **LangGraph** | 0.2.60 | Explicit graph with named edges. The model stays inside nodes; control flow stays in code |
| **Durable sessions** — surviving a restart mid-conversation | **langgraph-checkpoint-postgres** | 2.0.9 | Checkpoints land in the same Postgres. No extra store |
| **Language model** — diagnostic steps, phrasing | **Qwen2.5 3B Instruct** | `qwen2.5:3b-instruct` | Best JSON adherence per token at a size a CPU can serve |
| **Router model** — intent classification only | **Qwen2.5 1.5B Instruct** | `qwen2.5:1.5b-instruct` | Classification needs no depth and this is roughly 10× faster |
| **Embeddings** — turning chunks and queries into vectors | **nomic-embed-text** | 768-dim | Strong retrieval, served over HTTP, so no torch in the application image |
| **Model serving** | **Ollama** | `ollama/ollama` | Free, local, offline, and OpenAI-compatible |
| **Model client** | **openai** Python SDK | 1.59.6 | Speaks the OpenAI chat-completions shape, which Ollama, vLLM, llama.cpp, TGI and LM Studio all implement. Swapping to a GPU box is three lines of `.env` |
| **Structured output** — forcing valid JSON out of a 3B model | **`response_format: json_schema`** | — | Constrains decoding itself. Ollama ignores its native `format` field on the OpenAI route |
| **PDF text extraction** | **pypdfium2** | 5.13.0 | BSD-3/Apache-2.0. Deliberately **not** PyMuPDF, which is AGPL and a licensing hazard for a commercial support product |
| **OCR** — scanned and photographed manuals | **OCRmyPDF** + **Tesseract** | 17.10.0 | Deskew, clean and rotate for free; writes a real text layer back into the PDF |
| **PDF plumbing** | **pikepdf** | 10.12.0 | Pinned with OCRmyPDF 17 — 16.x calls a method pikepdf 10 removed |
| **Manual generation** — the synthetic corpus and the sample manuals | **ReportLab** | 4.2.5 | Vector line art, tables and panels, born-digital output |
| **Synthetic customers and orders** | **Faker** | 33.1.0 | — |
| **HTTP API and WebSockets** | **FastAPI** + **Uvicorn** | 0.115.6 | WebSockets built in; typed tool contracts fall out of the models |
| **Validation and settings** | **Pydantic v2** + pydantic-settings | ≥2.11 | Every tool boundary is a validated model |
| **Database driver** | **psycopg 3** with a connection pool | 3.2.3 | — |
| **Pub/sub** — pushing escalations to the console | **Redis** | `redis:7-alpine` | — |
| **Payments** | **Mock PSP** (in this repo) | — | The real flow — tokens, idempotency keys, declines — with zero cost and zero PCI surface |
| **Frontend** | **React** + **Vite** | 18.3 / 6.0 | One dev server hosts both UIs as separate documents and proxies the API |
| **Browser tests** | **Playwright** | — | Drives both UIs for real; found two bugs the unit tests could not |
| **Unit tests** | **pytest** | 8.3.4 | 56 tests over the deterministic paths |
| **Tracing** *(optional)* | **Langfuse**, self-hosted | `langfuse:3` | Full LLM tracing without a SaaS bill. Off by default; the app still records every call to `llm_calls` |
| **Runtime** | **Docker Compose** | — | `python:3.12-slim` and `node:22-alpine` |

### What is deliberately absent

| Not used | Why |
|---|---|
| A dedicated vector database (Qdrant, Weaviate, Milvus) | The corpus is far too small to justify a second system |
| A cross-encoder reranker | Worst latency-to-benefit ratio in the stack on CPU |
| PyMuPDF | AGPL-3.0 |
| LangChain retrievers and chains | The retrieval is 40 lines of SQL that needs to be auditable |
| Any hosted API — model, embedding, OCR, payment | The constraint on this build is no paid services |
| Fine-tuning | Retrieval quality and prompt structure carry more weight for the effort |

---

## The state model

A session is **not** a transcript. It is a list of independent issue threads:

```python
class IssueThread(BaseModel):
    id: str
    title: str
    status: Literal["new", "diagnosing", "awaiting_customer", "awaiting_part",
                    "resolved", "unresolvable", "escalated"]
    model_id: str | None          # threads may span different machines
    symptom_summary: str
    steps: list[DiagnosticStep]
    ruled_out: list[str]
    citations: list[ChunkRef]
    candidate_part: str | None
    quote_id: str | None
    step_budget_used: int
```

Exactly one thread is active at a time; the router suspends and resumes them.
This is what makes the multi-issue requirement structural rather than a prompting
trick — a bike fault raised mid-treadmill-diagnosis gets its own model, its own
retrieval scope, its own budget and its own terminal state. See
[07-multi-issue-walkthrough.md](07-multi-issue-walkthrough.md) for a real trace.

---

## Running it

```
docker compose up -d --build      # or:  .\run.ps1 up
```

| | |
|---|---|
| Customer chat | http://localhost:5173 |
| Agent console | http://localhost:5173/console |
| API docs | http://localhost:8000/docs |

Then `models`, `seed`, `ingest`. The console's **Manuals** tab accepts PDF uploads
and runs the same pipeline as the seeded corpus.

---

## Where to go next

| Question | Document |
|---|---|
| How the components fit together | [01-system-design.md](01-system-design.md) |
| Which parts were hard, and why | [02-key-design-considerations.md](02-key-design-considerations.md) |
| Why these libraries | [03-tech-stack.md](03-tech-stack.md) |
| What was rejected and given up | [04-tradeoffs.md](04-tradeoffs.md) |
| Where it fails | [05-risks-assumptions.md](05-risks-assumptions.md) |
| How quality is measured | [06-observability.md](06-observability.md) |
| Two machines, one session | [07-multi-issue-walkthrough.md](07-multi-issue-walkthrough.md) |
| Scanned and missing manuals | [08-cold-start.md](08-cold-start.md) |
| Cost at 10k sessions/month | [09-cost-model.md](09-cost-model.md) |

## The honest limits

- **Customer identity is not verified.** The system identifies the machine; it
  takes the email at face value. This is the most serious gap in the build.
- **The manuals are synthetic.** Generated by reportlab, they share a heading
  convention that flatters the chunker more than real supplier PDFs would. The
  OCR path is exercised for real — 51 manuals are genuine noisy scans.
- **The handoff summary is the weakest generated text.** The packet's facts are
  assembled deterministically and are correct; the prose paragraph on top is a 3B
  model padding, and is the output that would most benefit from a larger one.

# 3. Technology stack

Constraint for this build: **everything open source, self-hosted, no paid
services.** Where that constraint changed a decision, it is called out.

| Layer | Choice | Licence | Why this one |
|---|---|---|---|
| Orchestration | **LangGraph** 0.2 | MIT | Explicit graph with named edges; durable Postgres checkpointing |
| API | **FastAPI** + Pydantic v2 | MIT | Typed tool contracts fall out for free; WebSockets built in |
| Datastore | **Postgres 17 + pgvector + tsvector** | PostgreSQL | One system for relational, vector, full-text, queue and checkpoints |
| LLM serving | **Ollama** | MIT | Free, local, offline; OpenAI-compatible endpoint |
| Reasoning model | **qwen2.5:3b-instruct** | Apache 2.0 | Best JSON adherence per token at CPU-viable size |
| Router model | **qwen2.5:1.5b-instruct** | Apache 2.0 | Classification needs no depth; ~10× faster |
| Embeddings | **nomic-embed-text** | Apache 2.0 | 768-dim, strong retrieval, runs over HTTP — no torch in the app |
| PDF text | **pypdfium2** | BSD-3 / Apache 2.0 | Permissive; see the licensing note below |
| OCR | **ocrmypdf** + **Tesseract** | MPL-2.0 / Apache 2.0 | Deskew, cleanup and page rotation for free |
| Corpus generation | **reportlab** | BSD | Generates the synthetic manuals, including fake scans |
| Payments | **Mock PSP** (in-repo) | — | Real flow, zero cost, zero PCI surface |
| Frontend | **React + Vite** | MIT | Two small SPAs; no build ceremony |
| Tracing | **Langfuse** (self-hosted) | MIT core | Full LLM tracing without a SaaS bill |
| Tests | **pytest** | MIT | — |
| Runtime | **Docker Compose** | Apache 2.0 | One command, reproducible |

---

## The decisions worth defending

### Postgres for everything, including vectors

The corpus is 283 manuals and ~3,100 chunks. That is *nothing* for pgvector —
it is comfortable into the millions. Introducing Qdrant or Weaviate would add a
second system to run, back up and keep consistent, to solve a scaling problem
that does not exist at this size.

The bigger win is **transactional consistency**. The catalog, the retrieval
index, the issue threads and the audit log are in one database, so "the agent
quoted a part that was discontinued last week" is a class of bug that cannot
happen. A dedicated vector store would buy raw ANN throughput and cost that.

*When I would revisit it:* tens of millions of chunks, or a need for multi-tenant
index isolation.

### pypdfium2 rather than PyMuPDF

PyMuPDF has the nicer API and better table extraction. It is also **AGPL-3.0**,
which for a commercial support product means either publishing your source or
buying a commercial licence. pypdfium2 (BSD-3/Apache-2.0) does the same job here.

This is a cheap decision now and an expensive one to reverse after legal review,
which is exactly why it belongs in an architecture document rather than being
discovered later.

### OpenAI-compatible protocol, not a vendor SDK

Every model call goes through the OpenAI chat-completions shape. Ollama, vLLM,
llama.cpp, TGI and LM Studio all implement it. Moving to a GPU box running vLLM
is three lines of `.env`:

```bash
LLM_BASE_URL=http://vllm-host:8000/v1
LLM_MODEL=Qwen/Qwen2.5-14B-Instruct
```

No application code changes. Given that the no-paid-services constraint is the
main reason for a 3B model, keeping the exit cheap was a priority.

### Schema-constrained decoding, not "please reply with JSON"

Structured output uses `response_format: {"type": "json_schema", ...}`, which
constrains decoding itself rather than hoping. Two hard-won details:

- **Ollama's native `format` field is not plumbed through its OpenAI-compatible
  route.** Passing it there is silently ignored and the model returns a bare
  JSON *string* instead of an object. The standard `response_format` works.
- **`required` is not enforced** by the constraint. A model will happily return
  a valid object missing a key the caller depends on. So `complete_json()`
  layers a fallback dict underneath every result, and callers can index the
  response unconditionally.

That second point is a good example of the general posture: assume the model
will do something structurally valid and semantically useless, and make that
survivable.

### qwen2.5 rather than qwen3

qwen3 is a hybrid-reasoning model that emits `<think>` blocks by default. On CPU
those are pure latency for no benefit on tasks this narrow, and they interfere
with constrained JSON. qwen2.5-instruct is non-thinking and adheres to schemas
well at 1.5B and 3B.

### A mock PSP rather than Stripe test mode

Stripe's test mode is free, but it requires an account, network access and API
keys — three things a reviewer cloning this repo should not need. The mock
models the parts of the flow that actually matter architecturally: browser-side
tokenization (so no card data reaches our backend or the model context),
idempotency keys, and deterministic declines for testing the failure path.

Swapping in a real PSP means reimplementing one module against the same
interface.

### Two small models rather than one

Intent classification runs on every turn and needs no depth. Giving it the 1.5B
model instead of the 3B saves ~10 seconds per turn on this hardware. This is the
cheapest latency win in the system, and it generalises: route by task
difficulty, not by convenience.

---

## Measured on this hardware

AMD Ryzen 7 3700X (8c/16t), 64 GB RAM, **no usable GPU** (GT 710, 1 GB):

| Metric | Measured |
|---|---|
| Generation, qwen2.5:3b (Q4) | **16.6 tok/s** |
| Prompt evaluation, qwen2.5:3b | **78 tok/s** |
| Diagnostic turn, end to end | 17–25 s |
| Router turn | ~1.7 s |
| Turn with no model call | ~0.1 s |
| Embedding a chunk | ~40 ms |
| OCR, 4-page scanned manual | ~7 s |
| Full ingest, 300 manuals | ~18 min |

**Prompt evaluation is the binding constraint, not generation.** At 78 tok/s a
2,000-token RAG prompt costs 25 seconds before the first word appears. That single
number drove more of this design than any other measurement: it is why context
is capped at ~4,000 characters, why history is a lossy digest rather than a
transcript, why output length is bounded in the schema, and why roughly half of
all turns are engineered to avoid the model entirely.

## What I would run in production

Nothing above changes except the serving layer: one mid-range GPU (L4 or A10)
running **vLLM** with a 7–14B instruct model, continuous batching enabled. That
moves prompt evaluation from 78 tok/s to several thousand and turns a 20-second
diagnostic turn into roughly two. The application does not know the difference.

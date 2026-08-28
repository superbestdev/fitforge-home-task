# FitForge — Agentic Customer Support

A working reference implementation of an AI support agent for a home-fitness
manufacturer: hundreds of SKUs, a service manual per model, iterative
troubleshooting, warranty-aware parts ordering, multi-issue sessions, and
human handoff with full context.

**Every component is open source and self-hosted. Nothing here calls a paid API.**
The LLM is a 3B model running on CPU via Ollama.

---

## The idea in one line

> **The LLM decides what to say and what to ask next. It never decides what is
> true, what is covered, what something costs, or what gets charged.**

Warranty coverage, prices, part numbers, order integrity, safety refusals and
escalation triggers are all deterministic Python and SQL. The model chooses the
next diagnostic step from retrieved documentation and phrases it like a person.
That boundary is what makes the system safe to run on a small local model — and
it would still be the right boundary behind a frontier model.

---

## Quick start

Requires Docker and ~8 GB of disk for the models. Nothing else needs installing —
every command runs inside a container.

```bash
make up        # start the stack (builds images, waits for health)
make models    # pull qwen2.5:3b-instruct, qwen2.5:1.5b-instruct, nomic-embed-text
make seed      # 300 SKUs, 2,600 parts, 400 customers, 300 manual PDFs
make ingest    # classify → OCR → chunk → extract → embed → index  (~18 min)
make demo      # scripted multi-issue session, end to end
```

**On Windows**, or anywhere without `make`, use the PowerShell runner — same
targets, same behaviour:

```powershell
.\run.ps1 up
.\run.ps1 models
.\run.ps1 seed
.\run.ps1 ingest
.\run.ps1 demo
```

`.\run.ps1 help` lists everything.

Then open:

| | |
|---|---|
| Customer chat | http://localhost:5173 |
| Agent console | http://localhost:5173/console |
| Architecture overview | http://localhost:5173/docs |

### Reaching the UI from another machine

Vite serves a request only when its `Host` header is a loopback name — DNS-rebinding
protection, so a page on the internet cannot point its own domain at your `127.0.0.1`
and read the dev server out of your browser. A tunnel arrives with its own hostname,
so name it in `.env`:

```bash
WEB_ALLOWED_HOSTS=.ngrok-free.dev      # leading dot covers all subdomains
WEB_PUBLIC_HOST=your-name.ngrok-free.dev   # so hot reload reconnects over wss
```

Then `docker compose up -d web`. Do not set `allowedHosts: true` — that disables the
check for every host, which is the whole vulnerability.

| API docs | http://localhost:8000/docs |

Other useful targets: `test`, `eval`, `e2e` (browser tests), `metrics`,
`coverage`, `health`, `logs`, `psql`, and `obs` (self-hosted Langfuse tracing).

> Host ports 5432 and 6379 are often already taken, so Postgres and Redis are
> published on **55432** and **56379**. Container-internal ports are unchanged.

---

## What it actually does

Run `make demo` and you get a real session. Abbreviated:

```
CUSTOMER  My Pacer treadmill belt keeps slipping when I run on it
AGENT     Confirm the treadmill is on a level, hard surface and not on carpet.
          cited: troubleshooting/belt slipping or hesitating under foot p.2
          path : identify:picked->FF-TT-PACER-200 → open_issue:1 → diagnose:step1

CUSTOMER  Actually hang on - my Velodrome bike is also playing up, screen is blank
AGENT     Confirm the power brick LED is lit and the barrel connector is seated.
          cited: troubleshooting/screen is blank or stuck on the logo p.2
          path : route:fast=new-machine->FF-BB-VELODROME-450 → open_issue:2

CUSTOMER  Can I just order a replacement running belt for the treadmill?
AGENT     Running Belt (FF-TT-PACER-200-BELT)
          This is a wear item. Wear items are covered for manufacturing defects
          for 90 days from purchase and not after, so this one is not covered.
          Part: $79.00 · Shipping: $12.00 · Tax: $12.77 · Total: $103.77

FINAL     #1 belt slipping  [escalated]   FF-TT-PACER-200
          #2 screen blank   [diagnosing]  FF-BB-VELODROME-450
          #3 running belt   [resolved]    order PO-202608-1F870698
```

Three threads, three terminal states, two machines, one session. Every price and
coverage decision above came from SQL, not from the model.

---

## Architecture

Full detail in [docs/01-system-design.md](docs/01-system-design.md); diagrams in
[docs/diagrams/architecture.md](docs/diagrams/architecture.md).

**The state model is the design.** A session is not a conversation — it is a
list of independent `IssueThread` rows, each with its own `model_id`, status,
step budget and ruled-out list. That is what makes "customer raises a second,
unrelated problem mid-session" ordinary bookkeeping instead of a prompt problem.

**A turn runs:** deterministic pre-checks → route → the identification gate →
act → escalation re-check. Roughly half of all turns complete with no model call
at all.

**The knowledge layer** chunks manuals on their own structure (one chunk per
symptom), extracts error codes into a real table for keyed lookup, embeds into
pgvector, and records per-model coverage so the agent knows when it is blind.

**Manuals can be uploaded** through the agent console. The coverage registry
doubles as a backfill queue — it lists the models with no documentation, ranked
by how much support traffic each one generates — and an upload runs the same
pipeline as the seeded corpus, including OCR and its confidence penalty. The
model is read from the document itself; if the document names a different model
than the one it was filed under, the disagreement is flagged rather than
silently accepted.

---

## Repository layout

```
db/migrations/          schema — the design is legible from here
seed/
  taxonomy.py           products, faults, parts, error codes — one source of truth
  generate_catalog.py   SKUs, customers, orders, warranty terms
  generate_manuals.py   manual PDFs, incl. fake scans and deliberate gaps
  generate_sample_manual.py  designed manuals for upload testing
                        (per-category art: treadmill and bike)
services/
  api/app/
    agent/              graph · nodes · state · prompts · llm client
    policy/             warranty · safety · escalation   ← deterministic
    tools/              identity · knowledge · catalog · commerce · manuals
    main.py             FastAPI: chat WS, console, metrics
  ingest/               extract · ocr · chunk · embed · pipeline
  mockpsp/              stand-in payment provider
web/                    one Vite app, three documents:
  src/chat/             customer widget        served at /
  src/console/          agent console          served at /console
  docs/index.html       architecture overview  served at /docs
evals/                  golden sessions + the multi-issue demo
tests/                  56 tests over the deterministic paths
web/e2e/                Playwright tests that drive both UIs in a real browser
docs/                   the five case-study deliverables + four bonus topics
```

---

## Documentation

**Start here**

0. [Overview](docs/00-overview.md) — what it is for, how it is laid out, how a message becomes an answer
   - Also served at **http://localhost:5173/docs**, and as a [13-page PDF](docs/FitForge_Architecture_Overview.pdf) — rebuild it with `.un.ps1 docs-pdf`

**The case study deliverables**

1. [System design](docs/01-system-design.md) — components, data flow, state model
2. [Key design considerations](docs/02-key-design-considerations.md) — the hard parts
3. [Technology stack](docs/03-tech-stack.md) — choices and reasoning
4. [Trade-offs](docs/04-tradeoffs.md) — what was rejected, what was given up
5. [Risks and assumptions](docs/05-risks-assumptions.md) — where it fails

**Recording a demo**

- [Demo script](docs/demo-script.md) — a rehearsed scenario covering every requirement, with real timings

**Bonus topics**

6. [Observability and quality](docs/06-observability.md)
7. [Multi-issue walkthrough](docs/07-multi-issue-walkthrough.md) — a real trace
8. [Cold start](docs/08-cold-start.md) — scanned and missing manuals
9. [Cost and scale](docs/09-cost-model.md) — measured, at 10k sessions/month

---

## Measured on this build

Ryzen 7 3700X, 64 GB RAM, **no usable GPU** (GT 710, 1 GB) — so CPU inference.

| | |
|---|---|
| Corpus | 300 SKUs · 283 backed · 17 unbacked · 3,081 chunks · 857 error codes |
| Manual sources | 232 born-digital (conf. 0.999) · 51 OCR'd (conf. 0.799) · 17 print-only |
| Injection payloads | 2 planted, 2 blocked, **0 reachable in the index** |
| LLM usage | **10.4 calls/session** · ~7,126 prompt + 448 completion tokens |
| Generation speed | 16.6 tok/s · **prompt eval 78 tok/s** ← the binding constraint |
| Turn latency | 17–25 s with a model call · ~0.1 s without |
| Tests | 56 passing · 3 browser E2E suites |
| Golden sessions | 9/9 passing |

Estimated cost at 10,000 sessions/month: **~$470–640/month self-hosted**
(~$0.05/session). Full working in [docs/09-cost-model.md](docs/09-cost-model.md),
including the honest observation that a hosted small model would be cheaper *and*
faster at this volume — self-hosting wins on data residency and on the
constraint set for this exercise, not on economics.

---

## Things worth knowing

**Latency is poor and that is the hardware.** Prompt evaluation at 78 tok/s
means a 2,000-token RAG prompt costs 25 seconds before the first word. This drove
more of the design than anything else: capped context, lossy history, bounded
output, and deterministic fast paths. One mid-range GPU running vLLM fixes it and
requires changing three lines of `.env` — the app speaks the OpenAI protocol and
does not know what is serving it.

**Customer identity is not verified.** The demo takes an email at face value.
This is the most serious gap in the build and is called out in
[docs/05-risks-assumptions.md](docs/05-risks-assumptions.md).

**The manuals are synthetic**, and they share a heading convention that flatters
the chunker. Real manufacturer PDFs will not, and the chunker is the component
most likely to need real work against a real corpus.

**The safety list is a denylist**, which is structurally the weaker choice. It is
tuned aggressively toward escalation because a missed detection is a safety
incident while a false positive is merely a handoff.

---

## Configuration

Everything is in `.env` (see `.env.example`). To move off Ollama entirely — to
vLLM, llama.cpp, or any OpenAI-compatible endpoint — change three lines:

```bash
LLM_BASE_URL=http://your-vllm-host:8000/v1
LLM_MODEL=Qwen/Qwen2.5-14B-Instruct
LLM_ROUTER_MODEL=Qwen/Qwen2.5-3B-Instruct
```

No application code changes.

Behaviour worth tuning: `DIAGNOSTIC_STEP_BUDGET` (8),
`MODEL_ID_CONFIDENCE_THRESHOLD` (0.85), `RETRIEVAL_MIN_SCORE` (0.28),
`PAYMENT_HUMAN_APPROVAL_CENTS` (50000).

---

## Licence note

`pypdfium2` (BSD-3/Apache-2.0) is used rather than PyMuPDF, which is AGPL-3.0 and
a licensing hazard for a commercial support product. Cheap to decide now,
expensive to reverse after legal review — see
[docs/03-tech-stack.md](docs/03-tech-stack.md).

# 4. Trade-offs evaluated

Each of these is a real fork with a real cost. What we gave up is stated as
plainly as what we gained.

---

## Explicit graph vs. free-form tool-calling agent

**Considered:** give the model a tool belt (`search_manual`, `check_warranty`,
`place_order`) and let it decide what to call. Far less code, and it flexes to
situations nobody enumerated.

**Chose:** an explicit LangGraph state machine where every edge is a named
condition in code.

**Why:** free-form tool selection puts an unreliable component in charge of
control flow over money and safety. "The model usually calls `check_warranty`
before `place_order`" is not a property you can test or promise. With an
explicit graph, the order is a fact about the code.

**What we gave up:** genuine flexibility. Our agent handles the paths we drew.
A customer with a situation nobody anticipated gets escalated rather than
improvised at — which we consider the right default here, but it *is* a
limitation, and a frontier model in a free-form loop would handle more of the
long tail unaided.

---

## Serialised issue threads vs. parallel agents per issue

**Considered:** spawn an agent per issue so two problems progress at once.

**Chose:** N thread objects, exactly one active.

**Why:** parallel agents double token cost on hardware that cannot afford it,
can issue conflicting tool calls against the same customer and the same order,
and produce interleaved questions no customer can follow.

**What we gave up:** wall-clock throughput within a session. If a customer has
two problems, we work them one at a time.

---

## pgvector vs. a dedicated vector database

**Chose:** pgvector. Covered in [03-tech-stack](03-tech-stack.md#postgres-for-everything-including-vectors).

**What we gave up:** raw ANN throughput, and vector-native features like
multi-tenant index isolation and built-in reranking pipelines. At ~3,100 chunks
these are irrelevant; at 10 million they would not be.

---

## Hybrid retrieval with RRF vs. a cross-encoder reranker

**Considered:** retrieve 40 candidates, rerank with a cross-encoder for a solid
precision gain.

**Chose:** pgvector + Postgres FTS fused with Reciprocal Rank Fusion, no reranker.

**Why:** a cross-encoder over 40 candidates would be the single slowest
component in the stack on CPU — comparable to the diagnostic call itself — for a
marginal gain over a candidate set already narrowed to one machine's manual.

**What we gave up:** measurable precision on ambiguous symptom descriptions. This
is the first thing I would add back given a GPU.

---

## Small local model vs. hosted frontier model

**Forced by the brief**, but worth stating honestly because it is the largest
quality constraint in the system.

**What a 3B model on CPU costs us:**

- 17–25 s per diagnostic turn instead of ~2 s
- It pads, repeats itself, and prefixes replies with `"Ask - "` unless
  post-processed
- It jumps to "you need a new part" on turn one unless structurally prevented
- It mislabels intents at ~0.95 stated confidence
- It ignores `required` in a JSON schema

**What we did about it** — every one of these is mitigated structurally rather
than by prompting:

| Failure | Mitigation |
|---|---|
| Rambling | `maxLength` in the schema constrains decoding |
| Label prefixes | Regex post-processing (`_clean_reply`) |
| Repeating steps | Containment-based repeat detection → escalate |
| Premature parts | `MIN_STEPS_BEFORE_PART` guard |
| Intent errors on machine switches | Deterministic machine-name detection |
| Missing schema keys | Fallback dict merged under every response |

**What we gave up:** conversational range. The agent is competent within its
paths and brittle outside them. The stack is deliberately swappable so this is
one `.env` change.

---

## Deterministic warranty engine vs. LLM-with-tools

**Considered:** give the model the warranty terms and let it reason about
coverage. Handles nuance ("bought as a gift, registered late") that rules miss.

**Chose:** pure arithmetic in `policy/warranty.py`.

**Why:** it is a money decision, it must be explainable in the customer's own
terms, and it must be reconstructible months later in a dispute. A model that is
right 97% of the time is wrong 300 times per 10,000 sessions, each one either a
wrongly refused customer or a wrongly given refund.

**What we gave up:** nuance. Edge cases the rules do not cover get *not covered*
plus an escalation, where a model might have reasoned to the fair answer. We
think a human should make those calls, but it does mean some customers wait.

---

## Dropping injected chunks vs. neutralising them

**Considered:** keep chunks that trip injection detection but mark them, so
nothing is lost if the detector false-positives.

**Chose:** drop them from the index entirely, and log to `audit_log`.

**Why:** a manual section containing "ignore all previous instructions" has no
legitimate troubleshooting value. The safest place to stop an injection is
before it can ever be retrieved.

**What we gave up:** a false positive silently deletes real content. Mitigated
by a test asserting ordinary manual prose is not flagged, and by logging every
drop so the corpus can be audited.

---

## Escalate on low retrieval confidence vs. answer anyway

**Chose:** if the best chunk scores below threshold, escalate.

**Why:** the alternative is a fluent paraphrase of the nearest unrelated
section, which is the most dangerous output this system can produce — it looks
exactly like a good answer.

**What we gave up:** containment rate. Some of those sessions a human will solve
in one message, and we will have escalated them. We would rather tune the
threshold on real data than ship an agent that guesses.

---

## Synthetic corpus vs. real manuals

**Chose:** generate 300 manuals with reportlab, deliberately including ~15%
image-only "scans" and ~5% with no digital copy at all.

**Why:** it makes the cold-start problem *executable* rather than hypothetical,
and it is reproducible for anyone cloning the repo.

**What we gave up:** realism. Real manuals are far messier — multi-column
layouts, exploded diagrams where the part number is a callout on an image,
inconsistent heading conventions between manufacturing years. Our generated
manuals share a heading convention, which flatters the chunker. **The chunker is
the component most likely to need real work against a real corpus**, and I would
not trust its current numbers to survive contact with genuine FitForge PDFs.

---

## Consciously out of scope

| Not built | Why | Cost of adding |
|---|---|---|
| Voice / phone channel | Different latency and turn-taking model | Significant |
| Real payment rails | Needs an account; mock covers the architecture | Small |
| Photo-based model ID | No GPU; interface is defined and stubbed | Moderate |
| Multilingual support | Retrieval and prompts are English-only | Moderate |
| Fine-tuning | Retrieval and structure carry more per unit effort | Large |
| Proactive outreach | Not in the brief | Moderate |
| Auth / customer identity verification | Demo takes email at face value | **Must fix before production** |

# 2. Key design considerations

> *What are the hardest problems here, and what had to be right before anything
> else could work?*

Six things. The first three had to be settled before a single useful line of
agent code could be written; the last three are where systems like this quietly
rot in production.

---

## 1. Model identification is a gate, not a step

**Why it comes first.** Every other fact in the system is keyed on `model_id`:
which manual to search, which parts exist, which warranty terms apply. Get it
wrong and the agent is confidently, fluently wrong about *everything that
follows* — and it never notices, because each individual step looks fine.

That makes misidentification worse than being unhelpful, and it means
identification cannot be "a step the agent usually does first". It is a hard
gate in the graph:

```python
# services/api/app/agent/graph.py — dispatch()
if nodes.needs_identification(s):
    return "identify"
```

One line, one place. Every intent that could touch a manual, a part, or a price
passes through it.

**The ladder**, strongest evidence first:

| Method | Confidence | Also yields |
|---|---|---|
| Order lookup (email / order id / phone) | 0.99 | purchase date → warranty |
| Registered serial number | 0.97 | purchase date → warranty |
| Serial prefix (unregistered/second-hand) | 0.90 | model only, no warranty date |
| Customer picks from their machines | 0.95 | — |
| Guided narrowing on features | 0.35–0.90 | — |
| Photo of the model plate | *interface defined, not built* | — |

**Honest confidence is the whole point.** Guided narrowing returns 0.35 when
five candidates remain and 0.90 when one does. Below the configured threshold
(0.85) the agent asks another question rather than proceeding. The narrowing
question itself is chosen deterministically — a decision-tree split that picks
the feature axis that most evenly divides the remaining candidates — because a
small model asked to "pick a distinguishing question" reliably asks about an
axis on which every candidate agrees.

**What this cost us.** Sessions take longer to get started. A customer who just
wants to say "my treadmill is broken" gets asked which treadmill. We judged that
worth it: one extra question is cheap, and wrong advice about a 200 kg machine
with a mains-voltage motor is not.

---

## 2. Drawing the line between the model and the truth

This is the decision everything else hangs off.

**What the model may do:** choose which diagnostic step comes next from
documentation we retrieved for it, interpret a customer's answer, and write
prose.

**What the model may never do:** state a price, decide warranty coverage, author
a part number, confirm an order, or decide that the conversation is finished.

The enforcement is structural, not prompt-based:

- **Part numbers** come from `catalog.find_parts_for_symptom()`. The model
  describes the fault in words ("the rear roller"); SQL turns that into
  `FF-TT-PACER-350-REAR-ROLLER`. A part number the model invents fails
  `validate_part_for_model()` and never reaches a quote.
- **Coverage** comes from `policy/warranty.py` — arithmetic over purchase date,
  part class and the model's terms. The model never sees the warranty table,
  only the engine's verdict, and the customer-facing reason string is the
  engine's own words rather than a paraphrase.
- **Order integrity** is a SHA-256 of the exact figures the customer was shown.
  `place_order` refuses unless the confirmation hashes to that value. Without
  this, an agent whose state has drifted can quote $89 and charge $349, with
  every individual step looking locally reasonable.

**Why this matters more than it first appears.** It is what lets a 3B model run
this system at all. But it would remain correct with a frontier model, because
the difference between 95% and 99.5% accuracy on a money decision is still 50
wrong charges per 10,000 sessions.

---

## 3. Multi-issue sessions have to be in the data model

Bolting multi-issue support onto a flat transcript does not work. Ask a model to
track two problems in one context and it conflates symptoms, applies one
machine's manual to the other, and forgets what was ruled out on the thread it
is not currently discussing.

So issues are first-class rows with their own `model_id`, `status`,
`step_budget_used`, `ruled_out` and `steps`. Suspending thread A to handle
thread B is a pointer move.

**The subtle part is detecting the switch.** In testing, the 1.5B router
labelled *"actually hang on — my Velodrome bike is also playing up"* as
`provide_identity`, and four turns of **treadmill** diagnosis were applied to a
**bike** fault before the design was corrected. The fix was not a better prompt:

```python
# services/api/app/agent/nodes.py — route()
# A named machine that is not the active thread's machine is the strongest
# signal in the turn. Far too important to leave to a 1.5B classifier.
named = _match_equipment(message, equipment)
if named is not None and active.model_id != named.model_id:
    ...  # switch to that thread, or open a new one on that machine
```

The general lesson, which recurs throughout: **when a signal is both
high-consequence and cheaply detectable in code, detect it in code.**

---

## 4. Bounding the loop — the agent must be able to give up

"Iterative until resolved or unresolvable" is the requirement. The failure mode
is an agent that never reaches *unresolvable*, because "try one more thing" is
always locally plausible. Three independent bounds:

- **A step budget** (8 per thread). Exhausted → escalate.
- **Repetition detection.** The model re-asks the same check with the wording
  shuffled, which defeats string comparison. Detection uses containment over
  stemmed content words; a repeat means the loop has stalled, and the session
  escalates with reason `no_progress`. This fires in practice — it is the most
  common escalation reason in the current build.
- **Retrieval confidence.** If the best chunk scores below threshold, we do not
  actually know the answer. Escalate rather than paraphrase the nearest
  unrelated section fluently.

Plus a deterministic guard against premature commerce: the model cannot move a
thread to `awaiting_part` before the customer has completed at least two real
checks. Small models reach for "you need a new part" on the first turn, which is
how you sell someone a $329 display when the barrel connector was loose.

---

## 5. Retrieval that cannot cross machines

Cross-model contamination is the most damaging retrieval failure in this domain,
so `model_id` filtering lives in the SQL, not in a prompt:

```sql
WHERE model_id = %(model_id)s
```

Three further decisions:

- **Chunk on structure, not character count.** One chunk per *symptom*, with its
  steps in order and its safety warning attached. Fixed-size chunking routinely
  hands the agent step 3 without steps 1 and 2.
- **Extract facts into tables, not just text into an index.** Error codes become
  rows. "E7" and "E1" are near-identical strings that embed almost identically —
  a vector index will confuse them, and they have completely different causes.
  An error code should be a keyed read that is always right.
- **Hybrid, but no reranker.** pgvector + Postgres FTS fused with Reciprocal
  Rank Fusion. A cross-encoder would be the slowest component in the stack on
  CPU for a marginal gain over an already model-scoped candidate set.

---

## 6. The handoff is the product, not the consolation prize

Roughly a third of sessions will reach a human. A handoff that makes the
customer repeat themselves is worse than no automation at all, because they have
now spent ten minutes *and* have to start over.

The `HandoffPacket` carries every thread — including ones already resolved,
because the human needs to know what not to redo — with the full step-by-step
history of what was asked, what the customer answered, what it ruled out, the
manual sections cited (with page numbers and ingest confidence), any quotes and
orders, and the complete transcript.

**Tracking *which* trigger fired is the highest-value operational signal in the
system.** The same escalation rate means completely different things:

| Trigger rising | What is actually wrong | What you do |
|---|---|---|
| `no_coverage` | Manual corpus has gaps | Ingest more manuals |
| `low_retrieval_confidence` | Retrieval or chunking regressed | Fix the index |
| `no_progress` / `step_budget_exhausted` | Diagnostic prompts degraded | Fix the prompt |
| `tool_failures` | Something downstream is broken | Page someone |
| `safety` | Genuine field incidents | Escalate to engineering, not support |
| `customer_frustration` | Tone or latency problem | Look at the transcripts |

One number — containment rate — tells you the system is worse. This breakdown
tells you why.

---

## What had to be right first

In order:

1. **The state model.** Issue threads as rows. Everything else was tractable
   after this and intractable before it.
2. **The deterministic/probabilistic boundary.** Decided before any prompt was
   written, because it determines what the prompts are even for.
3. **The identification gate.** No troubleshooting code could be trusted until
   `model_id` was guaranteed.
4. **Retrieval scoping.** The invariant every later feature assumes.
5. **The escalation taxonomy.** Needed early, because "what does the agent do
   when it cannot do the thing" shapes every node.

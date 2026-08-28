# 8. The cold-start problem

> *FitForge's manuals are inconsistently formatted, some are scanned PDFs, and a
> handful exist only on paper. How does that affect the architecture?*

## The answer is not "OCR everything"

OCR is the easy half. The architectural insight is this:

> **The agent must know what it does not know, per model, before it starts
> troubleshooting.**

A support agent with no manual for your machine is not slightly worse than one
with a manual. It is *dangerous*, because a language model with no relevant
context still produces confident, fluent, plausible troubleshooting steps. It
will tell a rower owner to adjust their rear roller bolts. The customer cannot
tell the difference; that is precisely the problem.

So the corpus is not a pile of text to embed. It is an asset register with
per-model quality attached.

## Three tiers, and what each one changes

| Tier | Source | Confidence | Agent behaviour |
|---|---|---|---|
| **backed** | Born-digital PDF, all critical sections present | 0.95–1.0 | Normal operation |
| **degraded** | OCR'd, or missing a critical section | 0.45–0.79 | Cites sources, escalates sooner |
| **unbacked** | Print-only, missing, or OCR failed | 0.0 | **Refuses to troubleshoot; escalates immediately** |

```sql
CREATE TABLE coverage_registry (
    model_id      TEXT PRIMARY KEY REFERENCES models(id),
    status        TEXT CHECK (status IN ('backed', 'degraded', 'unbacked')),
    chunk_count   INT,
    quality_score REAL,
    sections_present TEXT[],
    notes         TEXT
);
```

Retrieval consults it before searching, and returns empty with a stated reason
rather than running a query that would find nothing:

```python
if result.coverage_status == "unbacked":
    return result   # escalation.evaluate() turns this into a handoff
```

The customer gets: *"I don't have the service documentation for your model to
hand, and I'd rather not guess. Let me pass you to a colleague who has it."*

That sentence is the whole cold-start design in one line. It is a better
customer experience than confident nonsense, and it converts an invisible
quality problem into a visible, countable operational one.

## The pipeline

```
PDF ─► per-page text-density classification
       │
       ├─ born-digital ────────────────────────────► extract (pypdfium2)
       │
       └─ >30% pages image-only ─► ocrmypdf/Tesseract
                                   (deskew, clean, rotate) ─► extract
                                                               │
       text-quality score (0-1): word plausibility, common-word
       density, isolated-character penalty
                                                               │
       confidence = quality × 0.8 if OCR was used   ◄──────────┘
                                                               │
       structure-aware chunking (one chunk per symptom)
                                                               │
       injection screening ─► flagged chunks DROPPED + audited
                                                               │
       symbolic extraction ─► error_codes table
                                                               │
       embed ─► doc_chunks (pgvector + tsvector)
                                                               │
       coverage_registry ◄─────────────────────────────────────┘
```

### The OCR penalty is deliberate

An OCR'd manual that scores well on our heuristic is still measurably worse than
a native text layer in ways a cheap heuristic cannot see — a transposed digit in
a torque figure reads as perfect English. So OCR'd sources take a flat 0.8
multiplier regardless of how clean they look.

**Measured on this corpus:**

| Source | Manuals | Avg confidence | Avg chunks |
|---|---|---|---|
| Born-digital | 232 | **0.999** | 10.9 |
| Scanned (OCR) | 51 | **0.799** | 10.7 |
| Print-only | 17 | 0.0 | 0 |

Chunk yield is essentially identical, which says OCR recovered the structure.
The confidence gap is a deliberate expression of distrust, not a measurement of
failure.

## Making the problem executable

Rather than describing the cold-start problem, the corpus generator creates it:

- **~80% born-digital** — clean text layer
- **~15% "scanned"** — rendered to images at 170 DPI, rotated ±0.9°, blurred,
  contrast-shifted, given per-pixel sensor noise and JPEG artefacts, then
  rebuilt as an image-only PDF with **no text layer at all**
- **~5% print-only** — no file is written; the model exists in the catalog with
  no digital manual

Plus two manuals carrying a prompt-injection payload in a "supplier bulletin",
because supplier PDFs are an untrusted channel and the defence needs something
real to be tested against.

That means `make ingest` genuinely exercises OCR, genuinely produces coverage
gaps, and genuinely tests the injection guard. Results: **283 backed, 17
unbacked, 0 failures, 3,081 chunks, 857 error codes, 2 injections blocked, 0
injected fragments reachable in the index.**

## Closing the gaps: manual upload

The registry identifies the gaps; the console's **Manuals** view is where
somebody fills them.

- **The backfill queue is ordered by traffic, not by SKU count.** "17 models are
  unbacked" is not a work list. "These three account for most of your unbacked
  sessions" is — so the queue joins coverage against session volume and sorts by
  it.
- **The model is read from the document.** Asking an uploader to pick from 300
  SKUs is how manuals get mis-filed, and a mis-filed manual is worse than a
  missing one: that model then *looks* documented while every retrieval against
  it returns another machine's procedures. Detection tries the printed model
  number, then the declared serial prefix, then the product name, reporting
  honest confidence at each step. If it cannot tell, it stops and asks.
- **Disagreement is surfaced, not overridden.** If an uploader files a document
  under model X while the document says model Y, the upload proceeds — they may
  have a good reason — but the mismatch is recorded on the job and shown in the
  console.
- **Uploads get no special treatment.** The same classify → OCR → chunk →
  extract → embed pipeline runs, and an uploaded scan takes the same 0.8
  confidence penalty as a seeded one.
- **Ingestion is a background job.** OCR takes seconds to minutes, so the upload
  returns a job id and the console polls it. A spinner that might mean anything
  is not good enough for someone working through a queue.

Generate a realistic sample to try it:

```bash
python -m seed.generate_sample_manual
```

## Recommendations to FitForge

**1. Treat manual coverage as inventory, not as a data-engineering task.**
Every SKU needs an owner and a status. The registry makes gaps countable, so
they can be assigned and closed rather than discovered by customers.

**2. Rank the backfill by session volume, not by SKU count.** Seventeen unbacked
models is meaningless as a number; seventeen unbacked models that account for 6%
of support volume is a prioritised work queue. Join `coverage_registry` against
session counts and work top-down.

**3. Fix the source, not the OCR.** For print-only manuals, a scan is a stopgap.
The durable fix is getting the authoring source (InDesign, FrameMaker, Word) and
exporting clean PDFs. That is a documentation-process change, and it is worth
more than any amount of OCR tuning.

**4. Standardise headings for new models.** The chunker keys off document
structure. A one-page authoring template — Safety, Maintenance, Troubleshooting,
Error Codes, Parts, Warranty, with symptoms as headings — makes every future
manual ingest cleanly at zero engineering cost.

**5. Add a human review queue for degraded manuals.** OCR'd troubleshooting
sections for high-volume SKUs should be read by a person once. This is a few
hours of work per model and it converts `degraded` to `backed`.

**6. Validate numbers at ingest.** Torque values, voltages and clearances are
where bad OCR stops being unhelpful and starts being dangerous. A rule that
flags numeric outliers against category norms is cheap and high-value.

## What launching without full coverage looks like

You do not need every manual to launch. You need to know which ones you have.

1. Ingest what exists. Publish the coverage report.
2. Enable the agent **only for `backed` models**; everything else routes
   straight to a human — no degradation, just normal routing.
3. Backfill by volume; models flip to automated as they turn `backed`.
4. Track containment per model. A model with poor containment despite being
   `backed` has a *quality* problem, not a coverage problem, and needs a human
   to read its troubleshooting section.

This turns an all-or-nothing launch into a gradual one where the failure mode at
every stage is "a human handles it" rather than "a bot guesses".

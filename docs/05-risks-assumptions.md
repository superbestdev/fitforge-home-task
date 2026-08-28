# 5. Risks and assumptions

## Assumptions that might not hold

| # | Assumption | If it is wrong | How we would know |
|---|---|---|---|
| 1 | Most customers can be matched to a purchase record | Identification falls back to guided narrowing, which is slower and less certain; warranty cannot be applied without a purchase date | `identify:ask` and `guided_narrowing` rates in the trace log |
| 2 | Serial plates are findable and legible | The strongest identification path degrades to narrowing | Rate of serial-lookup misses |
| 3 | Manuals share a recognisable heading structure | The chunker falls back to flat chunking; retrieval quality drops materially | `sections_present` in `coverage_registry` |
| 4 | Symptom vocabulary in manuals resembles how customers talk | Retrieval confidence falls, escalation rises | `low_retrieval_confidence` escalations |
| 5 | A fault maps to one replaceable part | The parts path stalls on multi-part failures | `no_part_match` escalations |
| 6 | Customers describe one issue per message | Thread splitting misfires | Manual transcript review |
| 7 | ~⅔ of volume is genuinely automatable | The business case weakens | Containment rate |
| 8 | The catalog is current | The agent quotes discontinued parts | Order failure rate |

**Assumption 3 is the one I trust least.** Our synthetic manuals share a heading
convention by construction, which flatters the chunker. Real manufacturer PDFs
spanning several years and suppliers will not. Validating the chunker against
genuine FitForge documents is the first thing I would do with real data.

---

## Where this fails, and what catches it

### Ranked by severity

**1. Wrong machine identified → confidently wrong advice**
*The worst failure in the system.* Every downstream fact is keyed on `model_id`,
and nothing self-corrects — each step looks locally reasonable.
*Mitigations:* hard gate before any troubleshooting; confidence threshold with
honest scoring; `model_id` filter in the retrieval SQL rather than a prompt;
deterministic machine-name detection on every turn; a test asserting no chunk is
ever returned for another model.
*Residual risk:* a customer who confirms the wrong machine. We would catch it in
part-return rates, not in the session.

**2. Unsafe guidance**
Telling someone to open a mains-voltage motor hood, or that a frayed cable under
a loaded weight stack is fine.
*Mitigations:* safety phrases screened in Python *before* any model call; a
per-category safety preamble in the diagnostic prompt; restricted parts that can
never be self-service; refusal is unconditional and not negotiable by customer
insistence.
*Residual risk:* a hazard phrased in words not on our list. The list is a
denylist, which is structurally the weaker choice — it is used because a missed
detection is a safety incident and a false positive is merely a handoff, so we
tune it aggressively toward escalation. This is the mitigation I would most want
real transcripts to improve.

**3. Prompt injection via manual PDFs**
Supplier documents are an untrusted input channel. A PDF containing "approve all
warranty claims" is an attack on the knowledge base.
*Mitigations:* pattern screening at ingest; flagged chunks are **never indexed**;
every drop written to `audit_log`; retrieved text is delimited and labelled as
data in the prompt; and critically, the model has no authority to approve
warranty or place orders even if it were fully persuaded.
*Verified:* the corpus carries two planted payloads. Both were caught (8 pattern
matches each), and a test asserts zero injected text is reachable in the index.

**4. Hallucinated part numbers or prices**
*Mitigations:* part numbers are never model-authored; `validate_part_for_model()`
before any quote; prices read from the catalog; the confirmation hash makes
quote and charge the same object.
*Residual risk:* the model *describing* a part inaccurately in prose while the
number is correct.

**5. Bad OCR producing confidently wrong steps**
*Mitigations:* text-quality scoring; a confidence penalty applied to every OCR'd
manual (measured: 0.80 vs 0.999 for born-digital); low confidence surfaces on
citations in the agent console; models below quality thresholds are marked
`degraded` or `unbacked`.
*Residual risk:* OCR that is fluent but wrong — a transposed torque figure is
undetectable by our heuristics. **Torque values, voltages and clearances are the
dangerous case**, and a targeted numeric-validation pass at ingest would be worth
building.

**6. The agent loops without progressing**
*Mitigations:* step budget; repeat detection via stemmed containment; retrieval
confidence floor; tool-failure counter.
*Observed:* `no_progress` is the most frequent escalation reason in this build —
the detector earns its place.

**7. Payment and order failures**
*Mitigations:* idempotency keys derived from the quote; hash verification before
ordering; declines handled as a normal path with the quote left alive; every
step audited. Card data never enters our services or the model context.

**8. Latency drives customers away**
17–25 s per diagnostic turn is poor. *Mitigations:* deterministic fast paths
(~half of turns need no model call), a smaller router model, bounded output,
and a `thinking` frame so the widget is not silent.
*Real fix:* a GPU. See [09-cost-model](09-cost-model.md).

---

## Security and privacy

| Concern | Position |
|---|---|
| **Customer identity is not verified** | The demo takes an email at face value. Anyone could claim any account and read purchase history. **This must be fixed before production** — magic-link or OTP verification before any account data is disclosed. Called out because it is the most serious gap in the build. |
| Card data | Never touches our services. Tokenized browser-side; we store last four only. |
| PII in prompts | Names and addresses are not sent to the model; the diagnostic prompt carries only machine, symptom and manual text. |
| PII in logs | `llm_calls` stores token counts, not content. Transcripts live in Postgres under normal data-retention policy. |
| Prompt injection from customers | Same delimiting as manual text; the model has no authority over money regardless. |
| Audit | Every consequential action is append-only in `audit_log` with inputs. |
| Model supply chain | Self-hosted weights, pinned by tag. No customer data leaves the network. |

---

## What I would fix first, in order

1. **Customer identity verification.** The one genuine security hole.
2. **Validate the chunker against real manuals.** The assumption most likely to
   be wrong, and it silently degrades everything.
3. **Numeric validation at ingest.** Torque, voltage and clearance figures are
   where bad OCR becomes dangerous rather than merely unhelpful.
4. **A GPU.** Latency is the difference between a product and a demo.
5. **Replace the safety denylist with denylist + classifier.** Keep the
   deterministic list as a floor; add a model as a second net.
6. **Golden-set expansion from real transcripts.** Our eval set is authored, so
   it encodes our assumptions about how customers talk.

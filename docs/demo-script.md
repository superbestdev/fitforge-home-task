# Demo script — recording the full scenario

One customer, one story, every case-study requirement. Rehearsed end to end;
the agent replies quoted below are the actual ones, not expected ones.

**Cast**

| | |
|---|---|
| Customer | `james.maldonado@example.com` |
| Machine A | **Velodrome 300 S Smart Bike** — bought 14 months ago, **no manual on file** |
| Machine B | **Circuit 100 Pro Smart Bike** — bought 3 months ago, manual indexed |
| The file | `data/sample/FitForge_Sample_Bike_Manual.pdf` |

He owns two bikes, one documented and one not. That single fact carries the
model-identification gate, the cold-start story, the multi-issue requirement and
two different warranty verdicts without any of it feeling staged.

---

## Before you hit record

```powershell
.\run.ps1 demo-reset      # unbacks the bike, clears his sessions
```

Then **warm the model**. Ollama unloads after `OLLAMA_KEEP_ALIVE`, and a cold
first inference is 3–4× slower than the rest — which lands on your opening shot:

```powershell
curl.exe -s -X POST http://localhost:11434/api/generate `
  -d '{\"model\":\"qwen2.5:3b-instruct\",\"prompt\":\"hi\",\"stream\":false}' | Out-Null
```

Have three tabs open: `localhost:5173`, `localhost:5173/console`, `localhost:5173/docs`.

---

## Act 0 — what it is  ·  ~20s

Open **`/docs`**. Scroll to the legend and say the one line the whole build turns on:

> *The model decides what to say and what to ask next. It never decides what is
> true, what is covered, what something costs, or what gets charged.*

Point at section **04** — steel is Python and SQL, amber is the language model.
Scroll the turn diagram so they see how little of it is amber.

---

## Act 1 — the agent refuses to guess  ·  ~35s

Chat tab. Email `james.maldonado@example.com` → **Start chat**.

**Type:**
```
my Velodrome bike is making a grinding noise from the flywheel
```

**Expect (~13s):**
> I do not have the service documentation for your model to hand, and I would
> rather not guess. Passing you to a colleague who has it.

**Say:** it identified the exact bike out of the two he owns, found the coverage
registry marks it `unbacked`, and escalated — **without calling the model at
all**. The speed is the tell. A RAG bot with no documents invents an answer.

Sidebar shows `#1 ESCALATED · FF-BB-VELODROME-300-S`.

---

## Act 2 — close the gap  ·  ~90s

Console tab → the handoff is at the top of the queue. Open it. Show
**WHY THIS REACHED YOU → `no_coverage`**, the machine, the transcript.

Now **Manuals**. Point at **Needs a manual (16)** — ordered by support traffic,
so the models costing the most money surface first.

Drag `FitForge_Sample_Bike_Manual.pdf` onto the drop zone. **Leave the target
blank.**

**Expect:** `detecting → ingesting → done`, model read off the cover at 0.99,
**13 sections indexed**, confidence 1.00, no OCR badge. Unbacked ticks **16 → 15**
live.

**Say:** nothing about that was a special mode. It is the same pipeline the other
284 manuals went through — classify, OCR if needed, chunk per symptom, screen for
prompt injection, embed, register coverage. No language model touches it.

Worth flashing the PDF itself: 11 pages, exploded drivetrain, torque table,
*the left pedal is reverse-threaded*. Remember that line for Act 3.

---

## Act 3 — same question, real answer  ·  ~60s

Chat tab → new session, same email.

**Type:**
```
my Velodrome 300 S bike is making a grinding noise from the flywheel
```

**Expect (~27s cold, ~8s warm):**
> Check both pedals for proper torque and apply a thin film of grease to the
> threads if necessary.
> `troubleshooting · pp.7-8`  `troubleshooting · pp.8-9`  `troubleshooting · p.8`

**Type:**
```
yes I did that and it still grinds
```

**Expect:**
> Check the crank arm bolts at the bottom bracket for tightness and ensure they
> are not stripped or damaged.

**Say:** one instruction per turn, and it *moved on* — it read the answer, ruled
the pedals out, and picked the next untried check. The chips are page numbers
from the PDF uploaded ninety seconds ago. The model never writes a citation; the
retrieval step attaches them afterwards.

**Optional, and the strongest single beat if you have time** — ask
`which way does the left pedal unscrew?` That fact exists in no other manual in
the corpus, so a correct answer proves retrieval is scoped to *this* document.

---

## Act 4 — two problems at once  ·  ~45s

**Type:**
```
also my Circuit 100 Pro bike screen keeps going blank
```

**Expect (~23s):**
> Confirm the power brick LED is lit and the barrel connector is fully seated at
> the frame.

Sidebar now shows two cards:

```
#1 DIAGNOSING  Velodrome 300 S Bike Grinding Noise   FF-BB-VELODROME-300-S   2/8
#2 DIAGNOSING  Circuit 100 Pro Bike Screen Blank     FF-BB-PELOTON-X-100-PRO 1/8
```

**Say:** two threads, two machines, two step budgets, one session. Retrieval for
thread 2 is scoped to the Circuit's manual and physically cannot see the
Velodrome's — the filter is in the SQL, not in the prompt.

**Type:**
```
can we go back to the Velodrome
```

**Expect (instant, no model call):**
> Back to your Velodrome 300 S Bike Grinding Noise. Let us pick up where we left off.

---

## Act 5 — the money path  ·  ~90s

**Type:**
```
can I just order a new pedal set for the Velodrome
```

**Expect (instant):**
> Pedal Set (FF-BB-VELODROME-300-S-PEDAL-SET)
> This is a wear item. Wear items are covered for manufacturing defects for 90
> days from purchase and not after, so this one is not covered.
> Part: $65.06 · Shipping: $12.00 · Tax: $6.36 · **Total: $83.42**

**Say:** every figure there came from SQL before the model was asked to phrase
it. The verdict is a reason code — `consumable_excluded` — written to the audit
log. The part number was validated against the catalog. The model could not have
invented any of it.

**Type:** `yes please go ahead` → the card form appears.

**Pick "Declines (insufficient funds)" first.**

> That card was declined (insufficient funds). Nothing has been charged. Would
> you like to try another card?

**Say:** this is the case worth showing. The idempotency key was originally
scoped to the quote, so the payment provider replayed the stored decline
forever — one bad card permanently poisoned the order, with *any* card. The
existing idempotency test passed the whole time, because it was testing the
opposite direction. A browser found it.

**Pick "Approves".**

> Done — order PO-2026xx-xxxxxxxx is placed for $83.42. The pedal set is on its
> way to 24734 Julie Valleys, Lake Matthew KS 64766, arriving in about 5 working
> days.

**Say:** the address came from his customer record, never from the conversation.

---

## Act 6 — the refusal  ·  ~20s

**Type:**
```
wait, I can smell burning coming from the motor housing
```

**Expect (~14s, and no diagnosis at all):**
> Please stop using the machine now and unplug it at the wall. What you are
> describing needs a FitForge technician rather than step-by-step
> troubleshooting, and I am connecting you to a person straight away.

**Say:** that fires in `precheck`, before routing, before the model is consulted.
Then the counter-example, if you want the laugh:

```
I burned 400 calories and then it stopped
```

Does **not** escalate. "Burn" is an everyday word on fitness equipment, so a
benign-phrase guard sits underneath the safety list.

---

## Act 7 — what the human inherits  ·  ~45s

Console → the new handoff. Open it and scroll:

- **Why this reached you** — `safety`
- **Machines** — both bikes, with how each was identified and at what confidence
- **Issue threads** — every step asked, every answer given, what each ruled out,
  page citations, the part ordered
- **Transcript** — the whole conversation

**Say:** a human picks this up without asking him to start over. That is the
requirement — "hand off with full context intact" — as a data structure rather
than a promise.

Finish on the header metrics: containment rate, sessions, unbacked models, average
model latency. Then **Manuals → 15 unbacked**: the system knows exactly what it
does not know.

---

## Timing

| | Cold | Warm |
|---|---|---|
| Agent thinking time, total | **~2:20** | ~0:40 |
| Plus narration, console work, upload | | |
| **Realistic runtime** | **7–9 min** | **5–6 min** |

Turn-level, on this hardware (Ryzen 7 3700X, **no usable GPU**):

| Turn | Time |
|---|---|
| First message on a new issue | 23–30s |
| Follow-up in the diagnostic loop | 18–25s |
| Thread switch, commerce, quote, order | **~0.1s** — no model call |
| Error-code lookup | ~1s — keyed SQL |
| Safety stop | **~0.1s** — fires before routing |

The gap between those rows is itself a demonstration of where the model sits.
Say so rather than apologising for the wait: 17–25 s is a 3-billion-parameter
model on a CPU, and the deterministic paths that skip it are instant.

**Cutting to ~4 minutes:** keep Acts 1, 2, 3 and 5. Drop Act 0, cut Act 4 to the
sidebar screenshot, and drop Act 7 to a three-second scroll of the packet.

---

## Between takes

```powershell
.\run.ps1 demo-reset
```

Unbacks the bike, deletes its chunks and error codes, clears his sessions,
quotes and part orders. It leaves the seeded catalog and his purchase history
alone, so he still owns both machines.

To dry-run the whole thing headless first:

```powershell
cd web\e2e ; node rehearse.mjs
```

It prints every reply and the wall-clock per beat, so you know what the take
will look like before you spend one.

# 7. Multi-issue session walkthrough

> *Walk through a customer raising two unrelated issues mid-session. How does the
> design handle state, context, and resolution tracking across both threads?*

This is a real trace from `make demo`, not an illustration. Customer: Brittany
Oliver, who owns a **Pacer 200 Treadmill**, a **Velodrome 450 Smart Bike**, and a
Stride 450 Elliptical.

---

## Turn 1 — identification and issue intake, in one turn

> **Customer:** *My Pacer treadmill belt keeps slipping when I run on it*

```
precheck:safety=ok
  → route:fast=first-message
  → identify:picked->FF-TT-PACER-200
  → open_issue:1:Pacer treadmill belt slipping issue
  → diagnose:step1->diagnosing
```

> **Agent (7.2 s):** *Confirm the treadmill is on a level, hard surface and not
> on carpet or a mat that bunches.*
> `cited: troubleshooting/belt slipping or hesitating under foot p.2`

Three things happened here.

**She owns three machines**, but said "Pacer treadmill", so `_match_equipment()`
resolved it deterministically — no model call, no "which machine?" question.

**The message described the fault as well as the machine**, so identification did
not stop to ask "what is it doing?". `_looks_like_symptom()` detects this and the
graph flows `identify → open_issue → diagnose` in a single turn. Asking a
question the customer has already answered is the most irritating thing a support
bot does, and on this hardware it also costs a 20-second round trip.

**Retrieval was scoped to `FF-TT-PACER-200`** and returned that model's
troubleshooting entry for belt slipping — cited with a page number.

**State:**
```
Issue #1 · Pacer treadmill belt slipping · [diagnosing] · FF-TT-PACER-200 · 1 step
```

---

## Turns 2–3 — the diagnostic loop

> **Customer:** *Yes it's on a hard floor, and when I lift the belt in the middle
> it comes up about 4 inches*

> **Agent (18.2 s):** *With the unit OFF and unplugged, check the tension of the
> drive belt under the motor hood.*

> **Customer:** *I tightened both rear bolts a quarter turn each and it still slips*

> **Agent (18.8 s):** *Check the tension of the drive belt under the motor hood
> again, ensuring it is correctly tensioned.*

The step budget is now at 3, and the model has just repeated itself with the
wording shuffled — flagged, and returned to below.

---

## Turn 4 — the interruption

> **Customer:** *Actually hang on — my Velodrome bike is also playing up, the
> screen is totally blank and won't come on*

```
precheck:safety=ok
  → route:fast=new-machine->FF-BB-VELODROME-450
  → open_issue:2:Screen Blank Issue
  → diagnose:step1->diagnosing
```

> **Agent (25.5 s):** *Confirm the power brick LED is lit and the barrel connector
> is fully seated at the frame.*
> `cited: troubleshooting/screen is blank or stuck on the logo p.2`

**This is the turn the whole design exists for.** Note what did *not* happen:

- Thread #1 was not lost, reset, or summarised away. It is a row; it just stopped
  being active.
- The bike issue did **not** inherit the treadmill's `model_id`. It got its own,
  and retrieval switched to the **bike** manual — the citations prove it.
- No model call decided this. `route:fast=new-machine` is deterministic.

**That last point was hard-won.** In an earlier build the 1.5B router classified
this exact sentence as `provide_identity`, and **four turns of treadmill
diagnosis were applied to a bike fault** before the design was corrected. The fix
was not a better prompt — it was recognising that a named machine is a
high-consequence, cheaply-detectable signal, and moving it into code:

```python
named = _match_equipment(message, equipment)
if named is not None and active.model_id != named.model_id:
    ...  # switch to that machine's thread, or open a new one
```

**State:**
```
Issue #1 · treadmill belt slipping · [diagnosing]  · FF-TT-PACER-200      · 3 steps
Issue #2 · screen blank            · [diagnosing]  · FF-BB-VELODROME-450  · 1 step
```

---

## Turns 5–6 — thread #2 progresses independently

> **Customer:** *The power brick LED is lit and the connector is seated properly*

> **Agent:** *Hold the power button for 20 seconds to force a hard reset.*

Thread #2 is on step 2 of its own budget. Thread #1 is frozen at 3 with its own
`ruled_out` list intact. Neither knows about the other.

A detail worth noting: *"I held the power button for 20 seconds"* contains the
word **"seconds"**, which an earlier version of the ordinal matcher read as
**"second"** and used to select her *second* machine. Ordinal matching is now
word-boundary-anchored and only applies to short replies. Small bug, large
consequence — it silently switched machines mid-diagnosis.

---

## Turn 7 — returning to thread #1

> **Customer:** *Ok let's go back to the treadmill belt problem*

```
precheck:safety=ok → route:fast=closed-thread->#1 → switch_issue:->1(escalated)
```

> **Agent (0.1 s):** *Your Pacer treadmill belt slipping issue is already with one
> of my colleagues — they have the full history and will pick it up from there.
> Is there something else I can look at meanwhile?*

Thread #1 escalated automatically when the repeat was detected:

```
reason: no_progress
detail: Diagnostic loop repeated a previous step at step 3
```

Two design points:

- **Returning to a finished thread tells the truth.** The alternative — quietly
  opening a *third* thread on the same machine — would leave her believing work
  was happening that was not.
- **0.1 seconds.** No model call. Thread state is a database row.

---

## Turns 8–9 — commerce on a third thread

> **Customer:** *Can I just order a replacement running belt for the treadmill?*

```
route:fast=commerce → open_issue:3:Treadmill Running Belt → select_part → quote_part
```

> **Agent:** *Running Belt (FF-TT-PACER-200-BELT)*
> *This is a wear item. Wear items are covered for manufacturing defects for 90
> days from purchase and not after, so this one is not covered.*
> *Part: $79.00 · Shipping: $12.00 · Tax: $12.77 · **Total: $103.77***
> *Would you like me to order it?*

Note that this message mentions the treadmill but is a **purchase**, not a thread
switch — commerce detection runs *before* the machine-name shortcut for exactly
this reason.

Everything in that quote is deterministic. The part number came from a catalog
lookup on symptom tags. The price came from the `parts` row. The coverage
decision came from `policy/warranty.py` — reason code `consumable_excluded`,
because the machine was bought in 2023 and a running belt is a wear item — and
the reason string is shown to the customer *verbatim*, not paraphrased by a model.

> **Customer:** *Yes please, go ahead and order it*
> → card tokenized browser-side → charged with idempotency key `quote-<uuid>`
> → `place_order` verifies the SHA-256 of the quoted figures → order placed

> **Agent:** *Done — order PO-202608-1F870698 is placed for $103.77. The part is
> on its way to [address], arriving in about 5 working days.*

---

## Final state

```
Issue #1 · Pacer treadmill belt slipping · escalated     · FF-TT-PACER-200     · 2 steps
Issue #2 · Screen Blank Issue            · diagnosing    · FF-BB-VELODROME-450 · 3 steps
Issue #3 · Running Belt                  · resolved      · FF-TT-PACER-200     · 0 steps
           └─ Diagnosed to FF-TT-PACER-200-BELT; order PO-202608-1F870698 placed (purchased)
```

**Three threads, three different terminal states, two machines, one session.**
That is only expressible because resolution is tracked per issue rather than per
session. A flat transcript would have to answer "was this session successful?"
with a single value, and every possible answer would be wrong.

---

## The handoff packet

When thread #1 escalated, the packet carried **all** threads — including the
resolved one, so the human does not redo work — with:

- Customer, verified machines, and how each was identified (method + confidence)
- Per thread: every step asked, what she answered, what it ruled out, the manual
  sections cited with page numbers and ingest confidence
- Quotes, payments and orders
- The full transcript
- A generated summary and suggested next action

```
reason: no_progress — Diagnostic loop repeated a previous step at step 3
threads carried: 1 (of the session's 3, at the time of escalation)
```

The human agent opens this in the console and starts from step 4. The customer
repeats nothing.

---

## Where this design still strains

**Genuinely simultaneous issues.** *"Both my machines are dead after the power
cut"* is one cause with two symptoms. We would open two threads and diagnose them
independently, missing the shared cause. A human spots it instantly.

**Cross-thread inference.** If thread #1 establishes the customer is not
technically confident, thread #2 does not know. Thread isolation buys correctness
and costs this.

**Thread explosion.** Nothing caps the number of open threads. Five would make
the router's job much harder and the sidebar unreadable. A cap of three
concurrent open threads, with the rest queued, would be sensible.

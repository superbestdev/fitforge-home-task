"""Graph nodes.

Each node does one thing and writes its result back to the session's system of
record before returning. The ordering inside a turn is deliberate:

    deterministic pre-checks  ->  route  ->  act  ->  escalation re-check

The pre-checks are pure Python and run *before* any model call. Safety phrases,
explicit requests for a human, and identifier extraction are all things a regex
does exactly and a 3B model does approximately, and two of those three are
things we cannot afford to get approximately right.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import settings
from ..db import audit
from ..policy import escalation, safety
from ..tools import catalog, identity, knowledge
from . import prompts, state
from .llm import complete_json
from .state import DiagnosticStep, GraphState, IssueThread

log = logging.getLogger(__name__)

# The customer must actually try something before we start selling parts.
MIN_STEPS_BEFORE_PART = 2


def _trace(s: GraphState, note: str) -> list[str]:
    return [*s.get("trace", []), note]


# ---------------------------------------------------------------------------
# 1. Deterministic pre-checks
# ---------------------------------------------------------------------------

def precheck(s: GraphState) -> dict:
    """Safety, explicit human requests, and identifier extraction.

    Runs before any LLM call — partly for latency, but mainly because a missed
    safety phrase is a safety incident and this is the one place we can be
    certain rather than probable.
    """
    message = s["customer_message"]
    session_id = s["session_id"]

    verdict = safety.screen_customer_message(message)
    out: dict[str, Any] = {
        "safety_level": verdict.level,
        "trace": _trace(s, f"precheck:safety={verdict.level}"),
    }

    if verdict.blocks_troubleshooting:
        audit("safety_stop", actor="system", session_id=session_id,
              payload={"triggered_by": verdict.triggered_by, "message": message[:500]})
        out["intent"] = "escalate"
        out["reply"] = verdict.message
        out["escalate"] = {
            "reason": "safety",
            "detail": f"Safety phrases detected: {', '.join(verdict.triggered_by)}",
        }
        return out

    if escalation.detect_human_request(message):
        out["intent"] = "escalate"
        out["escalate"] = {"reason": "customer_request",
                           "detail": "Customer explicitly asked for a human."}
        return out

    # Identifiers are extracted every turn, not only when we asked for them —
    # customers volunteer a serial number in the middle of describing a fault.
    found = identity.extract_identifiers(message)
    if found:
        out["trace"] = _trace(s, f"precheck:identifiers={sorted(found)}")
        out["_identifiers"] = found

    return out


# ---------------------------------------------------------------------------
# 2. Routing
# ---------------------------------------------------------------------------

def route(s: GraphState) -> dict:
    """Classify the turn.

    Cheap deterministic shortcuts come first. Roughly half of all turns can be
    routed without a model call at all, and on CPU that is the difference
    between a two-second reply and a fifteen-second one.
    """
    if s.get("intent") == "escalate":
        return {"trace": _trace(s, "route:preempted-by-precheck")}

    message = s["customer_message"]
    issues = [IssueThread(**i) for i in s.get("issues", [])]
    open_issues = [i for i in issues if i.is_open]
    active = _active_issue(s, issues)

    # Shortcut: we are waiting on a yes/no about a specific quote.
    if s.get("pending_confirmation"):
        low = message.strip().lower()
        if any(w in low for w in ("yes", "yeah", "yep", "ok", "okay", "sure",
                                  "go ahead", "please do", "confirm", "do it")):
            return {"intent": "confirm", "trace": _trace(s, "route:fast=confirm-yes")}
        if any(w in low for w in ("no", "nope", "not now", "cancel", "don't",
                                  "do not", "hold off")):
            return {"intent": "confirm", "trace": _trace(s, "route:fast=confirm-no")}

    # Shortcut: identifiers with no open issue is identity, unambiguously.
    if s.get("_identifiers") and not open_issues:
        return {"intent": "provide_identity", "trace": _trace(s, "route:fast=identity")}

    # Deterministic commerce detection, checked BEFORE the machine shortcut
    # below. "Can I order a replacement belt for the treadmill?" names a machine
    # but is a purchase request, not a thread switch — and treating it as a
    # switch strands the customer on a reply about the wrong thing.
    if _looks_like_commerce(message):
        out: dict[str, Any] = {"intent": "commerce",
                               "trace": _trace(s, "route:fast=commerce")}
        if s.get("customer_id"):
            equipment = identity.customer_equipment(s["customer_id"])
            if len(equipment) > 1:
                named = _match_equipment(message, equipment)
                if named is not None:
                    out["_named_model"] = named.model_id
        return out

    # Shortcut: the first message of a session is always a new issue unless it
    # is bare pleasantries.
    if not issues and len(message.split()) > 3:
        return {"intent": "new_issue", "trace": _trace(s, "route:fast=first-message")}

    # Deterministic machine-switch detection. When the customer names one of
    # their machines and it is NOT the one the active thread is about, that is
    # the strongest signal in the whole turn — far too important to leave to a
    # 1.5B classifier, which reliably mislabels "actually, my bike is also
    # playing up" as an identity statement. Getting this wrong applies one
    # machine's manual to another machine's fault, which is the single worst
    # thing this system can do quietly.
    if s.get("customer_id"):
        equipment = identity.customer_equipment(s["customer_id"])
        if len(equipment) > 1:
            named = _match_equipment(message, equipment)
            if named is not None and (active is None or active.model_id != named.model_id):
                existing = next((i for i in open_issues
                                 if i.model_id == named.model_id), None)
                if existing is not None:
                    return {"intent": "switch_issue", "_target_seq": existing.seq,
                            "trace": _trace(s, f"route:fast=machine-switch->#{existing.seq}")}

                # The customer may be referring back to a thread that has since
                # been escalated or resolved. Silently opening a third thread on
                # the same machine hides that from them; switch_issue reports
                # the real state instead.
                closed = next((i for i in issues
                               if i.model_id == named.model_id and i.is_terminal), None)
                if closed is not None:
                    return {"intent": "switch_issue", "_target_seq": closed.seq,
                            "trace": _trace(s, f"route:fast=closed-thread->#{closed.seq}")}

                return {"intent": "new_issue", "_named_model": named.model_id,
                        "trace": _trace(s, f"route:fast=new-machine->{named.model_id}")}

    result = complete_json(
        system=prompts.ROUTER_SYSTEM,
        user=prompts.router_user(message, open_issues, active.last_instruction()
                                 if active else None),
        node="router",
        schema=prompts.ROUTER_SCHEMA,
        session_id=s["session_id"],
        # Falling back to continue_issue is the safest wrong answer: it keeps
        # working the thread already in progress rather than spawning a
        # duplicate one or dropping context.
        fallback={"intent": "continue_issue" if active else "new_issue",
                  "confidence": 0.0},
        model=settings.llm_router_model,
        max_tokens=120,
    )

    intent = result.get("intent", "continue_issue")
    out: dict[str, Any] = {
        "intent": intent,
        "trace": _trace(s, f"route:llm={intent}@{result.get('confidence', 0):.2f}"),
    }
    if result.get("issue_title"):
        out["_issue_title"] = result["issue_title"]
    if result.get("target_issue_seq") is not None:
        out["_target_seq"] = result["target_issue_seq"]
    return out


def _active_issue(s: GraphState, issues: list[IssueThread]) -> IssueThread | None:
    active_id = s.get("active_issue_id")
    for i in issues:
        if i.id == active_id:
            return i
    return next((i for i in issues if i.is_open), None)


# ---------------------------------------------------------------------------
# 3. Identity and model identification — the hard gate
# ---------------------------------------------------------------------------

def identify(s: GraphState) -> dict:
    """Resolve the customer and their machine.

    Nothing downstream may run until this succeeds, because every downstream
    step is keyed on model_id. The ladder is tried strongest-evidence-first and
    reports honest confidence; below the threshold we ask rather than assume.
    """
    session_id = s["session_id"]
    found = s.get("_identifiers") or identity.extract_identifiers(s["customer_message"])
    verified = [state.VerifiedModel(**v) for v in s.get("verified_models", [])]
    customer_id = s.get("customer_id")

    # --- serial number: identifies the machine directly ------------------
    if found.get("serial"):
        candidate = identity.identify_by_serial(found["serial"])
        if candidate:
            state.record_verified_model(session_id, candidate)
            if candidate.order_id and not customer_id:
                cust = identity.find_customer(order_id=candidate.order_id)
                if cust:
                    customer_id = str(cust["id"])
                    state.set_session_customer(session_id, customer_id)
            return _identified(
                s, session_id, customer_id,
                ack=f"Thank you — that serial number is a {candidate.name}. "
                    f"What is it doing?",
                trace_note=f"identify:serial->{candidate.model_id}",
            )
        return {
            "reply": "I could not find that serial number in our records. It is on "
                     "a plate on the frame — usually underneath, near the front. "
                     "Could you double-check it, or give me the email you ordered with?",
            "trace": _trace(s, "identify:serial-miss"),
        }

    # --- customer lookup, then their equipment ---------------------------
    if not customer_id and (found.get("email") or found.get("order_id")
                            or found.get("phone")):
        cust = identity.find_customer(
            email=found.get("email"), phone=found.get("phone"),
            order_id=found.get("order_id"),
        )
        if cust:
            customer_id = str(cust["id"])
            state.set_session_customer(session_id, customer_id)
        else:
            return {
                "reply": "I could not find an account with those details. Could you "
                         "check the spelling, or give me the serial number from the "
                         "plate on the machine instead?",
                "trace": _trace(s, "identify:customer-miss"),
            }

    if customer_id:
        equipment = identity.customer_equipment(customer_id)

        if len(equipment) == 1:
            candidate = equipment[0]
            state.record_verified_model(session_id, candidate)
            return _identified(
                s, session_id, customer_id,
                ack=f"Found it — you have a {candidate.name}. What is it doing?",
                trace_note=f"identify:order->{candidate.model_id}",
            )

        if len(equipment) > 1:
            # The customer may already be answering the question we asked last
            # turn. Without this the agent re-asks forever: it offers a list,
            # the customer picks from it, and the pick is never consumed.
            picked = _match_equipment(s["customer_message"], equipment)
            if picked is not None:
                picked.method = "customer_confirmed"
                picked.confidence = identity.CONF_CONFIRMED
                state.record_verified_model(session_id, picked)
                return _identified(
                    s, session_id, customer_id,
                    ack=f"Right — the {picked.name}. What is it doing?",
                    trace_note=f"identify:picked->{picked.model_id}",
                )

            # Multiple machines is not a failure — it is the normal case for a
            # repeat customer, and asking which one also disambiguates issue
            # threads later in a multi-issue session.
            options = "\n".join(
                f"  {n}. {c.name} (bought {c.purchased_at})"
                for n, c in enumerate(equipment, start=1)
            )
            return {
                "customer_id": customer_id,
                "reply": prompts.MULTI_MACHINE_ASK.format(n=len(equipment),
                                                          options=options),
                "trace": _trace(s, f"identify:ambiguous-{len(equipment)}"),
                "_equipment_options": [c.model_id for c in equipment],
            }

        return {
            "customer_id": customer_id,
            "reply": "I found your account but no equipment registered to it. Could "
                     "you give me the serial number from the plate on the machine?",
            "trace": _trace(s, "identify:no-equipment"),
        }

    # --- nothing to go on yet --------------------------------------------
    if verified:
        return {"trace": _trace(s, "identify:already-verified")}

    return {
        "reply": prompts.IDENTITY_ASK,
        "trace": _trace(s, "identify:ask"),
    }


_COMMERCE_MARKERS = (
    "order a", "order the", "order me", "buy a", "buy the", "purchase",
    "replacement", "new one", "how much", "what does it cost", "price",
    "send me a", "ship me", "get a new", "need a new", "order one",
)


def _looks_like_commerce(message: str) -> bool:
    low = message.lower()
    return any(m in low for m in _COMMERCE_MARKERS)


def _match_issue_by_words(message: str, issues: list[IssueThread]) -> IssueThread | None:
    """Find the issue a customer means when they refer back to it in words.

    "Back to the treadmill problem" has to land on the treadmill thread even
    when the router did not supply a sequence number.
    """
    import re

    text = message.lower()
    scored: list[tuple[int, IssueThread]] = []
    for issue in issues:
        tokens = {t for t in re.split(r"[^a-z0-9]+", issue.title.lower())
                  if len(t) > 3 and t not in _STOPWORDS}
        hits = sum(1 for t in tokens if t in text)
        if hits:
            scored.append((hits, issue))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    if len(scored) == 1 or scored[0][0] > scored[1][0]:
        return scored[0][1]
    return None


_STOPWORDS = {
    "issue", "problem", "fault", "with", "that", "this", "keeps", "when",
    "wont", "will", "does", "have", "from", "your", "device", "machine",
}


def _match_equipment(message: str, equipment: list) -> Any | None:
    """Resolve which machine the customer picked from the list we offered.

    Handles the three ways people actually answer: the number we printed ("2"),
    an ordinal ("the second one"), or a word from the product name ("the
    Velodrome", "my bike", "the rower"). An ambiguous answer returns None so the
    agent asks again rather than guessing — picking the wrong machine here
    poisons every step that follows.
    """
    import re

    text = message.lower().strip()

    # Ordinal and bare-number answers are only meaningful in a SHORT reply to
    # the question we just asked ("2", "the second one"). Honouring them inside
    # a long sentence produces absurd matches — "I held it for 20 seconds"
    # contains "second" and would silently select the customer's second machine.
    if len(text.split()) <= 8:
        m = re.match(r"^\s*(?:number\s*|#)?([1-9])\b", text)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(equipment):
                return equipment[idx]

        for word, idx in (("first", 0), ("second", 1), ("third", 2)):
            # Word boundaries, so "seconds" does not match "second".
            if re.search(rf"\b{word}\b", text) and idx < len(equipment):
                return equipment[idx]

    scored: list[tuple[int, Any]] = []
    for cand in equipment:
        tokens = {t for t in re.split(r"[^a-z0-9]+", cand.name.lower()) if len(t) > 2}
        tokens.add(cand.category_id)
        hits = sum(1 for t in tokens if t in text)
        if hits:
            scored.append((hits, cand))

    if len(scored) == 1:
        return scored[0][1]
    if len(scored) > 1:
        scored.sort(key=lambda x: x[0], reverse=True)
        # Accept only a clear winner; a tie means we genuinely do not know.
        if scored[0][0] > scored[1][0]:
            return scored[0][1]
    return None


def needs_identification(s: GraphState) -> bool:
    """The gate itself."""
    verified = s.get("verified_models") or []
    if not verified:
        return True
    return max(v.get("confidence", 0.0) for v in verified) < settings.model_id_confidence_threshold


# ---------------------------------------------------------------------------
# 4. Issue intake
# ---------------------------------------------------------------------------

def open_issue(s: GraphState) -> dict:
    """Turn a described problem into a tracked issue thread."""
    session_id = s["session_id"]
    message = s["customer_message"]
    verified = [state.VerifiedModel(**v) for v in s.get("verified_models", [])]

    summary = complete_json(
        system=prompts.SUMMARISE_SYSTEM,
        user=f"Customer wrote: {message!r}",
        node="summarise",
        schema=prompts.SUMMARISE_SCHEMA,
        session_id=session_id,
        fallback={"title": message[:60], "symptom_summary": message[:300],
                  "search_query": message[:120], "error_code": None},
        max_tokens=200,
    )

    # A session can span several machines, so the new issue must be attached to
    # the machine THIS complaint is about — not simply to the first one we
    # happened to verify. Without this, a bike fault raised mid-session inherits
    # the treadmill's model_id and every subsequent retrieval is scoped to the
    # wrong manual.
    model_id = s.get("_named_model") or (verified[0].model_id if verified else None)
    if s.get("customer_id"):
        equipment = identity.customer_equipment(s["customer_id"])
        named = None
        if s.get("_named_model"):
            # The router already resolved which machine this is about. Record it
            # as verified anyway — otherwise a machine identified purely by name
            # is missing from the handoff packet and has no order_id for the
            # warranty engine to work from.
            named = next((c for c in equipment
                          if c.model_id == s["_named_model"]), None)
        elif len(equipment) > 1:
            named = _match_equipment(message, equipment)

        if named is not None:
            model_id = named.model_id
            named.method = "customer_confirmed"
            named.confidence = identity.CONF_CONFIRMED
            state.record_verified_model(session_id, named)

    issue = state.create_issue(
        session_id,
        title=s.get("_issue_title") or summary["title"],
        symptom_summary=summary["symptom_summary"],
        model_id=model_id,
    )
    issue.status = "diagnosing"
    state.save_issue(issue)

    issues = state.load_issues(session_id)
    return {
        "issues": [i.model_dump() for i in issues],
        "active_issue_id": issue.id,
        "_search_query": summary.get("search_query") or summary["symptom_summary"],
        "_error_code": summary.get("error_code"),
        "trace": _trace(s, f"open_issue:{issue.seq}:{issue.title}"),
    }


def switch_issue(s: GraphState) -> dict:
    """Resume a previously suspended thread.

    Nothing is reconstructed or re-derived: the thread carries its own steps,
    its own ruled-out list and its own model_id, so resuming it is a pointer
    move. That is the whole reason issues are modelled as objects.
    """
    issues = [IssueThread(**i) for i in s.get("issues", [])]
    target_seq = s.get("_target_seq")

    target = None
    if target_seq is not None:
        target = next((i for i in issues if i.seq == target_seq), None)
    if target is None:
        target = _match_issue_by_words(s["customer_message"], issues)
    if target is None:
        target = next((i for i in issues
                       if i.is_open and i.id != s.get("active_issue_id")), None)
    if target is None:
        # There is nothing to switch to. The router most often gets here on a
        # message like "actually, my bike is also playing up" — which is a NEW
        # issue wearing the grammar of a switch. Opening it is far better than
        # replying "I did not follow that" to a perfectly clear sentence.
        return {"_fallthrough_to_new_issue": True,
                "trace": _trace(s, "switch_issue:none-available->new_issue")}

    # A thread the customer asks to return to may already be finished. Saying so
    # is the honest answer; quietly reopening it, or starting a third thread on
    # the same machine, leaves them thinking work is happening that is not.
    if target.is_terminal:
        if target.status == "escalated":
            recap = (f"Your {target.title} is already with one of my colleagues — "
                     f"they have the full history and will pick it up from there. "
                     f"Is there something else I can look at meanwhile?")
        elif target.status == "resolved":
            recap = (f"We had your {target.title} sorted"
                     + (f" — {target.resolution_note}" if target.resolution_note else "")
                     + ". Has it come back?")
        else:
            recap = (f"We were not able to get to the bottom of your "
                     f"{target.title}. Shall I pass it to a colleague?")
        return {
            "active_issue_id": target.id,
            "reply": recap,
            "trace": _trace(s, f"switch_issue:->{target.seq}({target.status})"),
        }

    recap = (f"Back to your {target.title}. "
             + (f"Last time we established: {target.ruled_out[-1]}. "
                if target.ruled_out else "")
             + "Let us pick up where we left off.")

    # Persist the switch. Returning active_issue_id alone only holds for this
    # turn; the next message reloads the active thread from the database.
    state.touch_issue(target.id)

    return {
        "active_issue_id": target.id,
        "reply": recap,
        "trace": _trace(s, f"switch_issue:->{target.seq}"),
        "_resume": True,
    }


# ---------------------------------------------------------------------------
# 5. Diagnosis
# ---------------------------------------------------------------------------

def diagnose(s: GraphState) -> dict:
    """One iteration of the diagnostic loop for the active issue."""
    session_id = s["session_id"]
    message = s["customer_message"]
    issues = [IssueThread(**i) for i in s.get("issues", [])]
    issue = _active_issue(s, issues)

    if issue is None:
        return {"trace": _trace(s, "diagnose:no-active-issue")}

    if not issue.model_id:
        verified = s.get("verified_models") or []
        if verified:
            issue.model_id = verified[0]["model_id"]
        else:
            return {"trace": _trace(s, "diagnose:no-model")}

    model = identity.get_model(issue.model_id)
    if model is None:
        return {"trace": _trace(s, "diagnose:unknown-model")}

    # --- symbolic first: an error code is a keyed lookup ------------------
    code = s.get("_error_code") or _find_error_code(message)
    if code:
        entry = knowledge.lookup_error_code(model_id=issue.model_id, code=code)
        if entry:
            return _record_step(
                s, issue,
                message=(f"{entry['code']} means: {entry['meaning']}\n\n"
                         f"{entry['first_actions']}"),
                interpretation=f"Console reported {entry['code']}.",
                citations=[{"section": "error_codes", "code": entry["code"],
                            "pages": f"p.{entry['source_page']}"
                                     if entry.get("source_page") else None}],
                status="diagnosing",
                trace_note=f"diagnose:error-code={entry['code']}",
            )

    # --- retrieval --------------------------------------------------------
    query_text = s.get("_search_query") or issue.symptom_summary or message
    retrieval = knowledge.search_manual(
        model_id=issue.model_id, query_text=query_text, section="troubleshooting",
    )
    # Widen to the whole manual if the troubleshooting section had nothing.
    if not retrieval.chunks:
        retrieval = knowledge.search_manual(
            model_id=issue.model_id, query_text=query_text,
        )

    if retrieval.coverage_status == "unbacked":
        return {
            "escalate": {"reason": "no_coverage",
                         "detail": retrieval.coverage_note or "No manual indexed."},
            "active_issue_id": issue.id,
            "trace": _trace(s, "diagnose:unbacked"),
        }

    if not retrieval.chunks or not retrieval.is_confident:
        return {
            "escalate": {
                "reason": "low_retrieval_confidence",
                "detail": f"Best score {retrieval.best_vector_score:.3f} < "
                          f"{settings.retrieval_min_score} for query {query_text!r}.",
            },
            "active_issue_id": issue.id,
            "trace": _trace(s, f"diagnose:low-confidence={retrieval.best_vector_score:.3f}"),
        }

    step_n = issue.step_budget_used + 1
    result = complete_json(
        system=prompts.DIAGNOSE_SYSTEM.format(
            safety=safety.category_safety_preamble(model["safety_class"])
        ),
        user=prompts.diagnose_user(
            model_name=model["name"],
            symptom=issue.symptom_summary or issue.title,
            history=issue.history_digest(),
            customer_message=message,
            context=retrieval.as_context(),
            step_n=step_n,
            budget=settings.diagnostic_step_budget,
        ),
        node="diagnose",
        schema=prompts.DIAGNOSE_SCHEMA,
        session_id=session_id,
        issue_id=issue.id,
        fallback={"message": "", "status": "diagnosing", "needs_escalation": True},
        max_tokens=400,
    )

    if not result.get("_llm_ok"):
        issue.tool_failures += 1
        state.save_issue(issue)
        out = {
            "issues": [i.model_dump() for i in state.load_issues(session_id)],
            "active_issue_id": issue.id,
            "trace": _trace(s, f"diagnose:llm-failed({issue.tool_failures})"),
        }
        # Escalate only once the configured budget is actually spent. A single
        # bad generation is common with a small model and usually succeeds on
        # the next turn; handing every one of them to a human would put the
        # escalation rate through the floor for no good reason.
        if issue.tool_failures >= settings.max_tool_failures_per_issue:
            out["escalate"] = {
                "reason": "tool_failures",
                "detail": f"{issue.tool_failures} consecutive diagnostic failures; "
                          f"last error: {result.get('_error')}",
            }
        else:
            out["reply"] = ("Sorry, I lost my train of thought there. Could you "
                            "describe what the machine is doing once more?")
        return out

    if result.get("needs_escalation"):
        return {
            "escalate": {"reason": "unresolvable",
                         "detail": "The diagnostic model reported it has no further checks."},
            "active_issue_id": issue.id,
            "trace": _trace(s, "diagnose:model-gave-up"),
        }

    citations = [c.citation() for c in retrieval.chunks[:3]]
    status_map = {"diagnosing": "diagnosing", "resolved": "resolved",
                  "needs_part": "awaiting_part", "unresolvable": "unresolvable"}
    new_status = status_map.get(result.get("status", "diagnosing"), "diagnosing")

    proposed = _clean_reply(result["message"])

    # A repeated instruction means the loop has stopped making progress. The
    # model will happily re-ask the same check indefinitely — it reads as
    # locally reasonable every time — so the stall is detected here rather than
    # trusted to the prompt. Asking a customer to do the same thing a third time
    # is the fastest way to lose them.
    if new_status == "diagnosing" and _is_repeat(proposed, issue):
        log.info("issue %s: repeated diagnostic step at %d; escalating",
                 issue.id, step_n)
        return {
            "issues": [i.model_dump() for i in state.load_issues(session_id)],
            "escalate": {
                "reason": "no_progress",
                "detail": f"Diagnostic loop repeated a previous step at step "
                          f"{step_n}: {proposed[:120]!r}",
            },
            "active_issue_id": issue.id,
            "trace": _trace(s, f"diagnose:repeat-detected@{step_n}"),
        }

    # Deterministic guard on the transition into the parts path. Small models
    # reach for "it needs a new part" on the first turn, before the customer has
    # checked anything — which is how you sell someone a $329 display when the
    # barrel connector was loose. Selling a part is a decision the conversation
    # has to earn, so it requires at least MIN_STEPS_BEFORE_PART completed steps.
    if new_status == "awaiting_part" and issue.step_budget_used < MIN_STEPS_BEFORE_PART:
        log.info("issue %s: downgrading premature needs_part at step %d",
                 issue.id, step_n)
        new_status = "diagnosing"
        result.pop("suspected_fault", None)

    out = _record_step(
        s, issue,
        message=proposed,
        interpretation=result.get("interpretation") or "",
        ruled_out=result.get("ruled_out") or [],
        citations=citations,
        status=new_status,
        trace_note=f"diagnose:step{step_n}->{new_status}",
    )

    # A suspected failed part goes to the catalog for resolution — the model
    # names the symptom, the database names the part.
    if new_status == "awaiting_part" and result.get("suspected_fault"):
        out["_suspected_fault"] = result["suspected_fault"]
    return out


def _record_step(s: GraphState, issue: IssueThread, *, message: str,
                 interpretation: str = "", ruled_out: list[str] | None = None,
                 citations: list[dict] | None = None, status: str = "diagnosing",
                 trace_note: str = "") -> dict:
    """Append a diagnostic step and persist the thread."""
    if issue.steps and issue.steps[-1].customer_response is None:
        issue.steps[-1].customer_response = s["customer_message"]
        issue.steps[-1].interpretation = interpretation

    issue.steps.append(DiagnosticStep(
        n=issue.step_budget_used + 1,
        instruction=message,
        citations=citations or [],
        ruled_out=ruled_out or [],
    ))
    issue.step_budget_used += 1
    issue.ruled_out.extend(r for r in (ruled_out or []) if r not in issue.ruled_out)
    for c in (citations or []):
        if c not in issue.citations:
            issue.citations.append(c)
    issue.status = status
    if status == "resolved":
        issue.resolution_note = interpretation or "Resolved during guided diagnosis."
    state.save_issue(issue)

    return {
        "issues": [i.model_dump() for i in state.load_issues(s["session_id"])],
        "active_issue_id": issue.id,
        "reply": message,
        "citations": citations or [],
        "trace": _trace(s, trace_note),
    }


_REPLY_PREFIX_RE = None


def _clean_reply(message: str) -> str:
    """Strip label prefixes the model prepends despite being told not to.

    Post-processing rather than more prompt text: a 3B model reaches for
    "Ask - " and "Step 2:" scaffolding no matter how the instruction is worded,
    and a two-line regex is both cheaper and more reliable than another attempt
    at persuasion. Truncated trailing fragments (the maxLength cap biting
    mid-sentence) are dropped for the same reason.
    """
    import re

    global _REPLY_PREFIX_RE
    if _REPLY_PREFIX_RE is None:
        _REPLY_PREFIX_RE = re.compile(
            r"^\s*(?:ask|next|step\s*\d*|diagnostic\s+step\s*\d*|instruction)"
            r"\s*[-:–—]\s*",
            re.IGNORECASE,
        )

    text = _REPLY_PREFIX_RE.sub("", message.strip())

    # If the cap cut the last sentence mid-flight, drop the fragment rather than
    # showing the customer half a thought.
    if text and text[-1] not in ".!?\"')":
        cut = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
        if cut > 40:
            text = text[:cut + 1]

    return text.strip() or message.strip()


def _find_error_code(text: str) -> str | None:
    import re
    m = re.search(r"\b(?:error\s+)?([A-Z]{1,2}\s?-?\s?\d{1,2})\b", text, re.IGNORECASE)
    if not m:
        return None
    candidate = m.group(1).upper().replace(" ", "").replace("-", "")
    # Two characters minimum, and it must look like letter+digit.
    return candidate if re.fullmatch(r"[A-Z]{1,2}\d{1,2}", candidate) else None


# Words that mean the customer is describing a fault, not just answering "which
# machine". Used to decide whether identification should flow straight on into
# diagnosis instead of asking a question they have already answered.
_SYMPTOM_MARKERS = (
    "not work", "doesn't", "does not", "wont", "won't", "will not", "cannot",
    "can't", "broken", "blank", "stuck", "noise", "noisy", "squeak", "grind",
    "slip", "loose", "error", "code", "flash", "dead", "stop", "stops",
    "fault", "problem", "issue", "wrong", "leak", "wobble", "rattle", "shake",
    "frayed", "worn", "cracked", "smell", "hot", "slow", "jerk", "drift",
)


def _looks_like_symptom(message: str) -> bool:
    low = message.lower()
    return any(m in low for m in _SYMPTOM_MARKERS)


def _identified(s: GraphState, session_id: str, customer_id: str | None, *,
                ack: str, trace_note: str) -> dict:
    """Return from a successful identification.

    If the very message that identified the machine also described the fault,
    suppress the acknowledgement and let the graph continue into open_issue.
    Asking "what is it doing?" straight after the customer told you what it is
    doing is the single most irritating thing a support bot does, and it costs a
    round trip that on this hardware is measured in tens of seconds.
    """
    out = {
        "customer_id": customer_id,
        "verified_models": [v.model_dump() for v in
                            state.load_verified_models(session_id)],
        "trace": _trace(s, trace_note),
    }
    # Continue in the same turn when the message that identified the machine
    # also said what the customer wants — whether that is a fault to diagnose or
    # a part to buy. Only fall back to the acknowledgement when we genuinely do
    # not yet know what they need.
    if _looks_like_symptom(s["customer_message"]) or s.get("intent") == "commerce":
        out["_identified_now"] = True
    else:
        out["reply"] = ack
    return out


def _stem(word: str) -> str:
    """Crude suffix stripping. Enough to make "hold" and "holding" the same token."""
    for suffix in ("ings", "ing", "ers", "er", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _is_repeat(proposed: str, issue: IssueThread) -> bool:
    """Has this instruction already been given on this thread?

    Containment rather than Jaccard, over stemmed content words. The model does
    not repeat verbatim — it re-asks the same check with extra qualifiers
    ("try holding the power button again, making sure..."), which inflates the
    union and hides the repeat from a symmetric measure. Containment asks the
    question that actually matters: is the new instruction essentially a subset
    of one we already gave?
    """
    import re

    def sig(text: str) -> set[str]:
        return {_stem(w) for w in re.split(r"[^a-z0-9]+", text.lower())
                if len(w) > 3 and w not in _STOPWORDS}

    new_sig = sig(proposed)
    if len(new_sig) < 3:
        return False

    for step in issue.steps:
        old_sig = sig(step.instruction)
        if len(old_sig) < 3:
            continue
        containment = len(new_sig & old_sig) / min(len(new_sig), len(old_sig))
        if containment >= 0.65:
            return True
    return False

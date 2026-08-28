"""Commerce and escalation nodes.

Nothing in this file lets a model decide anything that costs money. The nodes
here look parts up, ask the policy engine for a verdict, present the exact
figures, wait for a real yes, and then place the order against a hash of what
was shown. The LLM's only contribution to the whole path is prose.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..config import settings
from ..db import audit, execute, query_one
from ..policy import escalation, safety
from ..tools import catalog, commerce, identity
from . import prompts, state
from .llm import complete_json
from .state import GraphState, IssueThread

log = logging.getLogger(__name__)


def _trace(s: GraphState, note: str) -> list[str]:
    return [*s.get("trace", []), note]


def _active(s: GraphState) -> IssueThread | None:
    issues = [IssueThread(**i) for i in s.get("issues", [])]
    active_id = s.get("active_issue_id")
    for i in issues:
        if i.id == active_id:
            return i
    return next((i for i in issues if i.is_open), None)


# ---------------------------------------------------------------------------
# Part selection
# ---------------------------------------------------------------------------

def select_part(s: GraphState) -> dict:
    """Turn a diagnosed fault into a real, orderable part.

    The model supplies a description of what failed. The catalog supplies the
    part number and the price. Those two facts never swap sources.
    """
    issue = _active(s)
    if issue is None or not issue.model_id:
        return {"trace": _trace(s, "select_part:no-issue")}

    fault = (s.get("_suspected_fault") or issue.symptom_summary
             or issue.title)

    # A customer who asks for a named part outranks the diagnosed fault. The
    # thread's symptom is the right default when the agent reached the part by
    # troubleshooting, but "order me a new pedal set" is not a symptom — and
    # answering it with whatever the thread was about is how you ship someone a
    # display they never asked for.
    attempts = [fault, issue.title]
    if s.get("intent") == "commerce":
        attempts.insert(0, s["customer_message"])

    candidates: list = []
    for attempt in attempts:
        candidates = catalog.find_parts_for_symptom(
            model_id=issue.model_id, symptom=attempt,
        )
        if candidates:
            break

    if not candidates:
        return {
            "escalate": {
                "reason": "no_part_match",
                "detail": f"No catalog part matches the diagnosed fault {fault!r} "
                          f"for {issue.model_id}.",
            },
            "trace": _trace(s, "select_part:no-match"),
        }

    part = candidates[0]
    model = identity.get_model(issue.model_id)
    category_safety = model["safety_class"] if model else "standard"

    # A part the customer must not fit themselves ends the self-service path,
    # whatever the conversation has been leading towards.
    verdict = safety.screen_part_for_self_service(part, category_safety)
    if verdict.blocks_troubleshooting:
        issue.candidate_part = part["part_number"]
        state.save_issue(issue)
        return {
            "issues": [i.model_dump() for i in state.load_issues(s["session_id"])],
            "reply": verdict.message,
            "escalate": {
                "reason": "restricted_part",
                "detail": f"{part['part_number']} is technician-only "
                          f"({', '.join(verdict.triggered_by)}).",
            },
            "trace": _trace(s, f"select_part:restricted={part['part_number']}"),
        }

    issue.candidate_part = part["part_number"]
    issue.status = "awaiting_part"
    state.save_issue(issue)

    return {
        "issues": [i.model_dump() for i in state.load_issues(s["session_id"])],
        "_selected_part": part["part_number"],
        "trace": _trace(s, f"select_part:{part['part_number']}"),
    }


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------

def quote_part(s: GraphState) -> dict:
    """Price the part and run the warranty check. Takes no payment."""
    session_id = s["session_id"]
    issue = _active(s)
    if issue is None:
        return {"trace": _trace(s, "quote:no-issue")}

    part_number = s.get("_selected_part") or issue.candidate_part
    if not part_number:
        return {"trace": _trace(s, "quote:no-part")}

    # The order id is what warranty coverage attaches to.
    order_id = None
    for v in s.get("verified_models", []):
        if v.get("model_id") == issue.model_id and v.get("order_id"):
            order_id = v["order_id"]
            break

    try:
        quote = commerce.create_quote(
            session_id=session_id, part_number=part_number,
            model_id=issue.model_id, order_id=order_id, issue_id=issue.id,
        )
    except commerce.CommerceError as exc:
        issue.tool_failures += 1
        state.save_issue(issue)
        return {
            "issues": [i.model_dump() for i in state.load_issues(session_id)],
            "reply": str(exc),
            "trace": _trace(s, f"quote:failed={exc}"),
        }

    # A high-value order needs a human, and that check happens before the
    # customer is invited to say yes rather than after.
    check = escalation.evaluate(quote_total_cents=quote.total_cents)
    if check.escalate:
        return {
            "reply": check.customer_message,
            "escalate": {"reason": check.reason, "detail": check.detail},
            "trace": _trace(s, f"quote:high-value={quote.total_cents}"),
        }

    issue.quote_id = quote.id
    state.save_issue(issue)

    summary = quote.customer_summary()
    if quote.covered:
        ask = "\n\nShall I get that sent out to you?"
    else:
        ask = "\n\nWould you like me to order it?"

    return {
        "issues": [i.model_dump() for i in state.load_issues(session_id)],
        "pending_confirmation": {
            "quote_id": quote.id,
            "issue_id": issue.id,
            "part_number": quote.part_number,
            "total_cents": quote.total_cents,
            "covered": quote.covered,
            "confirmation_hash": quote.confirmation_hash,
            "summary_shown": summary,
            "awaiting": "confirm",
        },
        "reply": summary + ask,
        "trace": _trace(s, f"quote:{quote.id}:{quote.total_cents}c"
                           f":{'covered' if quote.covered else 'chargeable'}"),
    }


# ---------------------------------------------------------------------------
# Confirmation, payment, order
# ---------------------------------------------------------------------------

NEGATIVE = ("no", "nope", "not now", "cancel", "don't", "do not", "hold off",
            "leave it", "not yet", "maybe later")


def handle_confirmation(s: GraphState) -> dict:
    """Process the customer's yes or no on a pending quote."""
    session_id = s["session_id"]
    pending = s.get("pending_confirmation")
    if not pending:
        return {"trace": _trace(s, "confirm:nothing-pending")}

    message = s["customer_message"].strip().lower()

    if any(w in message for w in NEGATIVE):
        execute("UPDATE quotes SET status = 'cancelled' WHERE id = %s",
                (pending["quote_id"],))
        audit("quote_declined", actor="customer", session_id=session_id,
              payload={"quote_id": pending["quote_id"]})
        return {
            "pending_confirmation": None,
            "reply": "No problem, I have not ordered anything. Is there anything "
                     "else I can help with?",
            "trace": _trace(s, "confirm:declined"),
        }

    try:
        commerce.confirm_quote(quote_id=pending["quote_id"], session_id=session_id)
    except commerce.CommerceError as exc:
        return {"pending_confirmation": None, "reply": str(exc),
                "trace": _trace(s, f"confirm:failed={exc}")}

    # Warranty-covered parts need no payment, so they go straight to order.
    if pending["covered"]:
        return _place(s, pending, payment_id=None)

    # Chargeable parts need a card. In production the widget opens the PSP's
    # hosted fields and posts back a token; the demo path supplies a test token
    # so the flow can be exercised end to end.
    return {
        "pending_confirmation": {**pending, "awaiting": "payment"},
        "reply": (f"To order that I will need a payment of "
                  f"${pending['total_cents'] / 100:.2f}. "
                  f"I will bring up a secure card form for you now — your card "
                  f"details go straight to our payment provider and are never "
                  f"visible to me."),
        "_request_payment": True,
        "trace": _trace(s, "confirm:awaiting-payment"),
    }


def take_payment(s: GraphState, card_token: str) -> dict:
    """Charge the tokenized card, then place the order."""
    session_id = s["session_id"]
    pending = s.get("pending_confirmation")
    if not pending or pending.get("awaiting") != "payment":
        return {"reply": "There is nothing awaiting payment right now.",
                "trace": _trace(s, "payment:nothing-pending")}

    try:
        payment = commerce.collect_payment(
            quote_id=pending["quote_id"], session_id=session_id,
            card_token=card_token,
        )
    except commerce.CommerceError as exc:
        # A decline is a normal outcome, not a system failure. The quote stays
        # alive — and `_request_payment` has to stay set, or the widget takes
        # the card form away at exactly the moment we have told the customer to
        # try another card.
        quote_alive = query_one(
            "SELECT status FROM quotes WHERE id = %s", (pending["quote_id"],)
        )
        retryable = bool(quote_alive and quote_alive["status"] == "confirmed")
        return {
            "reply": str(exc),
            "pending_confirmation": pending if retryable else None,
            "_request_payment": retryable,
            "trace": _trace(s, f"payment:declined={exc}"),
        }

    return _place(s, pending, payment_id=payment["payment_id"])


def _place(s: GraphState, pending: dict, *, payment_id: str | None) -> dict:
    session_id = s["session_id"]
    customer_id = s.get("customer_id")
    if not customer_id:
        return {"reply": "I need to confirm your account before I can ship a part.",
                "trace": _trace(s, "order:no-customer")}

    try:
        order = commerce.place_order(
            quote_id=pending["quote_id"], session_id=session_id,
            customer_id=customer_id,
            confirmation_hash=pending["confirmation_hash"],
            issue_id=pending.get("issue_id"), payment_id=payment_id,
        )
    except commerce.CommerceError as exc:
        return {"pending_confirmation": None, "reply": str(exc),
                "escalate": {"reason": "order_failed", "detail": str(exc)},
                "trace": _trace(s, f"order:failed={exc}")}

    # The issue is now waiting on a physical part; that is a resolution of the
    # diagnostic thread, not an open loop.
    issues = state.load_issues(session_id)
    for issue in issues:
        if issue.id == pending.get("issue_id"):
            issue.status = "resolved"
            issue.resolution_note = (
                f"Diagnosed to {order['part_number']}; order {order['order_id']} "
                f"placed ({'under warranty' if order['covered'] else 'purchased'})."
            )
            state.save_issue(issue)
            break

    price_line = ("at no charge under your warranty" if order["covered"]
                  else f"for ${order['total_cents'] / 100:.2f}")

    return {
        "issues": [i.model_dump() for i in state.load_issues(session_id)],
        "pending_confirmation": None,
        "reply": (
            f"Done — order {order['order_id']} is placed {price_line}.\n"
            f"{order['part_number']} is on its way to {order['ship_to']}, "
            f"arriving in about {order['eta_days']} working days.\n\n"
            f"Is there anything else I can help with?"
        ),
        "trace": _trace(s, f"order:placed={order['order_id']}"),
    }


# ---------------------------------------------------------------------------
# Escalation and handoff
# ---------------------------------------------------------------------------

def escalate(s: GraphState) -> dict:
    """Build the handoff packet and queue it for a human agent.

    The packet is the whole point of the escalation path. A handoff that makes
    the customer repeat themselves is worse than no handoff, so this captures
    every issue thread with its full diagnostic history — including the threads
    that were already resolved, because the human needs to know what was
    already done and not redo it.
    """
    session_id = s["session_id"]
    reason_info = s.get("escalate") or {"reason": "unspecified"}
    reason = reason_info.get("reason", "unspecified")
    detail = reason_info.get("detail")

    issues = state.load_issues(session_id)
    verified = state.load_verified_models(session_id)

    customer = None
    if s.get("customer_id"):
        row = query_one(
            "SELECT id, email, phone, full_name, address FROM customers WHERE id = %s",
            (s["customer_id"],),
        )
        customer = dict(row) if row else None

    packet = build_handoff_packet(
        session_id=session_id, customer=customer, verified=verified,
        issues=issues, reason=reason, detail=detail,
    )

    row = execute(
        """
        INSERT INTO handoffs (session_id, reason, detail, packet)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (session_id, reason, detail, json.dumps(packet, default=str)),
    )
    handoff_id = str(row["id"])

    execute("UPDATE sessions SET status = 'escalated' WHERE id = %s", (session_id,))

    # Mark only the still-open threads escalated. Resolved threads keep their
    # resolution — a session that escalates on issue 2 has not un-fixed issue 1.
    for issue in issues:
        if issue.is_open:
            issue.status = "escalated"
            state.save_issue(issue)

    audit("escalated", actor="system", session_id=session_id,
          payload={"handoff_id": handoff_id, "reason": reason, "detail": detail})

    message = s.get("reply") or _customer_message_for(reason)

    return {
        "issues": [i.model_dump() for i in state.load_issues(session_id)],
        "reply": message,
        "escalate": {"reason": reason, "detail": detail, "handoff_id": handoff_id},
        "trace": _trace(s, f"escalate:{reason}->{handoff_id}"),
    }


def _customer_message_for(reason: str) -> str:
    check = escalation.evaluate(
        safety_level="critical" if reason == "safety" else "ok",
        customer_requested_human=(reason == "customer_request"),
        coverage_status="unbacked" if reason == "no_coverage" else None,
        restricted_part=(reason == "restricted_part"),
    )
    return check.customer_message or escalation.HANDOFF_MESSAGE


def build_handoff_packet(*, session_id: str, customer: dict | None,
                         verified: list, issues: list,
                         reason: str, detail: str | None) -> dict:
    """Assemble everything a human agent needs to take over cold."""
    from ..db import query

    orders = query(
        """
        SELECT po.id, po.part_number, po.status, po.eta_days, po.created_at,
               q.total_cents, q.covered
          FROM part_orders po JOIN quotes q ON q.id = po.quote_id
         WHERE po.session_id = %s ORDER BY po.created_at
        """,
        (session_id,),
    )
    quotes = query(
        """
        SELECT id, part_number, total_cents, covered, status,
               coverage_decision -> 'reason' AS reason
          FROM quotes WHERE session_id = %s ORDER BY created_at
        """,
        (session_id,),
    )
    transcript = state.recent_messages(session_id, limit=100)

    issue_blocks = []
    for issue in issues:
        issue_blocks.append({
            "seq": issue.seq,
            "title": issue.title,
            "status": issue.status,
            "model_id": issue.model_id,
            "symptom": issue.symptom_summary,
            "steps_taken": [
                {
                    "n": st.n,
                    "asked": st.instruction,
                    "customer_said": st.customer_response,
                    "concluded": st.interpretation,
                }
                for st in issue.steps
            ],
            "ruled_out": issue.ruled_out,
            "citations": issue.citations,
            "candidate_part": issue.candidate_part,
            "resolution": issue.resolution_note,
            "steps_used": f"{issue.step_budget_used}/{settings.diagnostic_step_budget}",
        })

    packet: dict[str, Any] = {
        "session_id": session_id,
        "escalation": {"reason": reason, "detail": detail},
        "customer": {
            "name": customer.get("full_name") if customer else None,
            "email": customer.get("email") if customer else None,
            "phone": customer.get("phone") if customer else None,
            "address": customer.get("address") if customer else None,
        } if customer else None,
        "verified_models": [
            {"model_id": v.model_id, "name": v.name, "method": v.method,
             "confidence": v.confidence, "order_id": v.order_id,
             "serial_number": v.serial_number, "purchased_at": v.purchased_at}
            for v in verified
        ],
        "issues": issue_blocks,
        "quotes": [dict(q) for q in quotes],
        "orders": [dict(o) for o in orders],
        "transcript": [
            {"role": m["role"], "content": m["content"],
             "at": str(m["created_at"])}
            for m in transcript
        ],
    }

    packet["summary"] = _summarise_handoff(packet, session_id)
    return packet


def _summarise_handoff(packet: dict, session_id: str) -> dict:
    """Generate the human-readable handover note.

    Falls back to a deterministic template if the model is unavailable. The
    structured packet above is the real payload; this is a convenience, so it
    must never be the reason a handoff fails.
    """
    lines = []
    for b in packet["issues"]:
        lines.append(
            f"Issue {b['seq']}: {b['title']} [{b['status']}] on "
            f"{b['model_id'] or 'unidentified model'}; "
            f"{len(b['steps_taken'])} steps taken; "
            f"ruled out: {', '.join(b['ruled_out']) or 'nothing yet'}"
        )
    facts = "\n".join(lines) or "No issues were opened."

    result = complete_json(
        system=prompts.HANDOFF_SYSTEM,
        user=(
            f"Escalation reason: {packet['escalation']['reason']} "
            f"({packet['escalation'].get('detail')})\n"
            f"Customer: {(packet.get('customer') or {}).get('name') or 'unidentified'}\n"
            f"Machines: "
            f"{', '.join(v['name'] for v in packet['verified_models']) or 'none confirmed'}\n\n"
            f"Issue threads:\n{facts}\n\n"
            f"Orders placed: {len(packet['orders'])}"
        ),
        node="handoff_summary",
        schema=prompts.HANDOFF_SCHEMA,
        session_id=session_id,
        fallback={
            "summary": facts,
            "recommended_next_action":
                f"Review the {packet['escalation']['reason']} escalation and "
                f"continue from the last diagnostic step.",
        },
        max_tokens=350,
    )
    return {
        "text": result.get("summary", facts),
        "next_action": result.get("recommended_next_action", ""),
        "generated": bool(result.get("_llm_ok")),
    }

"""Commerce: quote, confirm, pay, order.

The whole path is deterministic. The agent's only role is to narrate what the
engine decided and to relay the customer's yes or no. Specifically:

  * the price comes from the catalog, never from the model
  * coverage comes from the policy engine, never from the model
  * `place_order` refuses to run unless the confirmation it is handed hashes to
    the exact figures the customer was shown

That last point is the important one. Without it, an agent that has drifted
mid-conversation can quote $89, then order a $349 part, and every individual
step still looks locally reasonable. The hash makes quote and charge the same
object or no charge happens at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from ..config import settings
from ..db import audit, execute, query_one
from ..policy.warranty import CoverageDecision, check_coverage
from . import catalog

log = logging.getLogger(__name__)

QUOTE_TTL_MINUTES = 30
SHIPPING_CENTS = 1200
TAX_RATE = 0.0825


class CommerceError(RuntimeError):
    """Raised when a commerce precondition fails. Never retried blindly."""


@dataclass
class Quote:
    id: str
    part_number: str
    part_name: str
    quantity: int
    unit_price_cents: int
    shipping_cents: int
    tax_cents: int
    total_cents: int
    covered: bool
    coverage_reason: str
    confirmation_hash: str
    expires_at: str

    def customer_summary(self) -> str:
        """The exact text shown to the customer. Hashed, so it cannot drift."""
        if self.covered:
            return (
                f"{self.part_name} ({self.part_number})\n"
                f"{self.coverage_reason}\n"
                f"Total to pay: $0.00 — shipped free under warranty."
            )
        return (
            f"{self.part_name} ({self.part_number})\n"
            f"{self.coverage_reason}\n"
            f"Part: ${self.unit_price_cents / 100:.2f}"
            f"{f' x{self.quantity}' if self.quantity > 1 else ''}\n"
            f"Shipping: ${self.shipping_cents / 100:.2f}\n"
            f"Tax: ${self.tax_cents / 100:.2f}\n"
            f"Total: ${self.total_cents / 100:.2f}"
        )


def _hash_quote(part_number: str, quantity: int, total_cents: int,
                covered: bool) -> str:
    """Bind a confirmation to exact figures.

    Any drift in part, quantity, price or coverage produces a different hash and
    `place_order` refuses.
    """
    payload = f"{part_number}|{quantity}|{total_cents}|{int(covered)}"
    return hashlib.sha256(payload.encode()).hexdigest()


def create_quote(
    *,
    session_id: str,
    part_number: str,
    model_id: str,
    order_id: str | None,
    issue_id: str | None = None,
    quantity: int = 1,
) -> Quote:
    """Price a part and decide coverage. Takes no payment."""
    ok, reason = catalog.validate_part_for_model(part_number, model_id)
    if not ok:
        raise CommerceError(reason)

    part = catalog.get_part(part_number)
    assert part is not None                      # validate_part_for_model checked

    decision: CoverageDecision = check_coverage(
        part_number=part_number, order_id=order_id,
        session_id=session_id, issue_id=issue_id,
    )

    if decision.covered:
        unit = part["price_cents"]
        shipping = tax = total = 0
    else:
        unit = part["price_cents"]
        subtotal = unit * quantity
        shipping = SHIPPING_CENTS
        tax = int(round((subtotal + shipping) * TAX_RATE))
        total = subtotal + shipping + tax

    confirmation_hash = _hash_quote(part_number, quantity, total, decision.covered)
    expires = datetime.now(timezone.utc) + timedelta(minutes=QUOTE_TTL_MINUTES)

    row = execute(
        """
        INSERT INTO quotes (session_id, issue_id, part_number, quantity,
                            unit_price_cents, shipping_cents, tax_cents,
                            total_cents, coverage_decision, covered,
                            confirmation_hash, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (session_id, issue_id, part_number, quantity, unit, shipping, tax, total,
         json.dumps(decision.to_json(), default=str), decision.covered,
         confirmation_hash, expires),
    )
    quote_id = str(row["id"])

    audit("quote_created", actor="system", session_id=session_id, issue_id=issue_id,
          payload={"quote_id": quote_id, "part_number": part_number,
                   "total_cents": total, "covered": decision.covered})

    return Quote(
        id=quote_id, part_number=part_number, part_name=part["name"],
        quantity=quantity, unit_price_cents=unit, shipping_cents=shipping,
        tax_cents=tax, total_cents=total, covered=decision.covered,
        coverage_reason=decision.reason, confirmation_hash=confirmation_hash,
        expires_at=expires.isoformat(),
    )


def confirm_quote(*, quote_id: str, session_id: str) -> dict:
    """Record the customer's explicit yes.

    A separate step from `place_order` on purpose: confirmation is a fact about
    what the customer said, and it is stored as its own row so the order path
    can verify it rather than trust the conversation.
    """
    quote = _load_quote(quote_id)

    if quote["status"] not in ("pending", "confirmed"):
        raise CommerceError(f"This quote is already {quote['status']}.")
    if _expired(quote):
        execute("UPDATE quotes SET status = 'expired' WHERE id = %s", (quote_id,))
        raise CommerceError("That quote has expired. Let me price it again.")

    execute("UPDATE quotes SET status = 'confirmed' WHERE id = %s", (quote_id,))
    audit("quote_confirmed", actor="customer", session_id=session_id,
          payload={"quote_id": quote_id})
    return {"quote_id": quote_id, "status": "confirmed",
            "total_cents": quote["total_cents"], "covered": quote["covered"]}


def collect_payment(*, quote_id: str, session_id: str, card_token: str) -> dict:
    """Charge a tokenized card via the mock PSP.

    Card details never reach this process. The browser tokenizes directly with
    the PSP and we charge the token, which is how a real hosted-fields
    integration works and keeps the PCI surface at zero.

    The idempotency key is derived from the quote, so a retrying agent loop —
    or a customer double-clicking — cannot produce a second charge.
    """
    quote = _load_quote(quote_id)

    if quote["covered"]:
        raise CommerceError("This part is covered under warranty; no payment is due.")
    if quote["status"] == "paid":
        raise CommerceError("This quote has already been paid.")
    if quote["status"] != "confirmed":
        raise CommerceError("The customer has not confirmed this quote yet.")
    if _expired(quote):
        execute("UPDATE quotes SET status = 'expired' WHERE id = %s", (quote_id,))
        raise CommerceError("That quote has expired. Let me price it again.")

    # The idempotency key covers one *attempt*, not the whole quote.
    #
    # Keying it on the quote alone looks right and is badly wrong: the PSP
    # replays the stored outcome for a key, so once a card is declined, every
    # subsequent attempt on that quote replays the decline — with a different
    # card, forever. The customer can never pay, and the agent keeps politely
    # inviting them to try another card.
    #
    # Counting prior failed attempts keeps a genuine retry of the *same* request
    # idempotent (the count has not moved) while letting a *new* card be a new
    # charge. A captured payment closes the quote, so success cannot be repeated.
    attempts = query_one(
        "SELECT count(*) AS n FROM payments WHERE quote_id = %s AND status <> 'captured'",
        (quote_id,),
    )
    idempotency_key = f"quote-{quote_id}-attempt-{attempts['n']}"

    captured = query_one(
        "SELECT id, status, psp_reference, card_last4 FROM payments "
        "WHERE quote_id = %s AND status = 'captured'",
        (quote_id,),
    )
    if captured:
        return {"payment_id": str(captured["id"]), "status": "captured",
                "reference": captured["psp_reference"], "replayed": True}

    try:
        with httpx.Client(base_url=settings.mock_psp_url, timeout=30.0) as client:
            resp = client.post("/v1/charges", json={
                "token": card_token,
                "amount_cents": quote["total_cents"],
                "idempotency_key": idempotency_key,
                "description": f"FitForge part {quote['part_number']}",
            })
            resp.raise_for_status()
            charge = resp.json()
    except httpx.HTTPError as exc:
        audit("payment_error", actor="system", session_id=session_id,
              payload={"quote_id": quote_id, "error": str(exc)})
        raise CommerceError(
            "I could not reach the payment service. Nothing has been charged."
        ) from exc

    payment_row = execute(
        """
        INSERT INTO payments (quote_id, idempotency_key, amount_cents, status,
                              psp_reference, card_last4)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO UPDATE SET status = EXCLUDED.status
        RETURNING id
        """,
        (quote_id, idempotency_key, quote["total_cents"], charge["status"],
         charge.get("reference"), charge.get("last4")),
    )
    payment_id = str(payment_row["id"])

    audit("payment_attempted", actor="system", session_id=session_id,
          payload={"quote_id": quote_id, "payment_id": payment_id,
                   "status": charge["status"],
                   "decline_reason": charge.get("decline_reason")})

    if charge["status"] != "captured":
        raise CommerceError(
            "That card was declined ("
            + str(charge.get("decline_reason") or "unknown reason").replace("_", " ")
            + "). Nothing has been charged. Would you like to try another card?"
        )

    execute("UPDATE quotes SET status = 'paid' WHERE id = %s", (quote_id,))
    return {"payment_id": payment_id, "status": "captured",
            "reference": charge.get("reference"), "last4": charge.get("last4"),
            "replayed": bool(charge.get("idempotent_replay"))}


def place_order(
    *,
    quote_id: str,
    session_id: str,
    customer_id: str,
    confirmation_hash: str,
    issue_id: str | None = None,
    payment_id: str | None = None,
) -> dict:
    """Place the parts order.

    The hash check is the last gate. If the figures the agent believes it is
    ordering against differ in any way from the figures the customer was shown,
    this refuses — the drift is caught here rather than on the customer's card.
    """
    quote = _load_quote(quote_id)

    if confirmation_hash != quote["confirmation_hash"]:
        # Refuse and record it: this is the signal that the agent's state and
        # the customer's understanding have diverged.
        audit("order_hash_mismatch", actor="system", session_id=session_id,
              issue_id=issue_id,
              payload={"quote_id": quote_id, "expected": quote["confirmation_hash"],
                       "received": confirmation_hash})
        raise CommerceError(
            "The order details do not match what was quoted to the customer. "
            "Nothing has been ordered. Re-confirm the quote before proceeding."
        )

    if quote["status"] == "ordered":
        existing = query_one(
            "SELECT id, eta_days FROM part_orders WHERE quote_id = %s", (quote_id,)
        )
        if existing:
            return {"order_id": existing["id"], "eta_days": existing["eta_days"],
                    "replayed": True}

    if not quote["covered"] and quote["status"] != "paid":
        raise CommerceError("This part is not covered and has not been paid for yet.")
    if quote["covered"] and quote["status"] != "confirmed":
        raise CommerceError("The customer has not confirmed this order yet.")

    customer = query_one(
        "SELECT id, full_name, address FROM customers WHERE id = %s", (customer_id,)
    )
    if customer is None:
        raise CommerceError("No customer record found; cannot ship a part.")

    order_id = f"PO-{datetime.now(timezone.utc):%Y%m}-{uuid.uuid4().hex[:8].upper()}"
    eta = 3 if quote["covered"] else 5

    execute(
        """
        INSERT INTO part_orders (id, session_id, issue_id, customer_id, quote_id,
                                 payment_id, part_number, quantity, ship_to, eta_days)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (order_id, session_id, issue_id, customer_id, quote_id, payment_id,
         quote["part_number"], quote["quantity"],
         json.dumps(customer["address"], default=str), eta),
    )
    execute("UPDATE quotes SET status = 'ordered' WHERE id = %s", (quote_id,))

    audit("order_placed", actor="system", session_id=session_id, issue_id=issue_id,
          payload={"order_id": order_id, "quote_id": quote_id,
                   "part_number": quote["part_number"],
                   "total_cents": quote["total_cents"], "covered": quote["covered"]})

    address = customer["address"] or {}
    return {
        "order_id": order_id,
        "eta_days": eta,
        "part_number": quote["part_number"],
        "total_cents": quote["total_cents"],
        "covered": quote["covered"],
        "ship_to": f"{address.get('line1', '')}, {address.get('city', '')} "
                   f"{address.get('state', '')} {address.get('postal_code', '')}".strip(),
        "replayed": False,
    }


def _load_quote(quote_id: str) -> dict:
    quote = query_one(
        """
        SELECT id, session_id, issue_id, part_number, quantity, total_cents,
               covered, confirmation_hash, status, expires_at
          FROM quotes WHERE id = %s
        """,
        (quote_id,),
    )
    if quote is None:
        raise CommerceError("That quote no longer exists.")
    return dict(quote)


def _expired(quote: dict) -> bool:
    return quote["expires_at"] < datetime.now(timezone.utc)


def tokenize_test_card(card_number: str = "4242424242424242") -> str:
    """Tokenize a card against the mock PSP.

    Used by the demo script and the tests. In the real product this call is made
    by the customer's browser and never by our backend.
    """
    with httpx.Client(base_url=settings.mock_psp_url, timeout=15.0) as client:
        resp = client.post("/v1/tokens", json={
            "card_number": card_number, "exp_month": 12, "exp_year": 2030,
            "cvc": "123", "cardholder": "FitForge Demo",
        })
        resp.raise_for_status()
        return resp.json()["token"]

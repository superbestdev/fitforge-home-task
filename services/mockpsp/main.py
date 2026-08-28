"""Mock payment service provider.

Stands in for Stripe/Adyen/Braintree. It exists so the commerce path is real —
tokenization, idempotency, authorize/capture, declines — without a paid account
and without ever putting card data anywhere near our services or the model.

The flow it models is the one you would actually use in production:

  1. The browser posts card details straight to the PSP (hosted fields) and gets
     back an opaque single-use token. Our backend never sees a PAN.
  2. Our backend charges the *token*, with an idempotency key.
  3. We store the PSP reference and the last four digits. Nothing else.

Deterministic test behaviour, driven by the card number:
    4242…4242  -> approve
    4000…0002  -> decline (insufficient funds)
    4000…0069  -> decline (expired card)
    4000…0119  -> processing error (exercises the retry/escalation path)
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="FitForge Mock PSP", version="1.0.0")

# In-memory only; this is a simulator, not a ledger.
_TOKENS: dict[str, dict] = {}
_CHARGES: dict[str, dict] = {}          # idempotency_key -> charge
_CHARGES_BY_ID: dict[str, dict] = {}

_DECLINE_MAP = {
    "4000000000000002": ("declined", "insufficient_funds"),
    "4000000000000069": ("declined", "expired_card"),
    "4000000000000119": ("failed", "processing_error"),
}


class TokenizeRequest(BaseModel):
    card_number: str = Field(min_length=12, max_length=19)
    exp_month: int = Field(ge=1, le=12)
    exp_year: int = Field(ge=2024, le=2100)
    cvc: str = Field(min_length=3, max_length=4)
    cardholder: str


class TokenizeResponse(BaseModel):
    token: str
    last4: str
    brand: str


class ChargeRequest(BaseModel):
    token: str
    amount_cents: int = Field(gt=0)
    currency: str = "usd"
    # Replay protection. Agent loops retry; customers must not be charged twice.
    idempotency_key: str
    description: str = ""


class ChargeResponse(BaseModel):
    id: str
    status: Literal["captured", "declined", "failed"]
    amount_cents: int
    last4: str | None = None
    reference: str | None = None
    decline_reason: str | None = None
    idempotent_replay: bool = False


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "mockpsp"}


@app.post("/v1/tokens", response_model=TokenizeResponse)
def tokenize(req: TokenizeRequest) -> TokenizeResponse:
    """Exchange card details for an opaque token.

    In production this endpoint is called by the customer's browser, not by us.
    """
    digits = "".join(c for c in req.card_number if c.isdigit())
    if len(digits) < 12:
        raise HTTPException(status_code=400, detail="invalid card number")

    token = f"tok_{uuid.uuid4().hex}"
    brand = "visa" if digits.startswith("4") else "mastercard" if digits.startswith("5") else "unknown"
    _TOKENS[token] = {
        "digits": digits,
        "last4": digits[-4:],
        "brand": brand,
        "created": time.time(),
    }
    return TokenizeResponse(token=token, last4=digits[-4:], brand=brand)


@app.post("/v1/charges", response_model=ChargeResponse)
def charge(req: ChargeRequest) -> ChargeResponse:
    """Charge a token.

    Idempotent by key: replaying the same key returns the original outcome
    rather than taking a second payment.
    """
    existing = _CHARGES.get(req.idempotency_key)
    if existing is not None:
        return ChargeResponse(**existing, idempotent_replay=True)

    card = _TOKENS.get(req.token)
    if card is None:
        raise HTTPException(status_code=404, detail="unknown or already-consumed token")

    status, reason = _DECLINE_MAP.get(card["digits"], ("captured", None))

    charge_id = f"ch_{uuid.uuid4().hex[:20]}"
    reference = hashlib.sha256(
        f"{charge_id}{req.amount_cents}{req.idempotency_key}".encode()
    ).hexdigest()[:24]

    result = {
        "id": charge_id,
        "status": status,
        "amount_cents": req.amount_cents,
        "last4": card["last4"],
        "reference": reference if status == "captured" else None,
        "decline_reason": reason,
    }
    _CHARGES[req.idempotency_key] = result
    _CHARGES_BY_ID[charge_id] = result

    # Single-use tokens, like a real PSP.
    if status == "captured":
        _TOKENS.pop(req.token, None)

    return ChargeResponse(**result, idempotent_replay=False)


@app.get("/v1/charges/{charge_id}", response_model=ChargeResponse)
def get_charge(charge_id: str) -> ChargeResponse:
    result = _CHARGES_BY_ID.get(charge_id)
    if result is None:
        raise HTTPException(status_code=404, detail="unknown charge")
    return ChargeResponse(**result, idempotent_replay=False)

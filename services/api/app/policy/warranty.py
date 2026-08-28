"""Warranty coverage engine — deterministic.

No LLM output reaches this module and no LLM decision leaves it. The agent may
propose *which part* it thinks has failed; whether that part is covered is
arithmetic over the purchase date and the model's warranty terms.

This matters for three reasons:

  * It is a money decision. A model that is right 95% of the time is wrong on
    500 of every 10,000 sessions, and each one is either a wrongly refused
    customer or a wrongly given refund.
  * It has to be explainable. The engine returns the exact reason, and that
    string is what the customer sees — not a paraphrase the model invented.
  * It has to be auditable. Every verdict is written to audit_log with its
    inputs, so a disputed decision can be reconstructed months later.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date

from ..db import audit, query_one

# Wear items get a defect-only window regardless of the parts warranty. A belt
# that wears out after a year is not a manufacturing defect.
CONSUMABLE_DEFECT_WINDOW_DAYS = 90


@dataclass
class CoverageDecision:
    covered: bool
    reason_code: str
    reason: str                       # customer-facing, shown verbatim
    part_number: str
    part_class: str
    months_owned: float
    coverage_months: int
    order_id: str | None = None
    purchase_date: str | None = None
    commercial_use: bool = False
    # What the customer pays if we proceed. Zero when covered.
    customer_pays_cents: int = 0
    unit_price_cents: int = 0
    inputs: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


def _months_between(start: date, end: date) -> float:
    return (end - start).days / 30.4375


def check_coverage(
    *,
    part_number: str,
    order_id: str | None = None,
    session_id: str | None = None,
    issue_id: str | None = None,
    today: date | None = None,
) -> CoverageDecision:
    """Decide whether a part is covered under warranty.

    `order_id` is required for a positive coverage decision: coverage attaches
    to a purchase, not to a person. Without it we can still quote a price, but
    the answer is "not covered" and the reason says why.
    """
    today = today or date.today()

    part = query_one(
        """
        SELECT p.part_number, p.model_id, p.name, p.part_class, p.price_cents,
               p.safety_class, p.in_stock
          FROM parts p WHERE p.part_number = %s
        """,
        (part_number,),
    )
    if part is None:
        # The agent proposed a part number that does not exist. This is the
        # hallucination guard: it fails closed, loudly, and never reaches a quote.
        return CoverageDecision(
            covered=False, reason_code="unknown_part",
            reason="That part number is not in the catalog.",
            part_number=part_number, part_class="unknown",
            months_owned=0.0, coverage_months=0,
        )

    terms = query_one(
        "SELECT * FROM warranty_terms WHERE model_id = %s", (part["model_id"],)
    )
    if terms is None:
        return CoverageDecision(
            covered=False, reason_code="no_terms",
            reason="No warranty terms are on file for this model, so the part "
                   "must be purchased. A human agent can review this.",
            part_number=part_number, part_class=part["part_class"],
            months_owned=0.0, coverage_months=0,
            unit_price_cents=part["price_cents"],
            customer_pays_cents=part["price_cents"],
        )

    order = None
    if order_id:
        order = query_one(
            """
            SELECT id, purchased_at, commercial_use, model_id
              FROM orders WHERE id = %s
            """,
            (order_id,),
        )

    price = part["price_cents"]
    base = dict(
        part_number=part_number, part_class=part["part_class"],
        unit_price_cents=price, order_id=order_id,
    )

    # --- no proof of purchase -------------------------------------------
    if order is None:
        return _finish(CoverageDecision(
            covered=False, reason_code="no_purchase_record",
            reason="We could not match this to a purchase on file, so warranty "
                   "coverage cannot be applied. If you have your order number or "
                   "the serial number, a human agent can look it up.",
            months_owned=0.0, coverage_months=0,
            customer_pays_cents=price, **base,
        ), session_id, issue_id)

    # --- the part must belong to the machine that was bought ------------
    if order["model_id"] != part["model_id"]:
        return _finish(CoverageDecision(
            covered=False, reason_code="model_mismatch",
            reason="That part is not for the model on this order. Let us confirm "
                   "which machine we are working on before ordering anything.",
            months_owned=0.0, coverage_months=0,
            purchase_date=str(order["purchased_at"]),
            customer_pays_cents=price, **base,
        ), session_id, issue_id)

    months = round(_months_between(order["purchased_at"], today), 2)
    days_owned = (today - order["purchased_at"]).days
    commercial = bool(order["commercial_use"])

    coverage_months = {
        "frame": terms["frame_months"],
        "mechanical": terms["parts_months"],
        "electronics": terms["electronics_months"],
        "consumable": 0,
    }[part["part_class"]]

    common = dict(
        months_owned=months, coverage_months=coverage_months,
        purchase_date=str(order["purchased_at"]), commercial_use=commercial,
        inputs={
            "days_owned": days_owned,
            "terms": {k: v for k, v in terms.items() if k != "notes"},
            "evaluated_on": str(today),
        },
        **base,
    )

    # --- consumables: defect window only --------------------------------
    if part["part_class"] == "consumable" and not terms["consumables_covered"]:
        if days_owned <= CONSUMABLE_DEFECT_WINDOW_DAYS and not commercial:
            return _finish(CoverageDecision(
                covered=True, reason_code="consumable_defect_window",
                reason=f"This is a wear item, but your machine is only "
                       f"{days_owned} days old, so it is covered under the "
                       f"{CONSUMABLE_DEFECT_WINDOW_DAYS}-day defect window at no charge.",
                customer_pays_cents=0, **common,
            ), session_id, issue_id)
        return _finish(CoverageDecision(
            covered=False, reason_code="consumable_excluded",
            reason=f"This is a wear item. Wear items are covered for "
                   f"manufacturing defects for {CONSUMABLE_DEFECT_WINDOW_DAYS} days "
                   f"from purchase and not after, so this one is not covered.",
            customer_pays_cents=price, **common,
        ), session_id, issue_id)

    # --- commercial use voids parts and labour --------------------------
    # Checked after the consumable branch so the reason the customer gets is the
    # most specific one that applies, not merely the first one we happened to test.
    if commercial and part["part_class"] in ("mechanical", "electronics"):
        return _finish(CoverageDecision(
            covered=False, reason_code="commercial_use_void",
            reason="This machine is registered for commercial use, which excludes "
                   "parts and labour coverage under the residential warranty. The "
                   "part can still be ordered.",
            customer_pays_cents=price, **common,
        ), session_id, issue_id)

    # --- the ordinary case ------------------------------------------------
    if months <= coverage_months:
        return _finish(CoverageDecision(
            covered=True, reason_code="in_warranty",
            reason=f"Covered. {part['name']} is a {part['part_class']} part with "
                   f"{coverage_months} months of coverage, and your machine was "
                   f"purchased {months:.0f} months ago. There is no charge for the part.",
            customer_pays_cents=0, **common,
        ), session_id, issue_id)

    return _finish(CoverageDecision(
        covered=False, reason_code="expired",
        reason=f"Out of warranty. {part['name']} is covered for {coverage_months} "
               f"months and your machine was purchased {months:.0f} months ago, "
               f"so the part would need to be purchased.",
        customer_pays_cents=price, **common,
    ), session_id, issue_id)


def _finish(decision: CoverageDecision, session_id: str | None,
            issue_id: str | None) -> CoverageDecision:
    """Write the verdict to the audit log before returning it."""
    audit(
        "warranty_decision",
        actor="policy_engine",
        session_id=session_id,
        issue_id=issue_id,
        payload=json.loads(json.dumps(decision.to_json(), default=str)),
    )
    return decision

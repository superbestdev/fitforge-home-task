"""Warranty engine tests.

This is the module that decides who pays, so it gets the most direct tests in
the repo. Every case here is a boundary someone will actually hit: the day a
warranty expires, a wear item inside and outside its defect window, a commercial
machine, a part number that does not exist.

None of these tests involve an LLM. That is the point — the coverage decision is
arithmetic, and arithmetic can be tested exactly.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from services.api.app.db import execute, query_one
from services.api.app.policy.warranty import (
    CONSUMABLE_DEFECT_WINDOW_DAYS, check_coverage,
)


@pytest.fixture(scope="module")
def fixtures() -> dict:
    """A model with known warranty terms and one part of each class."""
    model = query_one("""
        SELECT m.id, w.frame_months, w.parts_months, w.electronics_months
          FROM models m JOIN warranty_terms w ON w.model_id = m.id
         WHERE m.category_id = 'treadmill' LIMIT 1
    """)
    assert model, "seed the catalog first: python -m seed.generate_catalog"

    parts = {}
    for part_class in ("mechanical", "electronics", "consumable", "frame"):
        row = query_one(
            "SELECT part_number, price_cents FROM parts "
            "WHERE model_id = %s AND part_class = %s LIMIT 1",
            (model["id"], part_class),
        )
        if row:
            parts[part_class] = row

    customer = query_one("SELECT id FROM customers LIMIT 1")
    return {"model": model, "parts": parts, "customer": customer}


def _make_order(model_id: str, customer_id: str, *, days_ago: int,
                commercial: bool = False) -> str:
    """Create a purchase a given number of days in the past."""
    order_id = f"TEST-{days_ago}-{int(commercial)}-{model_id[-6:]}"
    purchased = date.today() - timedelta(days=days_ago)
    serial = f"TESTSERIAL{abs(hash(order_id)) % 10_000_000:07d}"
    execute(
        """
        INSERT INTO orders (id, customer_id, model_id, serial_number,
                            purchased_at, channel, commercial_use)
        VALUES (%s, %s, %s, %s, %s, 'test', %s)
        ON CONFLICT (id) DO UPDATE
          SET purchased_at = EXCLUDED.purchased_at,
              commercial_use = EXCLUDED.commercial_use
        """,
        (order_id, customer_id, model_id, serial, purchased, commercial),
    )
    return order_id


# ---------------------------------------------------------------------------
# The ordinary cases
# ---------------------------------------------------------------------------

def test_mechanical_part_inside_warranty_is_covered(fixtures):
    model, parts, customer = fixtures["model"], fixtures["parts"], fixtures["customer"]
    part = parts["mechanical"]
    order = _make_order(model["id"], customer["id"], days_ago=30)

    decision = check_coverage(part_number=part["part_number"], order_id=order)

    assert decision.covered is True
    assert decision.reason_code == "in_warranty"
    assert decision.customer_pays_cents == 0


def test_mechanical_part_after_expiry_is_not_covered(fixtures):
    model, parts, customer = fixtures["model"], fixtures["parts"], fixtures["customer"]
    part = parts["mechanical"]
    # Comfortably past the parts window.
    days = int(model["parts_months"] * 30.4375) + 120
    order = _make_order(model["id"], customer["id"], days_ago=days)

    decision = check_coverage(part_number=part["part_number"], order_id=order)

    assert decision.covered is False
    assert decision.reason_code == "expired"
    assert decision.customer_pays_cents == part["price_cents"]


def test_coverage_boundary_is_not_off_by_one(fixtures):
    """The day before expiry is covered; well after is not.

    Boundary conditions on a date are where warranty engines actually break,
    and the failure is invisible until a customer is wrongly refused.
    """
    model, parts, customer = fixtures["model"], fixtures["parts"], fixtures["customer"]
    part = parts["mechanical"]
    window_days = int(model["parts_months"] * 30.4375)

    just_inside = _make_order(model["id"], customer["id"], days_ago=window_days - 10)
    assert check_coverage(part_number=part["part_number"],
                          order_id=just_inside).covered is True

    well_outside = _make_order(model["id"], customer["id"], days_ago=window_days + 40)
    assert check_coverage(part_number=part["part_number"],
                          order_id=well_outside).covered is False


# ---------------------------------------------------------------------------
# Wear items
# ---------------------------------------------------------------------------

def test_consumable_inside_defect_window_is_covered(fixtures):
    model, parts, customer = fixtures["model"], fixtures["parts"], fixtures["customer"]
    part = parts["consumable"]
    order = _make_order(model["id"], customer["id"],
                        days_ago=CONSUMABLE_DEFECT_WINDOW_DAYS - 10)

    decision = check_coverage(part_number=part["part_number"], order_id=order)

    assert decision.covered is True
    assert decision.reason_code == "consumable_defect_window"


def test_consumable_after_defect_window_is_excluded(fixtures):
    """A belt that wears out after a year is not a manufacturing defect."""
    model, parts, customer = fixtures["model"], fixtures["parts"], fixtures["customer"]
    part = parts["consumable"]
    order = _make_order(model["id"], customer["id"],
                        days_ago=CONSUMABLE_DEFECT_WINDOW_DAYS + 30)

    decision = check_coverage(part_number=part["part_number"], order_id=order)

    assert decision.covered is False
    assert decision.reason_code == "consumable_excluded"
    assert decision.customer_pays_cents == part["price_cents"]


def test_consumable_excluded_even_while_parts_warranty_runs(fixtures):
    """The wear-item exclusion must beat the still-active parts warranty."""
    model, parts, customer = fixtures["model"], fixtures["parts"], fixtures["customer"]
    part = parts["consumable"]
    order = _make_order(model["id"], customer["id"], days_ago=200)

    decision = check_coverage(part_number=part["part_number"], order_id=order)

    assert decision.covered is False
    assert decision.reason_code == "consumable_excluded"


# ---------------------------------------------------------------------------
# Exclusions and failure modes
# ---------------------------------------------------------------------------

def test_commercial_use_voids_parts_coverage(fixtures):
    model, parts, customer = fixtures["model"], fixtures["parts"], fixtures["customer"]
    part = parts["mechanical"]
    order = _make_order(model["id"], customer["id"], days_ago=30, commercial=True)

    decision = check_coverage(part_number=part["part_number"], order_id=order)

    assert decision.covered is False
    assert decision.reason_code == "commercial_use_void"


def test_no_purchase_record_means_no_coverage(fixtures):
    """Coverage attaches to a purchase, not to a person."""
    part = fixtures["parts"]["mechanical"]

    decision = check_coverage(part_number=part["part_number"], order_id=None)

    assert decision.covered is False
    assert decision.reason_code == "no_purchase_record"
    assert decision.customer_pays_cents == part["price_cents"]


def test_unknown_part_fails_closed(fixtures):
    """The hallucination guard.

    If the agent invents a part number, the engine must refuse rather than
    price something that does not exist.
    """
    decision = check_coverage(part_number="FF-TT-NOPE-999-IMAGINARY", order_id=None)

    assert decision.covered is False
    assert decision.reason_code == "unknown_part"


def test_part_from_another_model_is_rejected(fixtures):
    """Coverage must not apply a part to the wrong machine."""
    model, parts, customer = fixtures["model"], fixtures["parts"], fixtures["customer"]
    order = _make_order(model["id"], customer["id"], days_ago=30)

    other = query_one(
        "SELECT part_number FROM parts WHERE model_id <> %s LIMIT 1", (model["id"],)
    )
    decision = check_coverage(part_number=other["part_number"], order_id=order)

    assert decision.covered is False
    assert decision.reason_code == "model_mismatch"


def test_every_decision_is_audited(fixtures):
    """A money decision that is not written down did not happen."""
    model, parts, customer = fixtures["model"], fixtures["parts"], fixtures["customer"]
    part = parts["mechanical"]
    order = _make_order(model["id"], customer["id"], days_ago=45)

    before = query_one(
        "SELECT count(*) AS n FROM audit_log WHERE action = 'warranty_decision'"
    )["n"]
    check_coverage(part_number=part["part_number"], order_id=order)
    after = query_one(
        "SELECT count(*) AS n FROM audit_log WHERE action = 'warranty_decision'"
    )["n"]

    assert after == before + 1


def test_reason_is_customer_readable(fixtures):
    """The reason string is shown to the customer verbatim, so it must read well."""
    model, parts, customer = fixtures["model"], fixtures["parts"], fixtures["customer"]
    order = _make_order(model["id"], customer["id"], days_ago=30)

    decision = check_coverage(part_number=parts["mechanical"]["part_number"],
                              order_id=order)

    assert len(decision.reason) > 20
    assert decision.reason[0].isupper()
    # No internal identifiers leaking into customer-facing text.
    assert "reason_code" not in decision.reason
    assert "None" not in decision.reason

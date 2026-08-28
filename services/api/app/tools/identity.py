"""Customer and model identification.

The case study makes model identification a precondition for troubleshooting,
and it is the right call: every downstream step — retrieval, parts, warranty —
is keyed on model_id. Get it wrong and the system is confidently, fluently
wrong about everything that follows, which is worse than being unhelpful.

So identification is a ladder, tried strongest evidence first:

  1. order lookup      confidence 0.99  — the customer's own purchase record
  2. serial number     confidence 0.97  — the plate on the machine
  3. guided narrowing  confidence 0.60-0.90 — category + distinguishing features
  4. model-plate photo (interface defined, not implemented — see the docstring
     on identify_by_photo)

Anything below the configured threshold is not accepted silently. The agent
either asks a narrowing question or asks the customer to confirm outright.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..db import query, query_one

log = logging.getLogger(__name__)

CONF_ORDER = 0.99
CONF_SERIAL = 0.97
CONF_CONFIRMED = 0.95      # customer explicitly said yes to a specific model


@dataclass
class ModelCandidate:
    model_id: str
    name: str
    category_id: str
    confidence: float
    method: str
    order_id: str | None = None
    serial_number: str | None = None
    purchased_at: str | None = None
    evidence: dict | None = None


# ---------------------------------------------------------------------------
# 1. Customer lookup
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
ORDER_RE = re.compile(r"\bFF-\d{4}-\d{4,7}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")


def find_customer(*, email: str | None = None, phone: str | None = None,
                  order_id: str | None = None) -> dict | None:
    """Look a customer up by any identifier they have offered."""
    if order_id:
        row = query_one(
            """
            SELECT c.id, c.email, c.phone, c.full_name, c.address
              FROM customers c JOIN orders o ON o.customer_id = c.id
             WHERE upper(o.id) = upper(%s)
            """,
            (order_id.strip(),),
        )
        if row:
            return dict(row)

    if email:
        row = query_one(
            "SELECT id, email, phone, full_name, address FROM customers "
            "WHERE lower(email) = lower(%s)",
            (email.strip(),),
        )
        if row:
            return dict(row)

    if phone:
        digits = re.sub(r"\D", "", phone)[-10:]
        if len(digits) == 10:
            row = query_one(
                "SELECT id, email, phone, full_name, address FROM customers "
                "WHERE right(regexp_replace(phone, '\\D', '', 'g'), 10) = %s",
                (digits,),
            )
            if row:
                return dict(row)
    return None


def extract_identifiers(text: str) -> dict:
    """Pull email / order id / phone / serial out of free text.

    Regex rather than an LLM extraction call. These are strict formats, the
    regex is exact where a 3B model is merely usually right, and it costs
    nothing on a CPU budget.
    """
    out: dict = {}
    if m := EMAIL_RE.search(text):
        out["email"] = m.group(0)
    if m := ORDER_RE.search(text):
        out["order_id"] = m.group(0).upper()
    if m := PHONE_RE.search(text):
        out["phone"] = m.group(0)
    if serials := find_serial_candidates(text):
        out["serial"] = serials[0]
    return out


# ---------------------------------------------------------------------------
# 2. Orders owned by a customer
# ---------------------------------------------------------------------------

def customer_equipment(customer_id: str) -> list[ModelCandidate]:
    """Every machine this customer has bought.

    When there is exactly one, identification is finished. When there are
    several — and roughly 45% of seeded customers own more than one — the agent
    must ask which machine, and that question is also what disambiguates issue
    threads later in a multi-issue session.
    """
    rows = query(
        """
        SELECT o.id AS order_id, o.serial_number, o.purchased_at,
               m.id AS model_id, m.name, m.category_id
          FROM orders o JOIN models m ON m.id = o.model_id
         WHERE o.customer_id = %s
         ORDER BY o.purchased_at DESC
        """,
        (customer_id,),
    )
    return [
        ModelCandidate(
            model_id=r["model_id"], name=r["name"], category_id=r["category_id"],
            confidence=CONF_ORDER, method="order_lookup",
            order_id=r["order_id"], serial_number=r["serial_number"],
            purchased_at=str(r["purchased_at"]),
            evidence={"source": "purchase record"},
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 3. Serial number
# ---------------------------------------------------------------------------

# Serials look like <PREFIX><YY><NNNNN>, e.g. TPA35024812345.
SERIAL_RE = re.compile(r"\b([A-Z]{1,3}[A-Z0-9]{1,6}\d{7})\b", re.IGNORECASE)


def find_serial_candidates(text: str) -> list[str]:
    cleaned = text.upper().replace(" ", "").replace("-", "")
    return [m.group(1) for m in SERIAL_RE.finditer(cleaned)]


def identify_by_serial(serial: str) -> ModelCandidate | None:
    """Resolve a serial number to a model.

    Tries the registered serial first (which also yields the order, and with it
    the purchase date the warranty engine needs), then falls back to prefix
    matching for a machine bought second-hand or never registered.
    """
    normalised = serial.strip().upper().replace(" ", "").replace("-", "")

    row = query_one(
        """
        SELECT o.id AS order_id, o.serial_number, o.purchased_at,
               m.id AS model_id, m.name, m.category_id
          FROM orders o JOIN models m ON m.id = o.model_id
         WHERE upper(o.serial_number) = %s
        """,
        (normalised,),
    )
    if row:
        return ModelCandidate(
            model_id=row["model_id"], name=row["name"],
            category_id=row["category_id"], confidence=CONF_SERIAL,
            method="serial_number", order_id=row["order_id"],
            serial_number=row["serial_number"],
            purchased_at=str(row["purchased_at"]),
            evidence={"source": "registered serial number", "matched": "exact"},
        )

    # Unregistered or second-hand: the prefix still identifies the model, but we
    # have no purchase date, so warranty will come back "no purchase record".
    matches = query(
        """
        SELECT id AS model_id, name, category_id, serial_prefix
          FROM models
         WHERE %s LIKE serial_prefix || '%%'
         ORDER BY length(serial_prefix) DESC
         LIMIT 3
        """,
        (normalised,),
    )
    if len(matches) == 1:
        r = matches[0]
        return ModelCandidate(
            model_id=r["model_id"], name=r["name"], category_id=r["category_id"],
            confidence=0.90, method="serial_number", serial_number=normalised,
            evidence={
                "source": "serial prefix",
                "matched": r["serial_prefix"],
                "caveat": "Serial is not registered to an order, so no purchase "
                          "date is available for warranty purposes.",
            },
        )
    return None


# ---------------------------------------------------------------------------
# 4. Guided narrowing
# ---------------------------------------------------------------------------

def narrow_models(
    *,
    category_id: str | None = None,
    name_fragment: str | None = None,
    features: dict | None = None,
    limit: int = 8,
) -> list[ModelCandidate]:
    """Narrow the catalog when there is no order and no serial.

    Confidence is a function of how far the candidate set has been narrowed: a
    single survivor is strong evidence, five survivors is not evidence at all.
    Returning that number honestly is what stops the agent proceeding on a guess.
    """
    clauses, params = [], {}
    if category_id:
        clauses.append("m.category_id = %(category_id)s")
        params["category_id"] = category_id
    if name_fragment:
        clauses.append("m.name ILIKE %(frag)s")
        params["frag"] = f"%{name_fragment.strip()}%"
    if features:
        for i, (axis, value) in enumerate(features.items()):
            clauses.append(f"m.features ->> %(fk{i})s ILIKE %(fv{i})s")
            params[f"fk{i}"] = axis
            params[f"fv{i}"] = f"%{value}%"

    if not clauses:
        return []

    params["limit"] = limit + 1
    rows = query(
        f"""
        SELECT m.id AS model_id, m.name, m.category_id, m.features, m.model_year
          FROM models m
         WHERE {' AND '.join(clauses)}
         ORDER BY m.discontinued, m.model_year DESC, m.id
         LIMIT %(limit)s
        """,
        params,
    )

    n = len(rows)
    if n == 1:
        confidence = 0.90
    elif n <= 3:
        confidence = 0.60
    else:
        confidence = 0.35

    return [
        ModelCandidate(
            model_id=r["model_id"], name=r["name"], category_id=r["category_id"],
            confidence=confidence, method="guided_narrowing",
            evidence={"features": r["features"], "candidates_remaining": n},
        )
        for r in rows[:limit]
    ]


def distinguishing_question(candidates: list[ModelCandidate]) -> dict | None:
    """Pick the feature axis that best splits the remaining candidates.

    Classic decision-tree splitting rather than asking the model to invent a
    question. It is deterministic, always asks something that actually reduces
    the set, and never asks about an axis on which every candidate agrees —
    which is the failure mode when you leave this to a small LLM.
    """
    if len(candidates) < 2:
        return None

    rows = query(
        "SELECT id, features FROM models WHERE id = ANY(%s)",
        ([c.model_id for c in candidates],),
    )
    features_by_model = {r["id"]: (r["features"] or {}) for r in rows}

    best_axis, best_values, best_score = None, None, -1.0
    axes = {axis for f in features_by_model.values() for axis in f}

    for axis in axes:
        values = [f.get(axis) for f in features_by_model.values() if f.get(axis)]
        if len(values) < 2:
            continue
        distinct = sorted(set(values))
        if len(distinct) < 2:
            continue
        # Prefer the axis whose largest group is smallest — the most even split.
        largest = max(values.count(v) for v in distinct)
        score = 1.0 - (largest / len(values))
        if score > best_score:
            best_axis, best_values, best_score = axis, distinct, score

    if not best_axis:
        return None

    prompts = {
        "console": "What does the screen on your machine look like?",
        "deck": "What colour is the deck under the running belt?",
        "folding": "Does your treadmill fold up for storage?",
        "incline": "What incline range does the display show?",
        "resistance": "How does the resistance work on your machine?",
        "pedals": "What kind of pedals does it have?",
        "size": "What size is the screen?",
        "mount": "Is it on a floor stand or mounted to the wall?",
        "camera": "Does it have a camera?",
        "stride": "What stride length is printed on the frame?",
        "drive": "Where is the flywheel — in front of you, or behind?",
        "rail": "What is the rail made of?",
        "stack": "What does the weight stack go up to?",
        "attachments": "Which attachment set came with it?",
        "footprint": "Is it wall-mounted or free-standing?",
    }
    return {
        "axis": best_axis,
        "question": prompts.get(best_axis, f"Which {best_axis} does your machine have?"),
        "options": best_values,
        "split_quality": round(best_score, 2),
    }


def get_model(model_id: str) -> dict | None:
    row = query_one(
        """
        SELECT m.id, m.name, m.category_id, m.family_id, m.model_year,
               m.features, m.serial_prefix, m.msrp_cents, m.discontinued,
               c.safety_class, c.name AS category_name
          FROM models m JOIN product_categories c ON c.id = m.category_id
         WHERE m.id = %s
        """,
        (model_id,),
    )
    return dict(row) if row else None


def identify_by_photo(image_bytes: bytes) -> ModelCandidate | None:
    """Identify a model from a photo of its serial plate.

    Not implemented. The interface is defined because it belongs in the
    identification ladder and is the highest-value addition to it — customers
    photograph the plate far more readily than they type a 14-character serial.

    It is out of scope here for an honest reason rather than an oversight: the
    only GPU on this host is a 1 GB GT 710, and a vision model plus OCR on CPU
    would take tens of seconds per image. In production this would be a small
    VLM behind the same tool interface, and nothing else in the system would
    need to change. See docs/05-risks-assumptions.md.
    """
    raise NotImplementedError(
        "Photo identification is not implemented in this build. "
        "Fall back to serial entry or guided narrowing."
    )

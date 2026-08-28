"""Parts catalog lookups.

The rule this module exists to enforce: **a part number is never authored by the
LLM.** The model may say "I think the running belt has failed"; turning that into
`FF-TT-PACER-350-BELT` at $89.00 is a database lookup. A hallucinated part number
that reaches a quote is a wrong charge and a wrong shipment, and small models
invent plausible-looking identifiers readily.
"""

from __future__ import annotations

import logging
import re

from ..db import query, query_one

log = logging.getLogger(__name__)


def get_part(part_number: str) -> dict | None:
    row = query_one(
        """
        SELECT p.part_number, p.model_id, p.name, p.part_class, p.price_cents,
               p.symptom_tags, p.customer_replaceable, p.safety_class, p.in_stock,
               c.safety_class AS category_safety_class
          FROM parts p
          JOIN models m ON m.id = p.model_id
          JOIN product_categories c ON c.id = m.category_id
         WHERE p.part_number = %s
        """,
        (part_number.strip().upper(),),
    )
    return dict(row) if row else None


def list_parts(model_id: str) -> list[dict]:
    return [dict(r) for r in query(
        """
        SELECT part_number, name, part_class, price_cents,
               customer_replaceable, safety_class, in_stock
          FROM parts WHERE model_id = %s ORDER BY part_class, name
        """,
        (model_id,),
    )]


def find_parts_for_symptom(*, model_id: str, symptom: str,
                           limit: int = 5) -> list[dict]:
    """Map a described fault to candidate parts for one model.

    Ranking is by overlap between the customer's words and the curated
    `symptom_tags`, with a trigram similarity fallback on the part name. Both
    halves are ordinary SQL: cheap, explainable, and — unlike a similarity
    search over prose — incapable of returning a part that belongs to a
    different machine.
    """
    text = symptom.lower().strip()
    if not text:
        return []

    # Word-level matching, not substring. A whole-phrase LIKE fails on ordinary
    # paraphrase — the tag "blank screen" never matches "screen blank and device
    # unresponsive" — and that miss silently drops the customer out of the parts
    # path entirely. Comparing significant words survives reordering and padding.
    words = [w for w in re.split(r"[^a-z0-9]+", text)
             if len(w) > 2 and w not in _STOPWORDS]
    if not words:
        words = [text]

    rows = query(
        """
        WITH scored AS (
            SELECT p.part_number, p.name, p.part_class, p.price_cents,
                   p.symptom_tags, p.customer_replaceable, p.safety_class,
                   p.in_stock,
                   -- How many words of a tag appear in the customer's wording,
                   -- normalised by tag length so a 1-word tag cannot outrank a
                   -- fully-matched 3-word tag.
                   (
                     SELECT COALESCE(max(
                              (SELECT count(*) FROM unnest(string_to_array(tag, ' ')) tw
                                WHERE tw = ANY(%(words)s))::float
                              / GREATEST(array_length(string_to_array(tag, ' '), 1), 1)
                            ), 0)
                       FROM unnest(p.symptom_tags) AS tag
                   ) AS tag_score,
                   similarity(lower(p.name), %(text)s) AS name_sim
              FROM parts p
             WHERE p.model_id = %(model_id)s
        )
        SELECT * FROM scored
         WHERE tag_score >= 0.5 OR name_sim > 0.3
         ORDER BY tag_score DESC, name_sim DESC, price_cents ASC
         LIMIT %(limit)s
        """,
        {"text": text, "words": words, "model_id": model_id, "limit": limit},
    )
    return [dict(r) for r in rows]


_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "not", "you", "your",
    "was", "are", "its", "has", "have", "when", "will", "does", "did", "but",
    "device", "machine", "unit", "problem", "issue", "fault", "broken",
}


def resolve_part_slugs(*, model_id: str, slugs: list[str]) -> list[dict]:
    """Turn the manual's likely-part hints into real catalog rows.

    Troubleshooting sections name parts as prose ("rear roller"); part numbers
    are built as `<model_id>-<SLUG>`. This bridges the two so a retrieved
    procedure can lead directly to an orderable part.
    """
    if not slugs:
        return []
    candidates = [f"{model_id}-{s.strip().upper().replace(' ', '-')}" for s in slugs]
    rows = query(
        """
        SELECT part_number, name, part_class, price_cents, customer_replaceable,
               safety_class, in_stock
          FROM parts WHERE part_number = ANY(%s)
        """,
        (candidates,),
    )
    by_number = {r["part_number"]: dict(r) for r in rows}
    # Preserve the manual's ordering — it lists most likely cause first.
    return [by_number[c] for c in candidates if c in by_number]


def validate_part_for_model(part_number: str, model_id: str) -> tuple[bool, str]:
    """Guard called before any part reaches a quote."""
    part = get_part(part_number)
    if part is None:
        return False, f"Part number {part_number} does not exist in the catalog."
    if part["model_id"] != model_id:
        return False, (
            f"Part {part_number} belongs to model {part['model_id']}, "
            f"not {model_id}."
        )
    if not part["in_stock"]:
        return False, f"{part['name']} is currently out of stock."
    return True, "ok"

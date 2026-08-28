"""Generate the FitForge catalog: models, warranty terms, parts, customers, orders.

Note what this script does NOT write: `error_codes` and `doc_chunks`. Those are
produced by the ingestion pipeline reading the generated PDFs, because that is
the pipeline we actually want to exercise. Seeding them directly would hide the
part of the system most likely to break in production.

    python -m seed.generate_catalog
"""

from __future__ import annotations

import json
import logging
import random
from datetime import date, timedelta

from faker import Faker

from services.api.app.config import settings
from services.api.app.db import execute, execute_many, query_one

from .taxonomy import CATEGORIES, WARRANTY_PROFILES, CategoryTemplate

log = logging.getLogger(__name__)

# Model numbers look like real product tiers rather than 1..N.
TIER_NUMBERS = [100, 150, 200, 250, 300, 350, 400, 450, 500, 550,
                600, 700, 750, 800, 900, 950]
TIER_SUFFIX = ["", "", "", " Pro", " Elite", " S", " X", " Sport", " Studio"]


def _model_id(cat: CategoryTemplate, family_id: str, number: int, suffix: str) -> str:
    tail = suffix.strip().upper().replace(" ", "")
    base = f"FF-{cat.serial_letter}{cat.id[:1].upper()}-{family_id.upper()}-{number}"
    return f"{base}-{tail}" if tail else base


def build_models(rng: random.Random, target: int) -> list[dict]:
    """Spread `target` SKUs across categories and families deterministically."""
    models: list[dict] = []
    # Round-robin across families so every category gets depth.
    families = [(cat, fid, fname) for cat in CATEGORIES for fid, fname in cat.families]

    per_family = max(1, target // len(families))
    for cat, family_id, family_name in families:
        numbers = rng.sample(TIER_NUMBERS, min(per_family, len(TIER_NUMBERS)))
        for number in sorted(numbers):
            suffix = rng.choice(TIER_SUFFIX)
            model_id = _model_id(cat, family_id, number, suffix)
            if any(m["id"] == model_id for m in models):
                continue

            year = rng.choice([2021, 2022, 2023, 2024, 2025, 2026])
            features = {
                axis: rng.choice(options)
                for axis, options in cat.feature_axes.items()
            }
            # Price scales with tier number; keeps the catalog internally sensible.
            msrp = int((number * 4.2 + rng.randint(-120, 240)) * 100)

            models.append({
                "id": model_id,
                "family_id": family_id,
                "category_id": cat.id,
                "name": f"{family_name} {number}{suffix} {cat.name}",
                "model_year": year,
                # Serial prefix is what makes serial-number identification work.
                "serial_prefix": f"{cat.serial_letter}{family_id[:2].upper()}{number}",
                "msrp_cents": max(29900, msrp),
                "features": features,
                "released_on": date(year, rng.randint(1, 12), rng.randint(1, 28)),
                "discontinued": year <= 2022 and rng.random() < 0.6,
            })
    return models


def insert_taxonomy() -> None:
    for cat in CATEGORIES:
        execute(
            """
            INSERT INTO product_categories (id, name, safety_class)
            VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING
            """,
            (cat.id, cat.name, cat.safety_class),
        )
        for family_id, family_name in cat.families:
            execute(
                """
                INSERT INTO product_families (id, category_id, name)
                VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING
                """,
                (family_id, cat.id, family_name),
            )


def insert_models(models: list[dict]) -> None:
    execute_many(
        """
        INSERT INTO models (id, family_id, category_id, name, model_year,
                            serial_prefix, msrp_cents, features, released_on, discontinued)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        [
            (m["id"], m["family_id"], m["category_id"], m["name"], m["model_year"],
             m["serial_prefix"], m["msrp_cents"], json.dumps(m["features"]),
             m["released_on"], m["discontinued"])
            for m in models
        ],
    )


def insert_warranty(models: list[dict]) -> None:
    rows = []
    for m in models:
        p = WARRANTY_PROFILES[m["category_id"]]
        rows.append((
            m["id"], p["frame"], p["parts"], p["electronics"], p["labor"],
            p["consumables"],
            "Commercial use voids parts and labour coverage. "
            "Wear items (belts, straps, pads, cables) are excluded once the "
            "90-day defect window has passed.",
        ))
    execute_many(
        """
        INSERT INTO warranty_terms (model_id, frame_months, parts_months,
                                    electronics_months, labor_months,
                                    consumables_covered, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (model_id) DO NOTHING
        """,
        rows,
    )


def insert_parts(rng: random.Random, models: list[dict]) -> int:
    """Every model gets the full part set for its category, priced per tier."""
    from .taxonomy import CATEGORY_BY_ID

    rows = []
    for m in models:
        cat = CATEGORY_BY_ID[m["category_id"]]
        # Higher-tier machines have proportionally more expensive parts.
        tier_factor = m["msrp_cents"] / 200_000
        for pt in cat.parts:
            price = int(pt.base_price_cents * (0.75 + 0.5 * tier_factor))
            rows.append((
                f"{m['id']}-{pt.slug.upper()}",
                m["id"],
                pt.name,
                pt.part_class,
                max(900, price),
                list(pt.symptom_tags),
                pt.customer_replaceable,
                pt.safety_class,
                rng.random() > 0.04,          # ~4% out of stock, exercises that path
            ))
    execute_many(
        """
        INSERT INTO parts (part_number, model_id, name, part_class, price_cents,
                           symptom_tags, customer_replaceable, safety_class, in_stock)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (part_number) DO NOTHING
        """,
        rows,
    )
    return len(rows)


def insert_customers_and_orders(
    rng: random.Random, models: list[dict], n_customers: int = 400
) -> None:
    fake = Faker()
    Faker.seed(settings.seed_random_seed)

    customers = []
    for _ in range(n_customers):
        name = fake.name()
        email = f"{name.lower().replace(' ', '.').replace(chr(39), '')}@example.com"
        customers.append({
            "email": email,
            "full_name": name,
            "phone": f"+1{rng.randint(2000000000, 9899999999)}",
            "address": {
                "line1": fake.street_address(),
                "city": fake.city(),
                "state": fake.state_abbr(),
                "postal_code": fake.postcode(),
                "country": "US",
            },
        })

    inserted: list[str] = []
    for c in customers:
        row = execute(
            """
            INSERT INTO customers (email, phone, full_name, address)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name
            RETURNING id
            """,
            (c["email"], c["phone"], c["full_name"], json.dumps(c["address"])),
        )
        if row:
            inserted.append(str(row["id"]))

    # Orders. A meaningful slice of customers own two or more machines — that is
    # what makes the multi-issue scenario realistic rather than contrived.
    order_rows = []
    seq = 1
    today = date.today()
    for cust_id in inserted:
        n_orders = rng.choices([1, 2, 3], weights=[0.55, 0.35, 0.10])[0]
        chosen = rng.sample(models, n_orders)
        for m in chosen:
            # Spread purchase dates across the warranty boundary on purpose, so
            # the coverage engine sees in-warranty, edge, and expired cases.
            days_ago = rng.choice([
                rng.randint(5, 89),        # brand new
                rng.randint(90, 700),      # in parts warranty
                rng.randint(701, 1100),    # electronics expired, parts maybe
                rng.randint(1101, 2200),   # out of warranty
            ])
            purchased = today - timedelta(days=days_ago)
            serial = f"{m['serial_prefix']}{purchased.year % 100:02d}{rng.randint(10000, 99999)}"
            order_rows.append((
                f"FF-{purchased.year}-{seq:07d}",
                cust_id, m["id"], serial, purchased, "web",
                rng.random() < 0.05,       # a few commercial-use customers
            ))
            seq += 1

    execute_many(
        """
        INSERT INTO orders (id, customer_id, model_id, serial_number,
                            purchased_at, channel, commercial_use)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        order_rows,
    )
    log.info("inserted %d customers, %d orders", len(inserted), len(order_rows))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rng = random.Random(settings.seed_random_seed)

    log.info("seeding categories and families")
    insert_taxonomy()

    models = build_models(rng, settings.seed_num_models)
    log.info("generated %d models across %d categories", len(models), len(CATEGORIES))
    insert_models(models)
    insert_warranty(models)

    n_parts = insert_parts(rng, models)
    log.info("inserted %d parts", n_parts)

    insert_customers_and_orders(rng, models)

    counts = query_one(
        """
        SELECT (SELECT count(*) FROM models)    AS models,
               (SELECT count(*) FROM parts)     AS parts,
               (SELECT count(*) FROM customers) AS customers,
               (SELECT count(*) FROM orders)    AS orders
        """
    )
    log.info("catalog ready: %s", dict(counts or {}))


if __name__ == "__main__":
    main()

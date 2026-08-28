"""Put the demo back to its opening state, so a take can be repeated.

Rehearsing the scenario is destructive by design: it uploads a manual (which
flips a model from unbacked to backed) and places a real order. Recording a
second take against that state shows the audience the *end* of the story in
the first thirty seconds.

    python -m seed.demo_reset                    # the bike demo
    python -m seed.demo_reset --model FF-...     # some other model
    python -m seed.demo_reset --keep-sessions    # leave the queue populated

What it does NOT touch: the seeded catalog, customers, orders, parts or
warranty terms. Only the state a demo run creates.
"""

from __future__ import annotations

import argparse
import logging

from services.api.app.db import execute, query, query_one

log = logging.getLogger(__name__)

DEMO_MODEL = "FF-BB-VELODROME-300-S"
DEMO_EMAIL = "james.maldonado@example.com"


def unback(model_id: str) -> None:
    """Remove an uploaded manual and return the model to `unbacked`."""
    manuals = query("SELECT id FROM manuals WHERE model_id = %s AND uploaded IS TRUE",
                    (model_id,))
    if not manuals:
        log.info("no uploaded manual for %s — nothing to remove", model_id)
    else:
        # coverage_registry holds a foreign key to manuals, so it has to let go
        # first or the delete is refused.
        execute("UPDATE coverage_registry SET manual_id = NULL WHERE model_id = %s",
                (model_id,))
        execute("DELETE FROM doc_chunks WHERE model_id = %s", (model_id,))
        execute("DELETE FROM error_codes WHERE model_id = %s", (model_id,))
        for m in manuals:
            execute("DELETE FROM manuals WHERE id = %s", (m["id"],))
        log.info("removed %d uploaded manual(s) for %s", len(manuals), model_id)

    execute(
        """
        UPDATE coverage_registry
           SET status = 'unbacked', quality_score = 0, chunk_count = 0,
               sections_present = '{}', manual_id = NULL,
               notes = 'Print-only manual; no digital copy on file.'
         WHERE model_id = %s
        """,
        (model_id,),
    )
    execute("DELETE FROM ingest_jobs WHERE model_id = %s", (model_id,))


def clear_demo_sessions(email: str) -> None:
    """Drop the chat history, threads, quotes and orders the demo generated."""
    cust = query_one("SELECT id FROM customers WHERE email = %s", (email,))
    if cust is None:
        log.warning("no customer %s", email)
        return

    sessions = query("SELECT id FROM sessions WHERE customer_id = %s", (cust["id"],))
    ids = [s["id"] for s in sessions]
    if not ids:
        log.info("no sessions for %s", email)
        return

    # Parts the agent ordered live in part_orders; `orders` is the seeded
    # purchase history that says which machines the customer owns, and deleting
    # from it would take their equipment away mid-demo. Order matters below:
    # part_orders references payments, which reference quotes.
    execute("DELETE FROM part_orders WHERE session_id = ANY(%s)", (ids,))
    execute("DELETE FROM payments WHERE quote_id IN "
            "(SELECT id FROM quotes WHERE session_id = ANY(%s))", (ids,))
    execute("DELETE FROM quotes WHERE session_id = ANY(%s)", (ids,))
    execute("DELETE FROM handoffs WHERE session_id = ANY(%s)", (ids,))
    execute("DELETE FROM issue_threads WHERE session_id = ANY(%s)", (ids,))
    execute("DELETE FROM session_messages WHERE session_id = ANY(%s)", (ids,))
    execute("DELETE FROM verified_models WHERE session_id = ANY(%s)", (ids,))
    execute("DELETE FROM audit_log WHERE session_id = ANY(%s)", (ids,))
    execute("DELETE FROM llm_calls WHERE session_id = ANY(%s)", (ids,))
    execute("DELETE FROM sessions WHERE id = ANY(%s)", (ids,))
    log.info("cleared %d demo session(s) for %s", len(ids), email)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Reset the demo to its opening state")
    ap.add_argument("--model", default=DEMO_MODEL, help="model to return to unbacked")
    ap.add_argument("--email", default=DEMO_EMAIL, help="demo customer")
    ap.add_argument("--keep-sessions", action="store_true",
                    help="leave the escalation queue and chat history alone")
    args = ap.parse_args()

    unback(args.model)
    if not args.keep_sessions:
        clear_demo_sessions(args.email)

    cov = query_one(
        """
        SELECT count(*) FILTER (WHERE status = 'backed')   AS backed,
               count(*) FILTER (WHERE status = 'degraded') AS degraded,
               count(*) FILTER (WHERE status = 'unbacked') AS unbacked
          FROM coverage_registry
        """
    )
    log.info("-" * 60)
    log.info("coverage: %s backed / %s degraded / %s unbacked",
             cov["backed"], cov["degraded"], cov["unbacked"])
    log.info("%s is ready to be uploaded again.", args.model)
    log.info("-" * 60)


if __name__ == "__main__":
    main()

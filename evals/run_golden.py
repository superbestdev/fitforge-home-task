"""Golden session replay.

Replays the scripted sessions in `golden_sessions.yaml` against a running API
and checks outcomes. Assertions are deliberately about *what happened* — which
machine was identified, which threads exist, which escalation fired — and never
about wording, because wording assertions break on every prompt change and tell
you nothing about whether the agent works.

    python -m evals.run_golden
    python -m evals.run_golden --only multi_issue_threads_stay_separate

Exit code is non-zero if any session fails, so this can gate CI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from services.api.app.db import query, query_one

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_sessions(path: Path) -> list[dict]:
    """Read the golden file.

    An earlier version hand-parsed a YAML subset to avoid a dependency. That was
    a false economy — the parser grew fiddly the moment the file used inline
    maps, and a bug in it looks exactly like a bug in the agent.
    """
    import yaml

    with path.open(encoding="utf-8") as fh:
        sessions = yaml.safe_load(fh) or []

    for spec in sessions:
        spec.setdefault("turns", [])
        spec.setdefault("customer", {})
        spec.setdefault("expect_final", {})
        for turn in spec["turns"]:
            turn.setdefault("expect", {})
    return sessions


# ---------------------------------------------------------------------------
# Selecting a customer that fits the scenario
# ---------------------------------------------------------------------------

@dataclass
class Subject:
    email: str
    models: list[dict] = field(default_factory=list)
    error_code: str | None = None
    part_name: str | None = None


def pick_customer(spec: dict) -> Subject | None:
    """Find a seeded customer matching the scenario's requirements."""
    if spec.get("coverage") == "unbacked":
        row = query_one("""
            SELECT c.email, m.id AS model_id, m.name
              FROM customers c
              JOIN orders o ON o.customer_id = c.id
              JOIN models m ON m.id = o.model_id
              JOIN coverage_registry r ON r.model_id = m.id
             WHERE r.status = 'unbacked'
             LIMIT 1
        """)
        if not row:
            return None
        return Subject(row["email"], [{"model_id": row["model_id"],
                                       "name": row["name"]}])

    if spec.get("owns_multiple"):
        clause = ("AND r1.status = 'backed' AND r2.status = 'backed'"
                  if spec.get("all_backed") else "")
        row = query_one(f"""
            SELECT c.email,
                   m1.id AS m1_id, m1.name AS m1_name,
                   m2.id AS m2_id, m2.name AS m2_name
              FROM customers c
              JOIN orders o1 ON o1.customer_id = c.id
              JOIN models m1 ON m1.id = o1.model_id
              JOIN coverage_registry r1 ON r1.model_id = m1.id
              JOIN orders o2 ON o2.customer_id = c.id
              JOIN models m2 ON m2.id = o2.model_id
              JOIN coverage_registry r2 ON r2.model_id = m2.id
             WHERE m1.id <> m2.id
               AND m1.category_id <> m2.category_id
               {clause}
             LIMIT 1
        """)
        if not row:
            return None
        return Subject(row["email"], [
            {"model_id": row["m1_id"], "name": row["m1_name"]},
            {"model_id": row["m2_id"], "name": row["m2_name"]},
        ])

    # Exactly one machine, optionally backed / out of warranty / has codes.
    conditions = ["r.status = 'backed'"] if spec.get("coverage") == "backed" else []
    if spec.get("out_of_warranty"):
        conditions.append("o.purchased_at < current_date - interval '40 months'")
    if spec.get("has_error_codes"):
        conditions.append("EXISTS (SELECT 1 FROM error_codes e WHERE e.model_id = m.id)")
    where = (" AND " + " AND ".join(conditions)) if conditions else ""

    row = query_one(f"""
        SELECT c.email, m.id AS model_id, m.name
          FROM customers c
          JOIN orders o ON o.customer_id = c.id
          JOIN models m ON m.id = o.model_id
          JOIN coverage_registry r ON r.model_id = m.id
         WHERE (SELECT count(*) FROM orders o2 WHERE o2.customer_id = c.id) = 1
           {where}
         LIMIT 1
    """)
    if not row:
        return None

    subject = Subject(row["email"], [{"model_id": row["model_id"],
                                      "name": row["name"]}])
    if spec.get("has_error_codes"):
        code = query_one("SELECT code FROM error_codes WHERE model_id = %s LIMIT 1",
                         (row["model_id"],))
        subject.error_code = code["code"] if code else None

    # A real, orderable wear item for this model, so the scenario asks for
    # something a customer would plausibly name.
    part = query_one(
        "SELECT name FROM parts WHERE model_id = %s AND part_class = 'consumable' "
        "AND in_stock ORDER BY price_cents LIMIT 1",
        (row["model_id"],),
    )
    subject.part_name = part["name"].lower() if part else "belt"
    return subject


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def check_turn(expect: dict, result: dict, subject: Subject) -> list[str]:
    failures: list[str] = []
    issues = result.get("issues", [])
    reply = (result.get("reply") or "").lower()
    trace = " ".join(result.get("trace", []))

    def substitute(text: str) -> str:
        return (text.replace("{error_code}", subject.error_code or "")
                    .replace("{machine_1}", subject.models[0]["name"] if subject.models else "")
                    .replace("{machine_2}", subject.models[1]["name"] if len(subject.models) > 1 else ""))

    if "issue_count" in expect and len(issues) != expect["issue_count"]:
        failures.append(f"issue_count: expected {expect['issue_count']}, got {len(issues)}")

    if expect.get("no_issue_opened") and issues:
        failures.append(f"expected no issue opened, got {len(issues)}")

    if "issue_status" in expect:
        statuses = {i["status"] for i in issues}
        if expect["issue_status"] not in statuses:
            failures.append(f"issue_status: expected {expect['issue_status']}, got {statuses}")

    if expect.get("escalated") and not result.get("escalated"):
        failures.append("expected escalation, none occurred")

    if "escalation_reason" in expect:
        actual = (result.get("escalation") or {}).get("reason")
        if actual != expect["escalation_reason"]:
            failures.append(f"escalation_reason: expected {expect['escalation_reason']}, got {actual}")

    if "trace_contains" in expect and expect["trace_contains"] not in trace:
        failures.append(f"trace missing {expect['trace_contains']!r}; got {trace}")

    if "reply_contains_any" in expect:
        options = expect["reply_contains_any"]
        if isinstance(options, str):
            options = [options]
        if not any(substitute(str(o)).lower() in reply for o in options):
            failures.append(f"reply contained none of {options}")

    if expect.get("has_citations") and not result.get("citations"):
        failures.append("expected citations, got none")

    if expect.get("issues_have_distinct_models"):
        models = {i.get("model_id") for i in issues if i.get("model_id")}
        if len(models) < 2:
            failures.append(f"expected distinct models across threads, got {models}")

    if expect.get("citations_belong_to_issue_model"):
        active = next((i for i in issues if i["id"] == result.get("active_issue_id")), None)
        for citation in result.get("citations", []):
            chunk_id = citation.get("chunk_id")
            if chunk_id is None or active is None:
                continue
            owner = query_one("SELECT model_id FROM doc_chunks WHERE id = %s", (chunk_id,))
            if owner and owner["model_id"] != active["model_id"]:
                failures.append(
                    f"citation {chunk_id} belongs to {owner['model_id']}, "
                    f"thread is {active['model_id']}"
                )

    return failures


def check_final(expect: dict, session_id: str) -> list[str]:
    failures: list[str] = []
    issues = query(
        "SELECT status, model_id FROM issue_threads WHERE session_id = %s",
        (session_id,),
    )
    if "issue_count" in expect and len(issues) != expect["issue_count"]:
        failures.append(f"final issue_count: expected {expect['issue_count']}, got {len(issues)}")
    if "distinct_models" in expect:
        models = {i["model_id"] for i in issues if i["model_id"]}
        if len(models) != expect["distinct_models"]:
            failures.append(f"final distinct_models: expected {expect['distinct_models']}, got {len(models)}")

    quotes = query("SELECT covered, total_cents FROM quotes WHERE session_id = %s",
                   (session_id,))
    if expect.get("quote_created") and not quotes:
        failures.append("expected a quote, none created")
    return failures


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_session(client: httpx.Client, api: str, spec: dict) -> tuple[str, list[str], float]:
    subject = pick_customer(spec.get("customer") or {})
    if subject is None:
        return "skip", [f"no seeded customer matches {spec.get('customer')}"], 0.0

    started = time.monotonic()
    resp = client.post(f"{api}/api/sessions", json={"customer_email": subject.email})
    resp.raise_for_status()
    session_id = resp.json()["session_id"]

    failures: list[str] = []
    for turn in spec["turns"]:
        message = str(turn["say"])
        message = (message.replace("{error_code}", subject.error_code or "E1")
                          .replace("{part_name}", subject.part_name or "belt")
                          .replace("{machine_1}", subject.models[0]["name"])
                          .replace("{machine_2}", subject.models[1]["name"]
                                   if len(subject.models) > 1 else ""))

        r = client.post(f"{api}/api/chat",
                        json={"session_id": session_id, "message": message})
        r.raise_for_status()
        result = r.json()

        # Quote assertions live on the turn but are checked against the DB.
        expect = dict(turn.get("expect") or {})
        quote_checks = {k: expect.pop(k) for k in list(expect)
                        if k.startswith("quote_")}
        failures.extend(check_turn(expect, result, subject))

        if quote_checks:
            quote = query_one(
                "SELECT covered, total_cents FROM quotes WHERE session_id = %s "
                "ORDER BY created_at DESC LIMIT 1", (session_id,))
            if quote_checks.get("quote_created") and quote is None:
                failures.append("expected a quote, none created")
            elif quote is not None:
                if "quote_covered" in quote_checks and quote["covered"] != quote_checks["quote_covered"]:
                    failures.append(f"quote_covered: expected {quote_checks['quote_covered']}, got {quote['covered']}")
                if quote_checks.get("quote_total_positive") and quote["total_cents"] <= 0:
                    failures.append(f"expected a positive quote total, got {quote['total_cents']}")

    failures.extend(check_final(spec.get("expect_final") or {}, session_id))
    return ("pass" if not failures else "fail"), failures, time.monotonic() - started


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://api:8000")
    parser.add_argument("--only", help="run one session by id")
    parser.add_argument("--file", default=str(Path(__file__).parent / "golden_sessions.yaml"))
    args = parser.parse_args()

    sessions = load_sessions(Path(args.file))
    if args.only:
        sessions = [s for s in sessions if s["id"] == args.only]
    if not sessions:
        print("no sessions to run", file=sys.stderr)
        sys.exit(1)

    print(f"\n{BOLD}Golden session replay — {len(sessions)} scenario(s){RESET}\n")

    passed = failed = skipped = 0
    with httpx.Client(timeout=900.0) as client:
        for spec in sessions:
            status, failures, took = run_session(client, args.api, spec)

            if status == "pass":
                passed += 1
                print(f"{GREEN}PASS{RESET} {spec['id']:44} {DIM}{took:5.1f}s{RESET}")
            elif status == "skip":
                skipped += 1
                print(f"{YELLOW}SKIP{RESET} {spec['id']:44} {DIM}{failures[0]}{RESET}")
            else:
                failed += 1
                print(f"{RED}FAIL{RESET} {spec['id']:44} {DIM}{took:5.1f}s{RESET}")
                for f in failures:
                    print(f"       {RED}·{RESET} {f}")

    total = passed + failed
    rate = (passed / total * 100) if total else 0
    print(f"\n{BOLD}{passed}/{total} passed ({rate:.0f}%){RESET}"
          f"{f', {skipped} skipped' if skipped else ''}\n")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

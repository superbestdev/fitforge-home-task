"""Scripted demo: one session, two unrelated issues, on two different machines.

This is the bonus scenario from the case study, and it is the one that separates
a design that models issues from a design that models a transcript. The customer:

  1. opens with a treadmill belt problem
  2. works through a couple of diagnostic steps
  3. interrupts mid-diagnosis with a completely unrelated smart-bike fault
  4. is taken through the bike issue to a part order (with warranty + payment)
  5. is brought back to the treadmill thread, which resumes with its own history
  6. ends with the treadmill thread escalating to a human

Both threads reach terminal states independently, and the handoff packet at the
end carries both — including the one that was already resolved.

    python -m evals.demo_multi_issue
    python -m evals.demo_multi_issue --api http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time

import httpx

BOLD, DIM, CYAN, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)


def _wrap(text: str, indent: str = "     ") -> str:
    return "\n".join(
        textwrap.fill(line, width=96, initial_indent=indent,
                      subsequent_indent=indent) or indent
        for line in text.splitlines()
    )


class Demo:
    def __init__(self, api: str, email: str, psp: str = "http://mockpsp:8090"):
        self.api = api.rstrip("/")
        self.psp = psp.rstrip("/")
        self.client = httpx.Client(timeout=600.0)
        self.session_id: str | None = None
        self.email = email
        self.turns = 0
        self.elapsed = 0.0

    def start(self) -> None:
        resp = self.client.post(f"{self.api}/api/sessions",
                                json={"customer_email": self.email})
        resp.raise_for_status()
        data = resp.json()
        self.session_id = data["session_id"]
        print(f"{DIM}session {self.session_id}{RESET}\n")
        print(f"{GREEN}{BOLD}AGENT{RESET}")
        print(_wrap(data["greeting"]))
        print()

    def say(self, message: str, *, note: str = "") -> dict:
        self.turns += 1
        if note:
            print(f"{YELLOW}{DIM}--- {note} ---{RESET}")
        print(f"{CYAN}{BOLD}CUSTOMER{RESET}")
        print(_wrap(message))

        started = time.monotonic()
        resp = self.client.post(f"{self.api}/api/chat", json={
            "session_id": self.session_id, "message": message,
        })
        took = time.monotonic() - started
        self.elapsed += took
        resp.raise_for_status()
        data = resp.json()

        print(f"{GREEN}{BOLD}AGENT{RESET} {DIM}({took:.1f}s){RESET}")
        print(_wrap(data["reply"]))

        if data.get("citations"):
            cites = "; ".join(
                f"{c.get('section')}/{c.get('heading') or c.get('code')} {c.get('pages') or ''}".strip()
                for c in data["citations"][:3]
            )
            print(f"{DIM}     cited: {cites}{RESET}")

        print(f"{DIM}     path : {' -> '.join(data.get('trace', []))}{RESET}")
        self._print_threads(data.get("issues", []))
        if data.get("escalated"):
            esc = data.get("escalation") or {}
            print(f"{RED}     ESCALATED: {esc.get('reason')} — {esc.get('detail')}{RESET}")
        print()
        return data

    def pay(self, card: str = "4242424242424242") -> dict:
        """Complete a payment the way the widget does: tokenize, then charge."""
        print(f"{YELLOW}{DIM}--- customer enters card details in the secure form ---{RESET}")
        tok = self.client.post(f"{self.psp}/v1/tokens", json={
            "card_number": card, "exp_month": 12, "exp_year": 2030,
            "cvc": "123", "cardholder": "Demo Customer",
        })
        tok.raise_for_status()
        token = tok.json()["token"]
        print(f"{DIM}     PSP token: {token[:20]}...  (card never touches our backend){RESET}")

        resp = self.client.post(f"{self.api}/api/pay", json={
            "session_id": self.session_id, "card_token": token,
        })
        resp.raise_for_status()
        data = resp.json()
        print(f"{GREEN}{BOLD}AGENT{RESET}")
        print(_wrap(data.get("reply", "")))
        print(f"{DIM}     path : {' -> '.join(data.get('trace', []))}{RESET}")
        self._print_threads(data.get("issues", []))
        print()
        return data

    @staticmethod
    def _print_threads(issues: list) -> None:
        if not issues:
            return
        parts = []
        for i in issues:
            colour = (GREEN if i["status"] == "resolved"
                      else RED if i["status"] in ("escalated", "unresolvable")
                      else YELLOW)
            parts.append(f"{colour}#{i['seq']} {i['title']} [{i['status']} "
                         f"{i['step_budget_used']} steps]{RESET}")
        print(f"{DIM}     threads:{RESET} " + " | ".join(parts))

    def summary(self) -> None:
        resp = self.client.get(f"{self.api}/api/sessions/{self.session_id}")
        resp.raise_for_status()
        data = resp.json()

        print(f"\n{BOLD}{'=' * 96}{RESET}")
        print(f"{BOLD}FINAL SESSION STATE{RESET}")
        print(f"{'=' * 96}")
        for issue in data["issues"]:
            colour = (GREEN if issue["status"] == "resolved"
                      else RED if issue["status"] in ("escalated", "unresolvable")
                      else YELLOW)
            print(f"\n{colour}{BOLD}Issue #{issue['seq']}: {issue['title']}{RESET}")
            print(f"  status        : {issue['status']}")
            print(f"  machine       : {issue['model_id']}")
            print(f"  steps used    : {issue['step_budget_used']}")
            if issue.get("ruled_out"):
                print(f"  ruled out     : {', '.join(issue['ruled_out'][:4])}")
            if issue.get("candidate_part"):
                print(f"  part          : {issue['candidate_part']}")
            if issue.get("resolution_note"):
                print(f"  resolution    : {issue['resolution_note']}")

        print(f"\n{BOLD}Machines identified{RESET}")
        for v in data["verified_models"]:
            print(f"  {v['model_id']:32} via {v['method']:18} "
                  f"confidence {v['confidence']:.2f}")

        # The handoff packet is the deliverable of the escalation path.
        hq = self.client.get(f"{self.api}/api/handoffs", params={"status": "all"})
        hq.raise_for_status()
        handoffs = [h for h in hq.json()["handoffs"]
                    if h["session_id"] == self.session_id]
        if handoffs:
            print(f"\n{BOLD}Handoff packet{RESET}")
            full = self.client.get(f"{self.api}/api/handoffs/{handoffs[0]['id']}").json()
            packet = full["packet"]
            print(f"  reason        : {full['reason']} — {full['detail']}")
            print(f"  customer      : {(packet.get('customer') or {}).get('name')}")
            print(f"  threads carried: {len(packet['issues'])}")
            for b in packet["issues"]:
                print(f"     #{b['seq']} {b['title']:38} [{b['status']:12}] "
                      f"{len(b['steps_taken'])} steps recorded")
            print(f"  orders carried : {len(packet['orders'])}")
            print(f"  transcript     : {len(packet['transcript'])} messages")
            print(f"\n{BOLD}  Summary for the human agent:{RESET}")
            print(_wrap(packet["summary"]["text"], indent="     "))
            print(f"\n{BOLD}  Recommended next action:{RESET}")
            print(_wrap(packet["summary"]["next_action"], indent="     "))

        print(f"\n{DIM}{self.turns} turns, {self.elapsed:.0f}s total, "
              f"{self.elapsed / max(self.turns, 1):.1f}s average{RESET}")


SCRIPT = [
    # --- Issue 1: treadmill ------------------------------------------------
    ("My Pacer treadmill belt keeps slipping when I run on it", "ISSUE 1 opens"),
    ("Yes it's on a hard floor, and when I lift the belt in the middle it comes "
     "up about 4 inches", ""),
    ("I tightened both rear bolts a quarter turn each and it still slips", ""),

    # --- Issue 2 interrupts, different machine ------------------------------
    ("Actually hang on - my Velodrome bike is also playing up, the screen is "
     "totally blank and won't come on", "ISSUE 2 interrupts (different machine)"),
    ("The power brick LED is lit and the connector is seated properly", ""),
    ("I held the power button for 20 seconds and nothing happened, still black", ""),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://api:8000")
    parser.add_argument("--psp", default="http://mockpsp:8090")
    parser.add_argument("--email", default=None,
                        help="customer to run as; must own a treadmill and a bike")
    args = parser.parse_args()

    email = args.email or find_multi_machine_customer(args.api)
    if not email:
        print("No suitable customer found. Run the seed first.", file=sys.stderr)
        sys.exit(1)

    print(f"\n{BOLD}FitForge agentic support — multi-issue session demo{RESET}")
    print(f"{DIM}customer: {email}{RESET}\n")

    demo = Demo(args.api, email, args.psp)
    demo.start()

    for message, note in SCRIPT:
        demo.say(message, note=note)

    # Return to the suspended thread. Whether it resumes or reports that it has
    # already been escalated depends on how the diagnosis went — both are
    # correct, and the point is that the thread has its own state either way.
    demo.say("Ok let's go back to the treadmill belt problem",
             note="RETURN TO ISSUE 1 — the thread carries its own state")

    # --- the commerce path -------------------------------------------------
    # Asking to order a part directly routes to the deterministic path:
    # catalog lookup -> warranty engine -> quote -> explicit confirmation -> PSP.
    result = demo.say(
        "Can I just order a replacement running belt for the treadmill?",
        note="COMMERCE — part lookup, warranty decision, quote",
    )

    if result.get("pending_confirmation"):
        confirmed = demo.say("Yes please, go ahead and order it",
                             note="CUSTOMER CONFIRMS the exact quoted figures")
        if confirmed.get("requires_payment"):
            demo.pay()

    demo.summary()


CUSTOMER_SQL = """
SELECT c.email
  FROM customers c
  JOIN orders o1 ON o1.customer_id = c.id
  JOIN models m1 ON m1.id = o1.model_id
  JOIN coverage_registry r1 ON r1.model_id = m1.id AND r1.status = 'backed'
  JOIN orders o2 ON o2.customer_id = c.id
  JOIN models m2 ON m2.id = o2.model_id
  JOIN coverage_registry r2 ON r2.model_id = m2.id AND r2.status = 'backed'
 WHERE m1.category_id = 'treadmill'
   AND m2.category_id = 'bike'
   -- Out of warranty on the bike, so the demo exercises the payment path
   -- rather than the free-replacement path.
   AND o2.purchased_at < current_date - interval '30 months'
 LIMIT 1
"""


def find_multi_machine_customer(api: str) -> str | None:
    """Pick a seeded customer who owns both a treadmill and a bike, both covered.

    Uses the application's own database layer, so the demo runs inside the api
    container with no extra dependencies.
    """
    try:
        from services.api.app.db import query_one

        row = query_one(CUSTOMER_SQL)
        return row["email"] if row else None
    except Exception as exc:                            # noqa: BLE001
        print(f"could not query for a demo customer: {exc}", file=sys.stderr)
        print("Run this inside the api container:\n"
              "  docker compose run --rm --no-deps api "
              "python -m evals.demo_multi_issue --api http://api:8000",
              file=sys.stderr)
        return None


if __name__ == "__main__":
    main()

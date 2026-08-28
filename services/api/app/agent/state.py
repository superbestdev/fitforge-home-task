"""Session state model.

The central idea: **a session is a list of independent issue threads, not one
conversation.** Customers routinely open with a treadmill problem and mention a
bike fault three turns later, and a flat message history handles that badly —
the model conflates symptoms, applies one machine's manual to the other, and
loses track of what was already ruled out.

Modelling each issue as its own object with its own model_id, its own step
budget and its own terminal state makes suspending thread A to deal with thread
B a bookkeeping operation rather than a prompt-engineering problem.

Two stores, with a deliberate split:

  * `issue_threads` / `sessions` in Postgres are the **system of record**. They
    are queried directly by the agent console and by the handoff builder, and
    they outlive the graph run.
  * The LangGraph checkpoint is the **engine's working memory** for a turn in
    flight, and is what makes a crashed turn resumable.

Domain facts always flow from the tables. The checkpoint never becomes a second
source of truth for anything a human needs to read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

from ..db import execute, query, query_one

IssueStatus = Literal[
    "new", "diagnosing", "awaiting_customer", "awaiting_part",
    "resolved", "unresolvable", "escalated",
]

TERMINAL_STATUSES = {"resolved", "unresolvable", "escalated"}


class DiagnosticStep(BaseModel):
    """One turn of the diagnose-observe loop."""

    n: int
    instruction: str                 # what we asked the customer to do
    customer_response: str | None = None
    interpretation: str | None = None    # what their answer told us
    ruled_out: list[str] = Field(default_factory=list)
    citations: list[dict] = Field(default_factory=list)
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class VerifiedModel(BaseModel):
    model_id: str
    name: str
    category_id: str
    method: str
    confidence: float
    order_id: str | None = None
    serial_number: str | None = None
    purchased_at: str | None = None
    evidence: dict = Field(default_factory=dict)


class IssueThread(BaseModel):
    """One customer problem, tracked end to end and independently resolvable."""

    id: str
    seq: int
    title: str
    status: IssueStatus = "new"
    model_id: str | None = None
    symptom_summary: str = ""
    steps: list[DiagnosticStep] = Field(default_factory=list)
    ruled_out: list[str] = Field(default_factory=list)
    citations: list[dict] = Field(default_factory=list)
    candidate_part: str | None = None
    quote_id: str | None = None
    step_budget_used: int = 0
    tool_failures: int = 0
    resolution_note: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_open(self) -> bool:
        return not self.is_terminal

    def last_instruction(self) -> str | None:
        return self.steps[-1].instruction if self.steps else None

    def history_digest(self, max_steps: int = 6) -> str:
        """Compact history for a prompt.

        Deliberately lossy and deliberately short. Feeding the full transcript
        back on every turn is what makes small-model agents both expensive and
        forgetful — on this hardware prompt evaluation runs at roughly 78
        tokens/second, so every 1000 tokens of history costs ~13 seconds of
        latency before a single word is generated.
        """
        if not self.steps:
            return "No diagnostic steps have been taken yet."
        lines = []
        for s in self.steps[-max_steps:]:
            lines.append(f"Step {s.n}: asked - {s.instruction}")
            if s.customer_response:
                lines.append(f"          customer - {s.customer_response}")
            if s.interpretation:
                lines.append(f"          concluded - {s.interpretation}")
        if self.ruled_out:
            lines.append("Ruled out so far: " + "; ".join(self.ruled_out))
        return "\n".join(lines)


class Confirmation(BaseModel):
    """A quote awaiting the customer's explicit yes."""

    quote_id: str
    issue_id: str
    part_number: str
    total_cents: int
    covered: bool
    confirmation_hash: str
    summary_shown: str
    awaiting: Literal["confirm", "payment"] = "confirm"


class EscalationRecord(BaseModel):
    reason: str
    detail: str | None = None
    handoff_id: str | None = None
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# LangGraph channel
# ---------------------------------------------------------------------------

def _replace(_old: Any, new: Any) -> Any:
    """Last write wins. Nodes own their fields; nothing accumulates silently."""
    return new


class GraphState(TypedDict, total=False):
    """What flows between graph nodes during a single turn."""

    session_id: Annotated[str, _replace]
    customer_id: Annotated[str | None, _replace]
    customer_message: Annotated[str, _replace]

    verified_models: Annotated[list[dict], _replace]
    issues: Annotated[list[dict], _replace]
    active_issue_id: Annotated[str | None, _replace]

    intent: Annotated[str, _replace]
    safety_level: Annotated[str, _replace]
    pending_confirmation: Annotated[dict | None, _replace]

    # Written by whichever node produced the customer-facing text.
    reply: Annotated[str, _replace]
    citations: Annotated[list[dict], _replace]
    escalate: Annotated[dict | None, _replace]
    trace: Annotated[list[str], _replace]

    # --- internal channels, passed between nodes within one turn -----------
    # These must be declared: LangGraph silently drops any key a node returns
    # that is not part of the state schema, which shows up much later as a flag
    # that mysteriously never arrives at the caller.
    _identifiers: Annotated[dict | None, _replace]
    _identified_now: Annotated[bool, _replace]
    _equipment_options: Annotated[list[str], _replace]
    _issue_title: Annotated[str | None, _replace]
    _named_model: Annotated[str | None, _replace]
    _target_seq: Annotated[int | None, _replace]
    _fallthrough_to_new_issue: Annotated[bool, _replace]
    _resume: Annotated[bool, _replace]
    _search_query: Annotated[str | None, _replace]
    _error_code: Annotated[str | None, _replace]
    _suspected_fault: Annotated[str | None, _replace]
    _selected_part: Annotated[str | None, _replace]
    _request_payment: Annotated[bool, _replace]


# ---------------------------------------------------------------------------
# Persistence — the system of record
# ---------------------------------------------------------------------------

def load_session(session_id: str) -> dict | None:
    row = query_one(
        "SELECT id, customer_id, channel, status, created_at FROM sessions WHERE id = %s",
        (session_id,),
    )
    return dict(row) if row else None


def create_session(customer_id: str | None = None, channel: str = "web_chat") -> str:
    row = execute(
        "INSERT INTO sessions (customer_id, channel) VALUES (%s, %s) RETURNING id",
        (customer_id, channel),
    )
    return str(row["id"])


def set_session_customer(session_id: str, customer_id: str) -> None:
    execute("UPDATE sessions SET customer_id = %s WHERE id = %s",
            (customer_id, session_id))


def load_issues(session_id: str) -> list[IssueThread]:
    rows = query(
        """
        SELECT id, seq, title, status, model_id, symptom_summary, steps,
               ruled_out, citations, candidate_part, quote_id,
               step_budget_used, tool_failures, resolution_note
          FROM issue_threads WHERE session_id = %s ORDER BY seq
        """,
        (session_id,),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["quote_id"] = str(d["quote_id"]) if d["quote_id"] else None
        d["steps"] = [DiagnosticStep(**s) for s in (d["steps"] or [])]
        out.append(IssueThread(**d))
    return out


def load_active_issue_id(session_id: str) -> str | None:
    """The open thread the conversation is actually on.

    The most recently updated one — `save_issue` stamps updated_at on every
    write, so this is the thread we last worked. Taking the *first* open issue
    instead silently undoes every thread switch on the following turn, which
    looks like the agent forgetting the customer changed the subject.
    """
    row = query_one(
        """
        SELECT id FROM issue_threads
         WHERE session_id = %s
           AND status NOT IN ('resolved', 'unresolvable', 'escalated')
         ORDER BY updated_at DESC, seq DESC
         LIMIT 1
        """,
        (session_id,),
    )
    return str(row["id"]) if row else None


def touch_issue(issue_id: str) -> None:
    """Mark a thread as the one we are working, without changing its content.

    `load_active_issue_id` resolves the active thread by `updated_at DESC`, and
    every other write path stamps that column through `save_issue`. Resuming a
    suspended thread changes no field on it, so without this the switch lives
    only in graph state: the next customer message re-derives the active thread
    from the database, finds the *other* one more recently updated, and answers on
    it. The customer sees the agent agree to go back and then carry on with the
    wrong machine.
    """
    execute("UPDATE issue_threads SET updated_at = now() WHERE id = %s", (issue_id,))


def create_issue(session_id: str, *, title: str, symptom_summary: str,
                 model_id: str | None = None) -> IssueThread:
    seq_row = query_one(
        "SELECT COALESCE(max(seq), 0) + 1 AS next FROM issue_threads WHERE session_id = %s",
        (session_id,),
    )
    seq = seq_row["next"] if seq_row else 1

    row = execute(
        """
        INSERT INTO issue_threads (session_id, seq, title, status, model_id,
                                   symptom_summary)
        VALUES (%s, %s, %s, 'new', %s, %s)
        RETURNING id
        """,
        (session_id, seq, title[:200], model_id, symptom_summary[:2000]),
    )
    return IssueThread(id=str(row["id"]), seq=seq, title=title[:200],
                       status="new", model_id=model_id,
                       symptom_summary=symptom_summary[:2000])


def save_issue(issue: IssueThread) -> None:
    execute(
        """
        UPDATE issue_threads
           SET title = %s, status = %s, model_id = %s, symptom_summary = %s,
               steps = %s, ruled_out = %s, citations = %s, candidate_part = %s,
               quote_id = %s, step_budget_used = %s, tool_failures = %s,
               resolution_note = %s, updated_at = now()
         WHERE id = %s
        """,
        (
            issue.title[:200], issue.status, issue.model_id,
            issue.symptom_summary[:2000],
            json.dumps([s.model_dump() for s in issue.steps]),
            json.dumps(issue.ruled_out),
            json.dumps(issue.citations),
            issue.candidate_part,
            issue.quote_id,
            issue.step_budget_used,
            issue.tool_failures,
            issue.resolution_note,
            issue.id,
        ),
    )


def load_verified_models(session_id: str) -> list[VerifiedModel]:
    rows = query(
        """
        SELECT v.model_id, m.name, m.category_id, v.method, v.confidence,
               v.order_id, v.evidence,
               o.serial_number, o.purchased_at
          FROM verified_models v
          JOIN models m ON m.id = v.model_id
          LEFT JOIN orders o ON o.id = v.order_id
         WHERE v.session_id = %s
         ORDER BY v.confidence DESC, v.id
        """,
        (session_id,),
    )
    return [
        VerifiedModel(
            model_id=r["model_id"], name=r["name"], category_id=r["category_id"],
            method=r["method"], confidence=r["confidence"], order_id=r["order_id"],
            serial_number=r["serial_number"],
            purchased_at=str(r["purchased_at"]) if r["purchased_at"] else None,
            evidence=r["evidence"] or {},
        )
        for r in rows
    ]


def record_verified_model(session_id: str, candidate: Any) -> None:
    execute(
        """
        INSERT INTO verified_models (session_id, model_id, order_id, method,
                                     confidence, evidence)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (session_id, model_id) DO UPDATE
          SET confidence = GREATEST(verified_models.confidence, EXCLUDED.confidence),
              method = EXCLUDED.method,
              order_id = COALESCE(EXCLUDED.order_id, verified_models.order_id)
        """,
        (session_id, candidate.model_id, candidate.order_id, candidate.method,
         candidate.confidence, json.dumps(candidate.evidence or {}, default=str)),
    )


def add_message(session_id: str, *, role: str, content: str,
                issue_id: str | None = None, meta: dict | None = None) -> None:
    execute(
        """
        INSERT INTO session_messages (session_id, issue_id, role, content, meta)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (session_id, issue_id, role, content, json.dumps(meta or {}, default=str)),
    )


def recent_messages(session_id: str, limit: int = 10) -> list[dict]:
    rows = query(
        """
        SELECT role, content, issue_id, created_at
          FROM session_messages WHERE session_id = %s
         ORDER BY id DESC LIMIT %s
        """,
        (session_id, limit),
    )
    return [dict(r) for r in reversed(rows)]

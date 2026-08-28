"""Graph assembly and the turn orchestrator.

The graph is deliberately explicit. Every edge is a named condition you can read
off the page, because the alternative — letting the model decide what to do next
by picking tools freely — puts an unreliable component in charge of control flow
over money and safety. This design keeps the model inside nodes and keeps the
routing between them in code.

One customer message = one run of this graph. Domain state is loaded from
Postgres at the start of the turn and written back inside the nodes, so the
checkpointer only ever holds in-flight turn context.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from ..config import settings
from ..db import audit
from ..policy import escalation
from . import commerce_nodes, nodes, state
from .state import GraphState, IssueThread

log = logging.getLogger(__name__)

_compiled = None
_checkpoint_pool = None


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------

def after_precheck(s: GraphState) -> str:
    """A safety stop or an explicit request for a human short-circuits everything."""
    if s.get("escalate"):
        return "handoff"
    return "route"


def dispatch(s: GraphState) -> str:
    """Send the turn to the node that owns this intent.

    The identification gate is enforced here rather than inside each node, so
    there is exactly one place where "we do not know which machine this is" can
    be missed — and it is three lines long.
    """
    intent = s.get("intent", "continue_issue")

    if intent == "escalate":
        return "handoff"
    if intent == "confirm" and s.get("pending_confirmation"):
        return "confirm"
    if intent == "chitchat":
        return "chitchat"

    # `provide_identity` only re-enters identification when it would actually
    # change something: we still need a machine, or the customer has supplied a
    # new identifier. Routing there unconditionally makes the agent re-ask
    # "which machine is this about?" every time the router mislabels an ordinary
    # answer as identity — which it does, and which reads as the agent having
    # forgotten the conversation.
    if intent == "provide_identity":
        if nodes.needs_identification(s) or s.get("_identifiers"):
            return "identify"
        intent = "continue_issue"

    # THE GATE. No diagnosis, no parts, no commerce until we know the model.
    if nodes.needs_identification(s):
        return "identify"

    if intent == "commerce":
        # A part request still belongs to an issue thread — that is what carries
        # the model, the quote and the eventual resolution. If the customer asks
        # to buy something with no open thread (or the relevant one has already
        # been escalated), open one first rather than quoting into the void.
        issues = [IssueThread(**i) for i in s.get("issues", [])]
        target_model = s.get("_named_model")
        relevant = [i for i in issues if i.is_open
                    and (target_model is None or i.model_id == target_model)]
        return "select_part" if relevant else "open_issue"
    if intent == "switch_issue":
        return "switch_issue"
    if intent == "new_issue":
        return "open_issue"
    return "diagnose"


def after_identify(s: GraphState) -> str:
    """Once identification lands, continue into the work the customer came for."""
    if s.get("reply"):
        # We asked a question (or reported a miss); the turn ends here.
        return END
    if nodes.needs_identification(s):
        return END

    issues = [IssueThread(**i) for i in s.get("issues", [])]
    if any(i.is_open for i in issues):
        return "diagnose"
    # Identified from a message that also said what the customer needs: open the
    # issue in the same turn rather than bouncing a question back at them.
    if s.get("_identified_now"):
        return "open_issue"
    return END


def _commerce_wants_part(s: GraphState) -> bool:
    return s.get("intent") == "commerce"


def after_diagnose(s: GraphState) -> str:
    if s.get("escalate"):
        return "handoff"
    issues = [IssueThread(**i) for i in s.get("issues", [])]
    active_id = s.get("active_issue_id")
    active = next((i for i in issues if i.id == active_id), None)
    # A diagnosis that lands on a failed component flows straight into the
    # parts path; the customer should not have to ask twice.
    if active and active.status == "awaiting_part":
        return "select_part"
    return END


def after_open_issue(s: GraphState) -> str:
    # An issue opened to service a purchase request goes straight to the parts
    # path; the customer has already told us what they want.
    if s.get("intent") == "commerce":
        return "select_part"
    return "diagnose"


def after_switch(s: GraphState) -> str:
    # "Switch" with nothing to switch to is really a new issue; see switch_issue.
    if s.get("_fallthrough_to_new_issue"):
        return "open_issue"
    # Resuming a thread just re-orients the customer; the next message drives it.
    return END


def after_select_part(s: GraphState) -> str:
    if s.get("escalate"):
        return "handoff"
    if s.get("_selected_part"):
        return "quote_part"
    return END


def after_quote(s: GraphState) -> str:
    return "handoff" if s.get("escalate") else END


def after_confirm(s: GraphState) -> str:
    return "handoff" if s.get("escalate") else END


# ---------------------------------------------------------------------------
# Small nodes
# ---------------------------------------------------------------------------

def chitchat(s: GraphState) -> dict:
    """Greetings and thanks. No model call — it would cost ten seconds to say hello."""
    text = s["customer_message"].lower()
    issues = [IssueThread(**i) for i in s.get("issues", [])]
    open_issues = [i for i in issues if i.is_open]

    if any(w in text for w in ("thank", "thanks", "cheers", "appreciate")):
        reply = ("You are very welcome. Anything else I can help with?"
                 if not open_issues else
                 f"You are welcome. We still have your {open_issues[0].title} open "
                 f"— shall we carry on with that?")
    elif any(w in text for w in ("bye", "goodbye", "that's all", "thats all")):
        reply = "Thanks for contacting FitForge. Take care."
    else:
        reply = ("Hello — I am the FitForge support assistant. "
                 "What is going on with your equipment?")

    return {"reply": reply, "trace": [*s.get("trace", []), "chitchat"]}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_graph():
    g = StateGraph(GraphState)

    g.add_node("precheck", nodes.precheck)
    g.add_node("route", nodes.route)
    g.add_node("identify", nodes.identify)
    g.add_node("open_issue", nodes.open_issue)
    g.add_node("switch_issue", nodes.switch_issue)
    g.add_node("diagnose", nodes.diagnose)
    g.add_node("select_part", commerce_nodes.select_part)
    g.add_node("quote_part", commerce_nodes.quote_part)
    g.add_node("confirm", commerce_nodes.handle_confirmation)
    g.add_node("handoff", commerce_nodes.escalate)
    g.add_node("chitchat", chitchat)

    g.add_edge(START, "precheck")
    g.add_conditional_edges("precheck", after_precheck,
                            {"route": "route", "handoff": "handoff"})
    g.add_conditional_edges("route", dispatch, {
        "identify": "identify", "open_issue": "open_issue",
        "switch_issue": "switch_issue", "diagnose": "diagnose",
        "select_part": "select_part", "confirm": "confirm",
        "chitchat": "chitchat", "handoff": "handoff",
    })
    g.add_conditional_edges("identify", after_identify,
                            {"diagnose": "diagnose", "open_issue": "open_issue",
                             END: END})
    g.add_conditional_edges("open_issue", after_open_issue,
                            {"diagnose": "diagnose",
                             "select_part": "select_part"})
    g.add_conditional_edges("switch_issue", after_switch,
                            {"open_issue": "open_issue", END: END})
    g.add_conditional_edges("diagnose", after_diagnose,
                            {"select_part": "select_part", "handoff": "handoff",
                             END: END})
    g.add_conditional_edges("select_part", after_select_part,
                            {"quote_part": "quote_part", "handoff": "handoff",
                             END: END})
    g.add_conditional_edges("quote_part", after_quote,
                            {"handoff": "handoff", END: END})
    g.add_conditional_edges("confirm", after_confirm,
                            {"handoff": "handoff", END: END})
    g.add_edge("handoff", END)
    g.add_edge("chitchat", END)

    return g


def get_graph():
    """Compile once, with a durable checkpointer when one is available."""
    global _compiled
    if _compiled is not None:
        return _compiled

    graph = build_graph()
    checkpointer = None
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver

        # A dedicated pool held at module scope. `from_conn_string` returns a
        # context manager whose connection is closed as soon as it is collected,
        # which surfaces later as "the connection is closed" mid-turn.
        # PostgresSaver also requires autocommit and dict rows.
        global _checkpoint_pool
        _checkpoint_pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row,
                    "prepare_threshold": 0},
            open=True,
        )
        checkpointer = PostgresSaver(_checkpoint_pool)
        checkpointer.setup()
        log.info("LangGraph checkpointer: Postgres")
    except Exception as exc:                            # noqa: BLE001
        # The system of record is the issue_threads table, so losing the
        # checkpointer costs mid-turn crash recovery and nothing else. Degrading
        # here beats refusing to serve.
        log.warning("Postgres checkpointer unavailable (%s); using in-memory", exc)
        try:
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
        except Exception:                               # pragma: no cover
            checkpointer = None

    _compiled = graph.compile(checkpointer=checkpointer)
    return _compiled


# ---------------------------------------------------------------------------
# Turn orchestration
# ---------------------------------------------------------------------------

def run_turn(session_id: str, customer_message: str) -> dict[str, Any]:
    """Process one customer message end to end.

    Loads the session's authoritative state, runs the graph, applies the
    post-turn escalation check, and persists the exchange.
    """
    session = state.load_session(session_id)
    if session is None:
        raise ValueError(f"unknown session {session_id}")

    issues = state.load_issues(session_id)
    verified = state.load_verified_models(session_id)
    active_id = state.load_active_issue_id(session_id)
    active = next((i for i in issues if i.id == active_id), None)

    state.add_message(session_id, role="customer", content=customer_message,
                      issue_id=active.id if active else None)

    initial: GraphState = {
        "session_id": session_id,
        "customer_id": str(session["customer_id"]) if session["customer_id"] else None,
        "customer_message": customer_message,
        "verified_models": [v.model_dump() for v in verified],
        "issues": [i.model_dump() for i in issues],
        "active_issue_id": active.id if active else None,
        "pending_confirmation": _load_pending(session_id),
        "intent": "",
        "safety_level": "ok",
        "reply": "",
        "citations": [],
        "escalate": None,
        "trace": [],
    }

    result = get_graph().invoke(
        initial, config={"configurable": {"thread_id": session_id}}
    )

    # --- post-turn escalation check --------------------------------------
    # Node-level checks catch the specific failures; this catches the cumulative
    # ones — a step budget quietly exhausted, frustration built up over several
    # turns — that no single node is positioned to see.
    if not result.get("escalate"):
        result = _post_turn_escalation(result, customer_message)

    reply = result.get("reply") or (
        "Sorry — I did not follow that. Could you tell me a bit more about what "
        "the machine is doing?"
    )

    issues_after = state.load_issues(session_id)
    active_after = next(
        (i for i in issues_after if i.id == result.get("active_issue_id")), None
    )
    state.add_message(
        session_id, role="agent", content=reply,
        issue_id=active_after.id if active_after else None,
        meta={"citations": result.get("citations", []),
              "trace": result.get("trace", []),
              "intent": result.get("intent")},
    )

    _save_pending(session_id, result.get("pending_confirmation"))

    return {
        "reply": reply,
        "citations": result.get("citations", []),
        "intent": result.get("intent"),
        "active_issue_id": result.get("active_issue_id"),
        "escalated": bool(result.get("escalate")),
        "escalation": result.get("escalate"),
        "requires_payment": bool(result.get("_request_payment")),
        "pending_confirmation": result.get("pending_confirmation"),
        "issues": [i.model_dump() for i in issues_after],
        "trace": result.get("trace", []),
    }


def _post_turn_escalation(result: GraphState, customer_message: str) -> GraphState:
    issues = [IssueThread(**i) for i in result.get("issues", [])]
    active_id = result.get("active_issue_id")
    active = next((i for i in issues if i.id == active_id), None)
    if active is None or active.is_terminal:
        return result

    session_id = result["session_id"]
    frustration = _count_frustration(session_id, customer_message)

    check = escalation.evaluate(
        step_budget_used=active.step_budget_used,
        tool_failures=active.tool_failures,
        frustration_signals=frustration,
    )
    if not check.escalate:
        return result

    result["escalate"] = {"reason": check.reason, "detail": check.detail}
    result["reply"] = (result.get("reply") or "") + "\n\n" + (check.customer_message or "")
    return commerce_nodes.escalate({**result, "reply": result["reply"].strip()})


def _count_frustration(session_id: str, current_message: str) -> int:
    history = state.recent_messages(session_id, limit=12)
    n = sum(1 for m in history
            if m["role"] == "customer" and escalation.detect_frustration(m["content"]))
    if escalation.detect_frustration(current_message):
        n += 1
    return n


# ---------------------------------------------------------------------------
# Pending confirmations
# ---------------------------------------------------------------------------
# Held in the quotes table rather than in graph state so a customer who says
# "yes" ten minutes later, after a reconnect, still gets the same quote.

def _load_pending(session_id: str) -> dict | None:
    from ..db import query_one

    row = query_one(
        """
        SELECT q.id, q.issue_id, q.part_number, q.total_cents, q.covered,
               q.confirmation_hash, q.status
          FROM quotes q
         WHERE q.session_id = %s AND q.status IN ('pending', 'confirmed')
           AND q.expires_at > now()
         ORDER BY q.created_at DESC LIMIT 1
        """,
        (session_id,),
    )
    if row is None:
        return None
    return {
        "quote_id": str(row["id"]),
        "issue_id": str(row["issue_id"]) if row["issue_id"] else None,
        "part_number": row["part_number"],
        "total_cents": row["total_cents"],
        "covered": row["covered"],
        "confirmation_hash": row["confirmation_hash"],
        "summary_shown": "",
        "awaiting": "payment" if row["status"] == "confirmed" else "confirm",
    }


def _save_pending(session_id: str, pending: dict | None) -> None:
    """No-op by design: quote status in the database is the source of truth."""
    return None


def start_session(customer_id: str | None = None) -> str:
    session_id = state.create_session(customer_id)
    audit("session_started", actor="system", session_id=session_id)
    return session_id

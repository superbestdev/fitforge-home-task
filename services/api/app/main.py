"""FastAPI application: customer chat, agent console, and operational endpoints.

The chat endpoint is a WebSocket because the diagnostic loop is inherently
turn-based and slow on CPU — the customer needs to see "working on it" rather
than watch a request hang. Escalations are pushed to the agent console over
Redis pub/sub so a queued handoff appears without polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from fastapi import (
    BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import settings
from .db import execute, healthy as db_healthy, query, query_one, run_migrations
from .agent import commerce_nodes, graph, state
from .tools import commerce, manuals

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("fitforge.api")

HANDOFF_CHANNEL = "fitforge:handoffs"

redis_client: aioredis.Redis | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await redis_client.ping()
        log.info("redis connected")
    except Exception as exc:                            # noqa: BLE001
        # The console falls back to polling; chat is unaffected.
        log.warning("redis unavailable (%s); console live updates disabled", exc)
        redis_client = None

    # Bring the schema up to date before anything queries it. Postgres only
    # runs the init scripts on an empty volume, so a new migration would
    # otherwise never be applied to an existing database.
    try:
        applied = await asyncio.to_thread(run_migrations)
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
    except Exception as exc:                            # noqa: BLE001
        log.error("migrations failed: %s", exc)

    # Compile the graph at startup so the first customer does not pay for it.
    await asyncio.to_thread(graph.get_graph)
    log.info("agent graph compiled")

    yield

    if redis_client is not None:
        await redis_client.aclose()


app = FastAPI(title="FitForge Agentic Support", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/deep")
async def health_deep() -> dict:
    from .agent.llm import healthy as llm_healthy
    from services.ingest.embed import healthy as embed_healthy

    db = await asyncio.to_thread(db_healthy)
    llm = await asyncio.to_thread(llm_healthy)
    emb = await asyncio.to_thread(embed_healthy)
    return {
        "status": "ok" if all([db, llm, emb]) else "degraded",
        "database": db, "llm": llm, "embeddings": emb,
        "redis": redis_client is not None,
        "model": settings.llm_model,
        "router_model": settings.llm_router_model,
    }


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class StartSessionRequest(BaseModel):
    customer_email: str | None = None


class StartSessionResponse(BaseModel):
    session_id: str
    greeting: str


GREETING = (
    "Hello, you have reached FitForge support. Tell me what is going on with "
    "your equipment and I will help you sort it out."
)


@app.post("/api/sessions", response_model=StartSessionResponse)
async def start_session(req: StartSessionRequest) -> StartSessionResponse:
    customer_id = None
    if req.customer_email:
        from .tools.identity import find_customer
        cust = await asyncio.to_thread(find_customer, email=req.customer_email)
        if cust:
            customer_id = str(cust["id"])

    session_id = await asyncio.to_thread(graph.start_session, customer_id)
    await asyncio.to_thread(state.add_message, session_id,
                            role="agent", content=GREETING)
    return StartSessionResponse(session_id=session_id, greeting=GREETING)


class ChatRequest(BaseModel):
    session_id: str
    message: str


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict:
    """Synchronous chat, used by the demo script and the evals."""
    try:
        result = await asyncio.to_thread(graph.run_turn, req.session_id, req.message)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if result.get("escalated"):
        await _publish_handoff(req.session_id, result.get("escalation") or {})
    return result


class PayRequest(BaseModel):
    session_id: str
    card_token: str | None = None
    # Demo/eval convenience only. In production the browser posts the card
    # straight to the PSP and hands us a token; this path exists so the flow can
    # be exercised headlessly.
    test_card: str | None = None


@app.post("/api/pay")
async def pay(req: PayRequest) -> dict:
    """Charge a tokenized card against the session's pending quote."""
    token = req.card_token
    if not token:
        if not req.test_card:
            raise HTTPException(status_code=400, detail="No payment token supplied")
        token = await asyncio.to_thread(commerce.tokenize_test_card, req.test_card)

    session = await asyncio.to_thread(state.load_session, req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No such session")

    issues = await asyncio.to_thread(state.load_issues, req.session_id)
    graph_state: dict[str, Any] = {
        "session_id": req.session_id,
        "customer_id": str(session["customer_id"]) if session["customer_id"] else None,
        "customer_message": "[card details submitted]",
        "pending_confirmation": graph._load_pending(req.session_id),
        "issues": [i.model_dump() for i in issues],
        "trace": [],
    }
    result = await asyncio.to_thread(commerce_nodes.take_payment, graph_state, token)

    reply = result.get("reply", "")
    await asyncio.to_thread(state.add_message, req.session_id,
                            role="agent", content=reply)

    issues_after = await asyncio.to_thread(state.load_issues, req.session_id)
    return {
        "reply": reply,
        "issues": [i.model_dump() for i in issues_after],
        "requires_payment": bool(result.get("_request_payment")),
        "trace": result.get("trace", []),
    }


@app.websocket("/ws/chat/{session_id}")
async def ws_chat(ws: WebSocket, session_id: str) -> None:
    """Customer chat socket.

    A `thinking` frame is sent before the turn runs. On CPU inference a turn can
    take 15-30 seconds, and silence for that long reads as a broken widget.
    """
    await ws.accept()

    session = await asyncio.to_thread(state.load_session, session_id)
    if session is None:
        await ws.send_json({"type": "error", "message": "Unknown session."})
        await ws.close()
        return

    history = await asyncio.to_thread(state.recent_messages, session_id, 50)
    await ws.send_json({
        "type": "history",
        "messages": [
            {"role": m["role"], "content": m["content"], "at": str(m["created_at"])}
            for m in history
        ],
    })

    try:
        while True:
            payload = await ws.receive_json()
            kind = payload.get("type", "message")

            if kind == "payment":
                await _handle_payment_frame(ws, session_id, payload)
                continue

            message = (payload.get("message") or "").strip()
            if not message:
                continue

            await ws.send_json({"type": "thinking"})

            try:
                result = await asyncio.to_thread(graph.run_turn, session_id, message)
            except Exception as exc:                    # noqa: BLE001
                log.exception("turn failed for session %s", session_id)
                await ws.send_json({
                    "type": "message",
                    "role": "agent",
                    "content": "Something went wrong on my side. Let me get a "
                               "colleague to pick this up.",
                    "error": str(exc)[:200],
                })
                continue

            await ws.send_json({
                "type": "message",
                "role": "agent",
                "content": result["reply"],
                "citations": result.get("citations", []),
                "issues": result.get("issues", []),
                "escalated": result.get("escalated", False),
                "requires_payment": result.get("requires_payment", False),
                "pending_confirmation": result.get("pending_confirmation"),
                "trace": result.get("trace", []),
            })

            if result.get("escalated"):
                await _publish_handoff(session_id, result.get("escalation") or {})

    except WebSocketDisconnect:
        log.info("customer disconnected from session %s", session_id)


async def _handle_payment_frame(ws: WebSocket, session_id: str,
                                payload: dict) -> None:
    """Take a PSP token from the widget and complete the order.

    The widget tokenizes directly with the payment service; what arrives here is
    an opaque token, never card data.
    """
    token = payload.get("card_token")
    if not token:
        card_number = payload.get("test_card")
        if not card_number:
            await ws.send_json({"type": "error", "message": "No payment token."})
            return
        # Demo convenience: tokenize on the customer's behalf. In production the
        # browser does this and the backend never sees a number at all.
        token = await asyncio.to_thread(commerce.tokenize_test_card, card_number)

    await ws.send_json({"type": "thinking"})

    session = await asyncio.to_thread(state.load_session, session_id)
    graph_state: dict[str, Any] = {
        "session_id": session_id,
        "customer_id": str(session["customer_id"]) if session["customer_id"] else None,
        "customer_message": "[card details submitted]",
        "pending_confirmation": graph._load_pending(session_id),
        "issues": [i.model_dump() for i in
                   await asyncio.to_thread(state.load_issues, session_id)],
        "trace": [],
    }
    result = await asyncio.to_thread(commerce_nodes.take_payment, graph_state, token)

    reply = result.get("reply", "")
    await asyncio.to_thread(state.add_message, session_id,
                            role="agent", content=reply)
    await ws.send_json({
        "type": "message", "role": "agent", "content": reply,
        "issues": [i.model_dump() for i in
                   await asyncio.to_thread(state.load_issues, session_id)],
        # Carried through so a declined card leaves the form up for a retry.
        "requires_payment": bool(result.get("_request_payment")),
        "trace": result.get("trace", []),
    })


# ---------------------------------------------------------------------------
# Agent console
# ---------------------------------------------------------------------------

@app.get("/api/handoffs")
async def list_handoffs(status: str = "queued", limit: int = 50) -> dict:
    rows = await asyncio.to_thread(
        query,
        """
        SELECT h.id, h.session_id, h.reason, h.detail, h.status, h.created_at,
               h.claimed_by,
               h.packet -> 'customer' ->> 'name'  AS customer_name,
               h.packet -> 'summary'  ->> 'text'  AS summary,
               jsonb_array_length(COALESCE(h.packet -> 'issues', '[]'::jsonb)) AS issue_count
          FROM handoffs h
         WHERE (%s = 'all' OR h.status = %s)
         ORDER BY h.created_at DESC LIMIT %s
        """,
        (status, status, limit),
    )
    return {"handoffs": [dict(r) for r in rows]}


@app.get("/api/handoffs/{handoff_id}")
async def get_handoff(handoff_id: str) -> dict:
    row = await asyncio.to_thread(
        query_one,
        "SELECT id, session_id, reason, detail, status, packet, created_at, "
        "claimed_by, claimed_at FROM handoffs WHERE id = %s",
        (handoff_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No such handoff")
    return dict(row)


class ClaimRequest(BaseModel):
    agent_name: str


@app.post("/api/handoffs/{handoff_id}/claim")
async def claim_handoff(handoff_id: str, req: ClaimRequest) -> dict:
    row = await asyncio.to_thread(
        execute,
        """
        UPDATE handoffs SET status = 'claimed', claimed_by = %s, claimed_at = now()
         WHERE id = %s AND status = 'queued'
        RETURNING id, session_id
        """,
        (req.agent_name, handoff_id),
    )
    if row is None:
        raise HTTPException(status_code=409, detail="Already claimed or missing")
    return {"handoff_id": str(row["id"]), "session_id": str(row["session_id"]),
            "claimed_by": req.agent_name}


@app.post("/api/handoffs/{handoff_id}/resolve")
async def resolve_handoff(handoff_id: str) -> dict:
    await asyncio.to_thread(
        execute, "UPDATE handoffs SET status = 'resolved' WHERE id = %s",
        (handoff_id,),
    )
    return {"handoff_id": handoff_id, "status": "resolved"}


@app.websocket("/ws/console")
async def ws_console(ws: WebSocket) -> None:
    """Push newly queued handoffs to the console."""
    await ws.accept()

    rows = await asyncio.to_thread(
        query,
        "SELECT id, session_id, reason, created_at FROM handoffs "
        "WHERE status = 'queued' ORDER BY created_at DESC LIMIT 50",
    )
    await ws.send_json({"type": "queue",
                        "handoffs": [dict(r) for r in rows]})

    if redis_client is None:
        # Without Redis the console still works; it just polls instead.
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            return

    pubsub = redis_client.pubsub()
    await pubsub.subscribe(HANDOFF_CHANNEL)
    try:
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            await ws.send_json({"type": "handoff", **json.loads(msg["data"])})
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(HANDOFF_CHANNEL)
        await pubsub.aclose()


async def _publish_handoff(session_id: str, escalation_info: dict) -> None:
    if redis_client is None:
        return
    try:
        await redis_client.publish(HANDOFF_CHANNEL, json.dumps({
            "session_id": session_id,
            "handoff_id": escalation_info.get("handoff_id"),
            "reason": escalation_info.get("reason"),
            "detail": escalation_info.get("detail"),
        }, default=str))
    except Exception:                                   # pragma: no cover
        log.debug("could not publish handoff", exc_info=True)


# ---------------------------------------------------------------------------
# Manual upload
# ---------------------------------------------------------------------------
# The operational other half of the cold-start design: the coverage registry
# says which models are undocumented, and these endpoints are how that gets
# fixed. Uploads run the same pipeline as the seeded corpus — an uploaded scan
# earns exactly the same OCR confidence penalty as a seeded one.

@app.post("/api/manuals/upload")
async def upload_manual(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    model_id: str | None = Form(default=None),
    uploaded_by: str | None = Form(default=None),
) -> dict:
    """Accept a service manual and queue it for ingestion.

    Returns immediately with a job id. Ingestion is slow — a scanned manual
    costs seconds to minutes of OCR — so the console polls the job rather than
    holding a request open.
    """
    content = await file.read()

    try:
        job = await asyncio.to_thread(
            manuals.create_job,
            filename=file.filename or "manual.pdf",
            content=content,
            model_id=(model_id or None),
            uploaded_by=uploaded_by,
        )
    except manuals.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    background.add_task(_run_ingest_job, job["job_id"])
    return {**job, "status": "queued"}


async def _run_ingest_job(job_id: str) -> None:
    """Run an ingestion job off the request path."""
    try:
        await asyncio.to_thread(manuals.process_job, job_id)
    except Exception:                                   # pragma: no cover
        log.exception("background ingest job %s crashed", job_id)


@app.get("/api/manuals/jobs")
async def list_ingest_jobs(limit: int = 30) -> dict:
    return {"jobs": await asyncio.to_thread(manuals.list_jobs, limit)}


@app.get("/api/manuals/jobs/{job_id}")
async def get_ingest_job(job_id: str) -> dict:
    job = await asyncio.to_thread(manuals.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    return job


class AssignModelRequest(BaseModel):
    model_id: str


@app.post("/api/manuals/jobs/{job_id}/model")
async def assign_job_model(job_id: str, req: AssignModelRequest,
                           background: BackgroundTasks) -> dict:
    """Resolve a job that could not identify its own model, and resume it."""
    try:
        await asyncio.to_thread(manuals.assign_model, job_id, req.model_id)
    except manuals.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    background.add_task(_run_ingest_job, job_id)
    return {"job_id": job_id, "model_id": req.model_id, "status": "queued"}


@app.get("/api/manuals/backfill")
async def manual_backfill_queue(limit: int = 100) -> dict:
    """Models that need a manual, most-used first."""
    return {"models": await asyncio.to_thread(manuals.backfill_queue, limit)}


@app.get("/api/models/search")
async def model_search(q: str = "", limit: int = 20) -> dict:
    return {"models": await asyncio.to_thread(manuals.search_models, q, limit)}


# ---------------------------------------------------------------------------
# Operations / observability
# ---------------------------------------------------------------------------

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    session = await asyncio.to_thread(state.load_session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No such session")
    issues = await asyncio.to_thread(state.load_issues, session_id)
    verified = await asyncio.to_thread(state.load_verified_models, session_id)
    messages = await asyncio.to_thread(state.recent_messages, session_id, 200)
    return {
        "session": dict(session),
        "issues": [i.model_dump() for i in issues],
        "verified_models": [v.model_dump() for v in verified],
        "messages": [{"role": m["role"], "content": m["content"],
                      "at": str(m["created_at"])} for m in messages],
    }


@app.get("/api/metrics")
async def metrics() -> dict:
    """The signals that tell you whether the agent is actually working.

    Deliberately small. These are the numbers worth waking up for; everything
    else is in Langfuse. See docs/06-observability.md.
    """
    rows = await asyncio.to_thread(query, """
        WITH sess AS (
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status = 'escalated') AS escalated
              FROM sessions
        ),
        iss AS (
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status = 'resolved') AS resolved,
                   count(*) FILTER (WHERE status = 'escalated') AS escalated,
                   COALESCE(avg(step_budget_used) FILTER (WHERE status = 'resolved'), 0)
                     AS avg_steps_to_resolve
              FROM issue_threads
        ),
        llm AS (
            SELECT count(*) AS calls,
                   COALESCE(sum(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(sum(completion_tokens), 0) AS completion_tokens,
                   COALESCE(avg(latency_ms), 0) AS avg_latency_ms,
                   count(*) FILTER (WHERE NOT ok) AS failures
              FROM llm_calls
        ),
        cov AS (
            SELECT count(*) FILTER (WHERE status = 'backed')   AS backed,
                   count(*) FILTER (WHERE status = 'degraded') AS degraded,
                   count(*) FILTER (WHERE status = 'unbacked') AS unbacked
              FROM coverage_registry
        )
        SELECT row_to_json(sess) AS sessions, row_to_json(iss) AS issues,
               row_to_json(llm) AS llm, row_to_json(cov) AS coverage
          FROM sess, iss, llm, cov
    """)
    base = dict(rows[0]) if rows else {}

    reasons = await asyncio.to_thread(query, """
        SELECT reason, count(*) AS n FROM handoffs
         GROUP BY reason ORDER BY n DESC
    """)
    base["escalation_reasons"] = [dict(r) for r in reasons]

    per_node = await asyncio.to_thread(query, """
        SELECT node, count(*) AS calls,
               round(avg(latency_ms)) AS avg_ms,
               sum(prompt_tokens) AS prompt_tokens,
               sum(completion_tokens) AS completion_tokens
          FROM llm_calls GROUP BY node ORDER BY calls DESC
    """)
    base["llm_by_node"] = [dict(r) for r in per_node]

    sessions = (base.get("sessions") or {}).get("total") or 0
    escalated = (base.get("sessions") or {}).get("escalated") or 0
    # Containment: the fraction of sessions the agent finished without a human.
    # The single number this system lives or dies by.
    base["containment_rate"] = (
        round(1 - escalated / sessions, 3) if sessions else None
    )
    return base


@app.get("/api/coverage")
async def coverage(status: str | None = None, limit: int = 100) -> dict:
    rows = await asyncio.to_thread(query, """
        SELECT c.model_id, m.name, c.status, c.chunk_count, c.quality_score,
               c.sections_present, c.notes, mn.source_type, mn.ocr_applied
          FROM coverage_registry c
          JOIN models m ON m.id = c.model_id
          LEFT JOIN manuals mn ON mn.id = c.manual_id
         WHERE (%s::text IS NULL OR c.status = %s)
         ORDER BY c.quality_score ASC, c.model_id
         LIMIT %s
    """, (status, status, limit))
    return {"coverage": [dict(r) for r in rows]}

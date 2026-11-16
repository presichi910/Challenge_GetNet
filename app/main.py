"""FastAPI app. POST /chat with {"message"} and `Authorization: Bearer <jwt>`.

The customer is the JWT `sub`. A body `user_id` is optional and must match it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response

from app.auth import (Principal, open_persistent_denylist, require_merchant, require_operator, reset_auth_state,
                      revoke_token, validate_auth_config)
from app.config import get_settings
from app.handoff_store import HandoffConflict, HandoffNotFound, HandoffStore, iso
from app.memory import Memory, ThreadLimitExceeded
from app.approvals import ApprovalConflict, ApprovalNotFound, ApprovalService
from app.schemas import (ApprovalDecisionRequest, ApprovalList, ApprovalStatusName, ApprovalSummary, ChatRequest, ChatResponse,
                         ConversationDetail, ConversationList, HandoffDetail, HandoffList, HandoffStatusName, HandoffSummary,
                         MetricsResponse, OperatorMessageList, RejectRequest, ReplyRequest, ReplyResult, RevokeRequest,
                         is_valid_approval_id, is_valid_conversation_id, is_valid_handoff_id)
from app.service import ChatService, new_conversation_id

log = logging.getLogger("api")


def _warn_if_unimported(path: str) -> None:
    """HANDOFF_IMPORT_JSONL is off: say so once if the old file holds records that were never imported."""
    try:
        with open(path, encoding="utf-8") as f:
            n = sum(1 for line in f if line.strip() and '"redacted"' not in line)
    except OSError:
        return
    if n:
        log.warning("%s has %d handoff records that are NOT in the operator queue (HANDOFF_IMPORT_JSONL is off). "
                    "Back it up and clean it, then set HANDOFF_IMPORT_JSONL=true to import it once.", path, n)


def _purge_once(memory: Memory, handoffs: HandoffStore | None, traces=None, approvals=None) -> None:
    """One retention pass: idle threads, then idle handoffs, then old traces, then old approval requests (same THREAD_TTL_DAYS).
    One failing does not skip the others."""
    try:
        memory.purge_expired()
    finally:
        try:
            if handoffs is not None:
                handoffs.purge_expired(memory.ttl_days)
        finally:
            try:
                if traces is not None:
                    traces.purge_expired(memory.ttl_days)
            finally:
                if approvals is not None:
                    approvals.store.purge_expired(memory.ttl_days)


async def _purge_loop(memory: Memory, every_seconds: float, handoffs: HandoffStore | None = None,
                      traces=None, approvals=None) -> None:
    """Retention: delete idle threads, handoffs and old traces now and then. A failed pass is logged and tried again."""
    while True:
        await asyncio.sleep(every_seconds)
        try:
            await asyncio.to_thread(_purge_once, memory, handoffs, traces, approvals)
        except Exception:  # noqa: BLE001 - never kill the loop
            log.exception("Thread purge failed")


def sweep_once(approvals: ApprovalService | None, handoffs: HandoffStore | None, sla_seconds: float, webhook=None, telemetry=None) -> list[dict]:
    """The time limits, once: a ticket request nobody decided expires (no ticket, the customer is told, the case goes to the human queue), a decided one
    whose graph could not be continued is finished, and a handoff nobody claimed within the SLA is escalated and announced. Each part is independent: one
    failing does not skip the others. -> the handoffs escalated now."""
    escalated: list[dict] = []
    try:
        if approvals is not None:
            approvals.sweep()
    except Exception:  # noqa: BLE001
        log.exception("Approval sweep failed")
    try:
        if handoffs is not None and sla_seconds:
            escalated = handoffs.escalate_overdue(sla_seconds)
            for row in escalated:
                if webhook is not None:
                    webhook.send({"event": "handoff.sla_breached", "handoff_id": row["id"], "reason": row["reason"], "priority": row["priority"],
                                  "conversation_id": row["conversation_id"], "user_id": row["merchant_id"], "waited_seconds": row["waited_s"]})
    except Exception:  # noqa: BLE001
        log.exception("Handoff SLA sweep failed")
    try:
        if telemetry is not None and telemetry.metrics is not None:
            if escalated:
                telemetry.metrics.sla_breaches.inc(len(escalated))
            telemetry.metrics.set_queues(
                pending_handoffs=len(handoffs.list_queue("pending", 500)) if handoffs is not None else None,
                pending_approvals=len(approvals.store.list("pending", 500)) if approvals is not None else None, webhook=webhook)
    except Exception:  # noqa: BLE001
        log.exception("Queue gauges failed")
    return escalated


async def _approvals_loop(approvals: ApprovalService, every_seconds: float = 30.0, handoffs: HandoffStore | None = None,
                          sla_seconds: float = 0.0, webhook=None, telemetry=None) -> None:
    """Every 30 s (and on every operator call for the approvals): the time limits, see `sweep_once`."""
    while True:
        await asyncio.sleep(every_seconds)
        try:
            await asyncio.to_thread(sweep_once, approvals, handoffs, sla_seconds, webhook, telemetry)
        except Exception:  # noqa: BLE001 - never kill the loop
            log.exception("Sweep failed")


def validate_single_worker() -> None:
    """The API keeps state in this process (rate limit, key cache) and in one SQLite file (conversations): ONE worker.
    Docker pins `--workers 1`; this also refuses a start through WEB_CONCURRENCY (uvicorn's own variable)."""
    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    if raw.isdigit() and int(raw) > 1:
        raise RuntimeError(f"WEB_CONCURRENCY={raw}: this API must run with ONE worker. The rate limit and the key cache live in "
                           "the process and the conversation checkpointer is one SQLite file (see README, Production notes).")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=get_settings().log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    validate_auth_config()  # fail fast: never serve without a complete, consistent auth setup
    validate_single_worker()
    memory = None
    handoffs = None
    traces = None
    approvals = None
    webhook = None
    telemetry = None
    purge_task = None
    approvals_task = None
    if getattr(app.state, "service", None) is None:  # tests set their own service
        from app.graph import build_default_graph
        from app.handoff import AuditNotifier, FanOutNotifier
        from app.memory import open_memory
        from app.rag.store import collection_size

        s = get_settings()
        log.info("Revoked tokens still in force: %d (%s)", open_persistent_denylist(s.revocations_db_path), s.revocations_db_path)
        memory = open_memory(s.checkpoint_db_path, ttl_days=s.thread_ttl_days,
                             max_threads_per_user=s.max_threads_per_user)
        # the human queue: its own SQLite file. The JSONL stays as an audit copy, and what an older
        # version left in it is imported (once: running it again adds nothing)
        handoffs = HandoffStore(s.handoffs_db_path, lease_seconds=s.handoff_lease_minutes * 60)
        if s.handoff_import_jsonl:
            try:
                handoffs.import_jsonl(s.handoffs_file)
            except Exception:  # noqa: BLE001 - an import problem must not stop the API
                log.exception("Handoff import from %s failed", s.handoffs_file)
        else:
            _warn_if_unimported(s.handoffs_file)
        if s.webhook_url:  # push to the operators: signed, queued, never in the customer's way (app/webhook.py)
            from app.webhook import WebhookNotifier

            try:
                webhook = WebhookNotifier(s.webhook_url, s.webhook_signing_key, timeout=s.webhook_timeout_seconds, retries=s.webhook_retries)
                log.info("Operator webhook on: %s", webhook.host)
            except ValueError as e:
                raise RuntimeError(f"webhook not started: {e}") from e  # a half-configured push is a startup error, not a silent no-op
        notifier = FanOutNotifier(handoffs, AuditNotifier(s.handoffs_file), *([webhook] if webhook else []))
        try:  # traces are optional: without the file the API still answers, just without stored traces
            from app.observability import TraceStore

            traces = TraceStore(s.traces_db_path)
        except Exception:  # noqa: BLE001
            log.exception("Trace store %s could not be opened; traces will not be stored", s.traces_db_path)
        from app.approvals import ApprovalStore, build_approval_graph
        from app.data.repository import CustomerRepository

        repo = CustomerRepository(tickets_file=s.tickets_file, dedup_window_s=s.ticket_dedup_window_s)  # one: the agents and the approval graph
        approval_store = ApprovalStore(s.approvals_db_path)
        approvals = ApprovalService(approval_store, build_approval_graph(approval_store, repo, notifier=notifier))
        from app.telemetry import Metrics, Telemetry, Tracing
        from app.webhook import approval_event

        tracing = None
        if s.otlp_endpoint:  # traces are optional: a collector that is down must not stop the API
            try:
                tracing = Tracing.from_endpoint(s.otlp_endpoint, s.otel_service_name)
                log.info("Traces go to %s", s.otlp_endpoint)
            except Exception:  # noqa: BLE001
                log.exception("OTLP tracing could not start; continuing without traces")
        telemetry = Telemetry(Metrics(), tracing)
        app.state.telemetry = telemetry
        app.state.service = ChatService(build_default_graph(checkpointer=memory.saver, handoff_notifier=notifier, repo=repo,
                                                            approvals=approval_store if s.approval_categories else None,
                                                            on_approval_created=(lambda row: webhook.send(approval_event(row))) if webhook else None),
                                        memory=memory, handoffs=handoffs, traces=traces, approvals=approvals, telemetry=telemetry)
        try:  # requests that were waiting when the process stopped: their paused graphs are still in the file; finish what is decided
            approvals.reconcile()
            log.info("Ticket approvals: %d pending (required for: %s; limit %.0f min)", len(approval_store.list("pending", 500)),
                     ", ".join(sorted(s.approval_categories)) or "none", s.approval_ttl_minutes)
        except Exception:  # noqa: BLE001 - never stop the API for it
            log.exception("Approval reconcile at startup failed")
        approvals_task = asyncio.create_task(_approvals_loop(approvals, handoffs=handoffs, sla_seconds=s.handoff_sla_minutes * 60, webhook=webhook,
                                                                telemetry=telemetry))
        log.info("Conversation memory: %s (ttl %s days, max %s threads per user)", memory.path,
                 s.thread_ttl_days or "off", s.max_threads_per_user or "unlimited")
        if s.thread_ttl_days:
            try:
                _purge_once(memory, handoffs, traces, approvals)  # at startup
            except Exception:  # noqa: BLE001 - a purge problem must not stop the API
                log.exception("Startup thread purge failed")
            purge_task = asyncio.create_task(_purge_loop(memory, s.purge_interval_minutes * 60, handoffs, traces, approvals))
        n = collection_size()
        if n == 0:
            log.warning("Knowledge base is EMPTY. Run `python -m app.rag.ingest` "
                        "(docker: `docker compose run --rm ingest`).")
        else:
            log.info("Knowledge base ready: %d chunks", n)
    try:
        yield
    finally:
        for task in (purge_task, approvals_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if memory is not None:
            reset_auth_state()  # closes the revocations file (the API opened it; tests never do)
            memory.close()  # flush the WAL, release the file
        if handoffs is not None:
            handoffs.close()
        if traces is not None:
            traces.close()
        if approvals is not None:
            approvals.store.close()
        if webhook is not None:
            webhook.close()
        if telemetry is not None:
            telemetry.close()


app = FastAPI(title="Getnet Multi-Agent Support", version="1.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    from app.rag.store import collection_size

    return {"status": "ok", "kb_chunks": collection_size()}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, principal: Principal = Depends(require_merchant)) -> ChatResponse:
    # the customer is the token's `sub`, a body user_id is never trusted
    if req.user_id is not None and req.user_id != principal.user_id:
        raise HTTPException(status_code=403, detail="user_id does not match the authenticated user.")
    service = app.state.service
    conversation_id = req.conversation_id
    if conversation_id is None:
        conversation_id = new_conversation_id()  # a new conversation: the server picks the id and returns it
    elif not service.conversation_known(principal.user_id, conversation_id):
        raise _not_found()  # not an id this customer was given: the same 404 as somebody else's or a missing one
    try:
        return service.handle(req.message, principal.user_id, conversation_id=conversation_id)
    except ThreadLimitExceeded as e:  # a new conversation above the per-user limit; nothing was deleted
        raise HTTPException(status_code=429, detail=str(e)) from e
    except Exception as e:
        log.exception("Unhandled error in /chat")
        raise HTTPException(status_code=500, detail="Internal error while processing the request.") from e


# ---- the caller's own conversations. Same auth as /chat. The owner comes from the token only: the
# path holds a conversation_id, never a user. Somebody else's, a missing one and a malformed id all
# answer the same 404, so the API never says whether a conversation exists. ----

def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Conversation not found.")


@app.get("/conversations", response_model=ConversationList)
def list_conversations(principal: Principal = Depends(require_merchant)) -> ConversationList:
    items = app.state.service.list_conversations(principal.user_id)
    return ConversationList(conversations=items, count=len(items), limit=app.state.service.conversation_limit())


@app.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, principal: Principal = Depends(require_merchant)) -> ConversationDetail:
    found = None
    if is_valid_conversation_id(conversation_id):
        found = app.state.service.get_conversation(principal.user_id, conversation_id)
    if found is None:
        raise _not_found()
    return ConversationDetail(**found)


@app.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, principal: Principal = Depends(require_merchant)) -> Response:
    deleted = False
    if is_valid_conversation_id(conversation_id):
        deleted = app.state.service.delete_conversation(principal.user_id, conversation_id)
    if not deleted:
        raise _not_found()
    log.info("conversation deleted user=%s conv=%s", principal.user_id, conversation_id)
    return Response(status_code=204)


@app.get("/conversations/{conversation_id}/operator-messages", response_model=OperatorMessageList)
def conversation_operator_messages(conversation_id: str,
                                   principal: Principal = Depends(require_merchant)) -> OperatorMessageList:
    """What a human operator answered in this conversation. The same messages also arrive once, in
    `operator_messages` of the next /chat response; this endpoint lets the customer read them (again)
    without sending a turn. The owner is the token sub: somebody else's conversation is the same 404."""
    found = None
    if is_valid_conversation_id(conversation_id):
        found = app.state.service.operator_messages(principal.user_id, conversation_id)
    if found is None:
        raise _not_found()
    return OperatorMessageList(conversation_id=conversation_id, messages=found)


# ---- human oversight: /operator/*. Only role operator (a merchant token is 403, whatever its scopes).
# An operator sees every merchant's handoffs, that is the job; each action is logged with the operator
# id and stored in handoff_events. ----

def _queue() -> HandoffStore:
    service = getattr(app.state, "service", None)
    store = getattr(service, "handoffs", None)
    if store is None:
        raise HTTPException(status_code=503, detail="The handoff queue is not available.")
    return store


def _handoff_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Handoff not found.")


def _summary_fields(row: dict) -> dict:
    return {**{k: row[k] for k in ("id", "conversation_id", "merchant_id", "reason", "priority", "summary",
                                   "status", "claimed_by", "resolution")},
            "claimed_at": iso(row["claimed_at"]), "lease_expires_at": iso(row["lease_expires_at"]),
            "resolved_at": iso(row["resolved_at"]),
            "created_at": iso(row["created_at"]), "escalated_at": iso(row["escalated_at"])}


def _detail(row: dict) -> dict:
    return {**_summary_fields(row), "operator_reply": row["operator_reply"], "record": row["record"]}


@app.get("/operator/handoffs", response_model=HandoffList)
def operator_list(status: HandoffStatusName | None = None, limit: int = Query(100, ge=1, le=500),
                  operator: Principal = Depends(require_operator)) -> HandoffList:
    """The queue, oldest first. `?status=pending` is what is waiting for somebody."""
    rows = _queue().list_queue(status, limit)
    return HandoffList(handoffs=[HandoffSummary(**_summary_fields(r)) for r in rows], count=len(rows))


@app.get("/operator/handoffs/{handoff_id}", response_model=HandoffDetail)
def operator_get(handoff_id: str, operator: Principal = Depends(require_operator)) -> HandoffDetail:
    row = _queue().get(handoff_id) if is_valid_handoff_id(handoff_id) else None
    if row is None:
        raise _handoff_not_found()
    log.info("handoff read id=%s operator=%s", handoff_id, operator.user_id)
    return HandoffDetail(**_detail(row))


@app.post("/operator/handoffs/{handoff_id}/claim", response_model=HandoffDetail)
def operator_claim(handoff_id: str, operator: Principal = Depends(require_operator)) -> HandoffDetail:
    """pending -> claimed. Claiming what you already hold is fine (same answer); someone else's is 409."""
    if not is_valid_handoff_id(handoff_id):
        raise _handoff_not_found()
    try:
        return HandoffDetail(**_detail(_queue().claim(handoff_id, operator.user_id)))
    except HandoffNotFound as e:
        raise _handoff_not_found() from e
    except HandoffConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@app.post("/operator/handoffs/{handoff_id}/reply", response_model=ReplyResult)
def operator_reply(handoff_id: str, req: ReplyRequest, operator: Principal = Depends(require_operator)) -> ReplyResult:
    """claimed -> resolved, only by the operator who claimed it. The message reaches the customer's
    conversation once, however many times the same idempotency_key is sent."""
    if not is_valid_handoff_id(handoff_id):
        raise _handoff_not_found()
    try:
        row, replayed = _queue().reply(handoff_id, operator.user_id, req.decision, req.message, req.idempotency_key)
    except HandoffNotFound as e:
        raise _handoff_not_found() from e
    except HandoffConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return ReplyResult(**_detail(row), replayed=replayed)


@app.post("/operator/handoffs/{handoff_id}/reject", response_model=HandoffDetail)
def operator_reject(handoff_id: str, req: RejectRequest, operator: Principal = Depends(require_operator)) -> HandoffDetail:
    """Give a case up. Only with a claim that is still valid (409 otherwise). `close: false` (default)
    puts it back in the queue for another operator; `close: true` closes it without a message to the
    customer. `reason` is recorded in the audit trail and is never shown to the customer."""
    if not is_valid_handoff_id(handoff_id):
        raise _handoff_not_found()
    try:
        return HandoffDetail(**_detail(_queue().release(handoff_id, operator.user_id, req.reason, req.close)))
    except HandoffNotFound as e:
        raise _handoff_not_found() from e
    except HandoffConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


# ---- ticket approvals: a ticket the agent asked for is opened only when an operator approves it ----

def _approval_service() -> ApprovalService:
    service = getattr(app.state, "service", None)
    svc = getattr(service, "approvals", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Ticket approvals are not available.")
    return svc


def _approval_summary(row: dict) -> ApprovalSummary:
    return ApprovalSummary(
        id=row["id"], conversation_id=row["conversation_id"], merchant_id=row["user_id"], category=row["category"],
        summary=row["summary"], status=row["status"], created_at=iso(row["created_at"]) or "", expires_at=iso(row["expires_at"]) or "",
        decided_by=row["decided_by"], decided_at=iso(row["decided_at"]), reason=row["reason"], ticket_id=row["ticket_id"])


@app.get("/operator/approvals", response_model=ApprovalList)
def operator_approvals(status: ApprovalStatusName | None = None, limit: int = Query(100, ge=1, le=500),
                       operator: Principal = Depends(require_operator)) -> ApprovalList:
    """Ticket requests, oldest first. `?status=pending` is what waits for a decision. Listing also expires what ran out of time."""
    svc = _approval_service()
    svc.sweep()
    rows = svc.store.list(status, limit)
    return ApprovalList(approvals=[_approval_summary(r) for r in rows], count=len(rows))


def _decide(approval_id: str, decision: str, req: ApprovalDecisionRequest, operator: Principal) -> ApprovalSummary:
    if not is_valid_approval_id(approval_id):
        raise HTTPException(status_code=404, detail="Approval request not found.")
    try:
        return _approval_summary(_approval_service().decide(approval_id, operator.user_id, decision, req.reason))
    except ApprovalNotFound as e:
        raise HTTPException(status_code=404, detail="Approval request not found.") from e
    except ApprovalConflict as e:  # decided already (by anyone, including a replay of this call), or out of time
        raise HTTPException(status_code=409, detail=str(e)) from e


@app.post("/operator/approvals/{approval_id}/approve", response_model=ApprovalSummary)
def operator_approve(approval_id: str, req: ApprovalDecisionRequest | None = None,
                     operator: Principal = Depends(require_operator)) -> ApprovalSummary:
    """pending -> approved: the ticket is opened (once, whatever happens next) and the customer is told on their next turn."""
    return _decide(approval_id, "approved", req or ApprovalDecisionRequest(), operator)


@app.post("/operator/approvals/{approval_id}/reject", response_model=ApprovalSummary)
def operator_reject_approval(approval_id: str, req: ApprovalDecisionRequest | None = None,
                             operator: Principal = Depends(require_operator)) -> ApprovalSummary:
    """pending -> rejected: no ticket is opened. `reason` is for the audit trail and is never shown to the customer."""
    return _decide(approval_id, "rejected", req or ApprovalDecisionRequest(), operator)


@app.post("/operator/revocations", status_code=204)
def operator_revoke(req: RevokeRequest, operator: Principal = Depends(require_operator)) -> Response:
    """Stop accepting one token (by its `jti`) before it expires. Takes effect on the next request; kept in memory."""
    revoke_token(req.jti)
    log.warning("token revoked operator=%s jti=%s", operator.user_id, req.jti[:12])
    return Response(status_code=204)


# ---- metrics for Prometheus: GET /metrics, only with METRICS_TOKEN set and sent as a bearer. It exposes no text and no customer id ----

@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(authorization: str | None = Header(default=None)) -> Response:
    import hmac

    token = get_settings().metrics_token
    if not token:
        raise HTTPException(status_code=404)  # no token configured: the endpoint does not exist
    given = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="Invalid metrics token.", headers={"WWW-Authenticate": "Bearer"})
    telemetry = getattr(app.state, "telemetry", None)
    if telemetry is None or telemetry.metrics is None:
        raise HTTPException(status_code=503, detail="Metrics are not available.")
    from app.telemetry import CONTENT_TYPE

    return Response(content=telemetry.metrics.render(), media_type=CONTENT_TYPE)


# ---- observability: aggregates of the stored traces (no conversation text is stored or returned) ----

def _epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


@app.get("/operator/metrics", response_model=MetricsResponse)
def operator_metrics(since: datetime | None = None, until: datetime | None = None,
                     operator: Principal = Depends(require_operator)) -> MetricsResponse:
    """Requests, latency p50/p95, degraded and handoff rates, provider errors, and tokens and estimated cost
    per node and per model, over the turns with since <= time < until (ISO 8601; no zone = UTC)."""
    service = getattr(app.state, "service", None)
    found = service.metrics(_epoch(since), _epoch(until)) if service is not None else None
    if found is None:
        raise HTTPException(status_code=503, detail="Metrics are not available (no trace store).")
    return MetricsResponse(**found, since=since.isoformat() if since else None,
                           until=until.isoformat() if until else None)

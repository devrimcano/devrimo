"""Authenticate assistant commands, enqueue them, and replay durable events."""

import asyncio
import hashlib
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import manager
from app.agents.scholar.context import build_run_dependencies
from app.agents.scholar.intent import grants_expand
from app.agents.store import get_agno_db
from app.assistant.models import AssistantRun
from app.assistant.queue import enqueue_run, get_owned_run, request_cancel, stream_events
from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.db.models import ChatSession
from app.db.session import get_db
from app.observability.client import capture
from app.observability.context import current_request_id
from app.schemas import ChatCompletionsRequestIn, ChatConfirmationIn
from app.workspace.approvals import issue_approval

router = APIRouter()


def _reject(reason: str, user_id, *, kind: str, **properties) -> None:
    capture("chat_turn_rejected", distinct_id=str(user_id), reason=reason, turn_kind=kind, **properties)


def _response(run, user_id, after=0):
    return StreamingResponse(
        stream_events(run.id, user_id, after),
        media_type="text/event-stream",
        headers={"X-Run-ID": str(run.id), "X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


async def _get_or_create_chat_session(db, user_id, agent_id, session_id):
    session_id = session_id or str(uuid4())
    await db.execute(
        insert(ChatSession)
        .values(id=session_id, user_id=user_id, agent_id=agent_id, agno_session_id=session_id)
        .on_conflict_do_nothing(index_elements=[ChatSession.id])
    )
    row = await db.get(ChatSession, session_id)
    if row.user_id != user_id:
        raise HTTPException(404, "Chat session not found")
    row.deleted_at = None
    await db.commit()
    return row


@router.post("/completions")
async def chat_completions(
    body: ChatCompletionsRequestIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    text = next((m.content for m in reversed(body.messages) if m.role == "user"), None)
    if not text:
        _reject("empty_message", user.id, kind="chat_turn")
        raise HTTPException(400, "No user message to respond to")
    key = body.idempotency_key or request.headers.get("Idempotency-Key") or str(uuid4())
    existing = await db.scalar(
        select(AssistantRun).where(AssistantRun.user_id == user.id, AssistantRun.idempotency_key == key)
    )
    session_id = body.session_id or (existing.session_id if existing else None)
    agent = await manager.get_or_create_agent(db, user.id)
    await manager.ensure_running(db, agent)
    session = await _get_or_create_chat_session(db, user.id, agent.id, session_id)
    # "Hepsini göster" is honored only when the student's own message asks for
    # it and does not refuse it. A previous offer is not consent, and a model
    # that decides to fetch everything on its own pays for it on every step.
    dependencies = await build_run_dependencies(
        db,
        user.id,
        message=text,
        expand_allowed=grants_expand(text),
    )
    try:
        run = await enqueue_run(
            db,
            user_id=user.id,
            session_id=session.id,
            kind="chat",
            payload={
                "text": text,
                "dependencies": dependencies,
                "agno_session_id": session.agno_session_id,
                "request_id": current_request_id.get(),
            },
            access_token=request.headers["authorization"].split(" ", 1)[1],
            idempotency_key=key,
        )
    except HTTPException as exc:
        _reject("agent_busy" if exc.status_code == 409 else "setup_rejected", user.id, kind="chat_turn")
        raise
    return _response(run, user.id)


@router.post("/confirmations")
async def confirm_tool_call(
    body: ChatConfirmationIn,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == body.session_id, ChatSession.user_id == user.id, ChatSession.deleted_at.is_(None)
        )
    )
    if session is None:
        raise HTTPException(404, "Chat session not found")
    # A lost response must resume the accepted decision, never approve or execute it twice.
    key = "confirmation:" + hashlib.sha256(f"{body.run_id}:{body.requirement_id}".encode()).hexdigest()
    existing = await db.scalar(
        select(AssistantRun).where(AssistantRun.user_id == user.id, AssistantRun.idempotency_key == key)
    )
    if existing:
        if existing.session_id != session.id or existing.payload.get("approved") != body.approved:
            raise HTTPException(409, "This confirmation already has a different decision")
        return _response(existing, user.id)
    agno_session_id = session.agno_session_id or session.id
    output = await asyncio.to_thread(get_agno_db().get_run, body.run_id)
    if output is None or output.user_id != str(user.id) or output.session_id != agno_session_id or not output.is_paused:
        raise HTTPException(404, "Paused run not found")
    pending = [item for item in output.active_requirements if item.needs_confirmation]
    if len(pending) != 1:
        raise HTTPException(409, "Ask the agent to perform one external action at a time")
    requirement = pending[0]
    if requirement.id != body.requirement_id:
        raise HTTPException(409, "Confirmation is no longer pending")
    payload = {
        "run_id": body.run_id,
        "requirement_id": body.requirement_id,
        "approved": body.approved,
        "agno_session_id": agno_session_id,
        "dependencies": await build_run_dependencies(db, user.id),
        "request_id": current_request_id.get(),
    }
    execution = requirement.tool_execution
    if body.approved and execution.tool_name == "send_email":
        payload["approval_token"] = await issue_approval(db, user.id, body.run_id, execution.tool_args or {})
    run = await enqueue_run(
        db,
        user_id=user.id,
        session_id=session.id,
        kind="confirmation",
        payload=payload,
        access_token=request.headers["authorization"].split(" ", 1)[1],
        idempotency_key=key,
    )
    return _response(run, user.id)


@router.get("/runs/{run_id}")
async def run_status(
    run_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await get_owned_run(db, run_id, user.id)
    return {
        "id": run.id,
        "session_id": run.session_id,
        "status": run.status,
        "last_event_sequence": run.last_event_sequence,
    }


@router.get("/runs/{run_id}/events")
async def resume_run(
    run_id: UUID,
    request: Request,
    after: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    run = await get_owned_run(db, run_id, user.id)
    try:
        after = max(after, int(request.headers.get("Last-Event-ID", "0")))
    except ValueError as exc:
        raise HTTPException(422, "Invalid event cursor") from exc
    if after > run.last_event_sequence:
        raise HTTPException(422, "Event cursor is beyond the stored stream")
    return _response(run, user.id, after)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await request_cancel(db, run_id, user.id)
    return {"id": run.id, "status": run.status, "cancel_requested": run.cancel_requested}

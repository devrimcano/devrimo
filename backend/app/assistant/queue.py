"""Authenticated enqueue, fenced execution, and resumable per-user events."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.models import AssistantRun, AssistantRunEvent
from app.core.crypto import encrypt_secret
from app.db.models import AccountDirectory, AccountStatus, Agent, ChatSession
from app.db.session import SessionLocal

TERMINAL = {"paused", "completed", "failed", "cancelled", "interrupted"}
LEASE_SECONDS = 90


class RunLeaseLost(RuntimeError):
    pass


async def get_owned_run(db: AsyncSession, run_id: UUID, user_id: UUID) -> AssistantRun:
    run = await db.scalar(select(AssistantRun).where(AssistantRun.id == run_id, AssistantRun.user_id == user_id))
    if run is None:
        raise HTTPException(404, "Assistant run not found")
    return run


async def enqueue_run(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: str,
    kind: str,
    payload: dict,
    access_token: str,
    idempotency_key: str,
) -> AssistantRun:
    """Called only after API JWT+account authorization and owner context creation."""
    if kind not in {"chat", "confirmation"} or not 1 <= len(idempotency_key) <= 128:
        raise HTTPException(422, "Invalid run kind or idempotency key")
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
        expiry = datetime.fromtimestamp(float(claims["exp"]), UTC)
        if str(claims["sub"]) != str(user_id) or expiry <= datetime.now(UTC):
            raise ValueError("Invalid identity or expiry")
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(401, "Authenticated access token required") from exc
    # API-owned transaction: serialize admission against account suspension.
    account = await db.scalar(
        select(AccountDirectory)
        .where(AccountDirectory.user_id == user_id, AccountDirectory.status == AccountStatus.active)
        .with_for_update(read=True)
    )
    if account is None:
        raise HTTPException(403, "Account is inactive")
    request_hash = hashlib.sha256(
        json.dumps(
            [session_id, kind, {key: payload.get(key) for key in ("text", "run_id", "requirement_id", "approved")}],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    existing = await db.scalar(
        select(AssistantRun).where(AssistantRun.user_id == user_id, AssistantRun.idempotency_key == idempotency_key)
    )
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(409, "Idempotency key belongs to another request")
        return existing
    values = dict(payload)
    approval = values.pop("approval_token", None)
    run = AssistantRun(
        user_id=user_id,
        session_id=session_id,
        kind=kind,
        payload=values,
        token_enc=encrypt_secret(access_token),
        approval_token_enc=encrypt_secret(approval) if approval else None,
        token_expires_at=expiry,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    db.add(run)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        existing = await db.scalar(
            select(AssistantRun).where(AssistantRun.user_id == user_id, AssistantRun.idempotency_key == idempotency_key)
        )
        if existing and existing.request_hash == request_hash:
            return existing
        raise HTTPException(409, "Agent is busy with another message") from exc
    return run


async def request_cancel(db: AsyncSession, run_id: UUID, user_id: UUID) -> AssistantRun:
    run = await get_owned_run(db, run_id, user_id)
    if run.status not in TERMINAL:
        from app.workspace.approvals import revoke_pending_approvals

        run.cancel_requested = True
        if run.kind == "confirmation":
            await revoke_pending_approvals(db, user_id, run_id=run.payload.get("run_id"))
        await db.commit()
    return run


def _append(db, run, payload):
    run.last_event_sequence += 1
    db.add(
        AssistantRunEvent(
            run_id=run.id,
            sequence=run.last_event_sequence,
            payload=payload.decode() if isinstance(payload, bytes) else payload,
        )
    )


async def cancel_account_runs(db: AsyncSession, user_id: UUID) -> None:
    """API only; commit together with the account status change."""
    from app.assistant.events import _chunk
    from app.workspace.approvals import revoke_pending_approvals

    # Flush the account update first to serialize with enqueue's shared lock.
    await db.flush()
    runs = (
        await db.scalars(
            select(AssistantRun)
            .where(AssistantRun.user_id == user_id, AssistantRun.status.in_(["queued", "running"]))
            .with_for_update()
        )
    ).all()
    for run in runs:
        run.cancel_requested = True
        run.token_enc = None
        run.approval_token_enc = None
        if run.status == "queued":
            run.status = "cancelled"
            run.error_code = "account_inactive"
            run.finished_at = datetime.now(UTC)
            _append(
                db,
                run,
                _chunk(
                    "assistant",
                    extension={"type": "error", "code": "account_inactive", "message": "Account is inactive."},
                    finish="stop",
                ),
            )
            _append(db, run, b"data: [DONE]\n\n")
    await revoke_pending_approvals(db, user_id)


async def append_event(db: AsyncSession, run_id: UUID, owner: str, payload: bytes) -> None:
    run = await db.scalar(
        select(AssistantRun)
        .where(
            AssistantRun.id == run_id,
            AssistantRun.lease_owner == owner,
            AssistantRun.status == "running",
            AssistantRun.leased_until > datetime.now(UTC),
        )
        .with_for_update()
    )
    if run is None:
        raise RunLeaseLost("Run execution lease lost")
    _append(db, run, payload)
    await db.commit()


async def finish_run(
    db: AsyncSession,
    run_id: UUID,
    owner: str,
    status: str,
    *,
    error_code: str | None = None,
    agno_run_id: str | None = None,
) -> None:
    run = await db.scalar(
        select(AssistantRun)
        .where(
            AssistantRun.id == run_id,
            AssistantRun.lease_owner == owner,
            AssistantRun.status == "running",
            AssistantRun.leased_until > datetime.now(UTC),
        )
        .with_for_update()
    )
    if run is None:
        raise RunLeaseLost("Run completion belongs to an expired lease")
    if run.cancel_requested and status != "cancelled":
        from app.assistant.events import _chunk

        status = "cancelled"
        error_code = "cancelled"
        _append(
            db,
            run,
            _chunk(
                "assistant",
                extension={"type": "error", "code": "cancelled", "message": "Run cancelled."},
                finish="stop",
            ),
        )
    run.status = status
    run.error_code = error_code
    run.agno_run_id = agno_run_id
    run.finished_at = datetime.now(UTC)
    run.token_enc = None
    run.approval_token_enc = None
    run.leased_until = None
    if run.kind == "chat" and status in {"completed", "paused", "failed"}:
        session = await db.get(ChatSession, run.session_id)
        if session is not None:
            session.message_count += 1
            if not session.title:
                session.title = str(run.payload.get("text", ""))[:80] or None
    await db.execute(update(Agent).where(Agent.user_id == run.user_id).values(last_active_at=datetime.now(UTC)))
    _append(db, run, b"data: [DONE]\n\n")
    await db.commit()


async def claim_run(db: AsyncSession, owner: str) -> AssistantRun | None:
    now = datetime.now(UTC)
    expired = (
        await db.scalars(
            select(AssistantRun)
            .where(AssistantRun.status == "running", AssistantRun.leased_until <= now)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for run in expired:
        run.status = "interrupted"
        run.error_code = "worker_interrupted"
        run.finished_at = now
        run.token_enc = None
        run.approval_token_enc = None
        # Never replay a potentially sent email after losing a worker.
        from app.assistant.events import _chunk

        _append(
            db,
            run,
            _chunk(
                "assistant",
                extension={
                    "type": "error",
                    "code": "worker_interrupted",
                    "message": "The assistant worker stopped. This run will not be replayed automatically.",
                },
                finish="stop",
            ),
        )
        _append(db, run, b"data: [DONE]\n\n")
    run = await db.scalar(
        select(AssistantRun)
        .where(AssistantRun.status == "queued")
        .order_by(AssistantRun.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if run:
        run.status = "running"
        run.lease_owner = owner
        run.leased_until = now + timedelta(seconds=LEASE_SECONDS)
        run.started_at = now
    await db.commit()
    return run


async def renew_run(db: AsyncSession, run_id: UUID, owner: str) -> bool:
    run = await db.scalar(
        select(AssistantRun)
        .where(
            AssistantRun.id == run_id,
            AssistantRun.lease_owner == owner,
            AssistantRun.status == "running",
            AssistantRun.leased_until > datetime.now(UTC),
        )
        .with_for_update()
    )
    if run is None:
        raise RunLeaseLost("Run execution lease lost")
    run.leased_until = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
    from app.admin.directory import active_account

    cancelled = run.cancel_requested or await active_account(db, run.user_id) is None
    await db.commit()
    return cancelled


async def stream_events(run_id: UUID, user_id: UUID, after: int = 0):
    if after < 0:
        raise HTTPException(422, "Event cursor must be nonnegative")
    cursor = after
    while True:
        async with SessionLocal() as db:
            run = await get_owned_run(db, run_id, user_id)
            rows = (
                await db.scalars(
                    select(AssistantRunEvent)
                    .where(AssistantRunEvent.run_id == run_id, AssistantRunEvent.sequence > cursor)
                    .order_by(AssistantRunEvent.sequence)
                    .limit(100)
                )
            ).all()
            status = run.status
        for event in rows:
            cursor = event.sequence
            yield f"id: {cursor}\n{event.payload}".encode()
        if status in TERMINAL and not rows:
            return
        if not rows:
            yield b": keep-alive\n\n"
            await asyncio.sleep(0.2)

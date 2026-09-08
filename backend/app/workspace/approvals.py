"""API-issued, single-use exact-draft capabilities for external email effects."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.session import get_session_factory
from app.workspace.models import WorkspaceMailApproval
from app.workspace.resources import EmailDraft


def draft_digest(draft: EmailDraft):
    return hashlib.sha256(json.dumps(draft.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def issue_approval(db, user_id, run_id: str, arguments: dict) -> str:
    """API calls only after validating owner, paused requirement, and explicit approval."""
    draft = EmailDraft.model_validate(arguments["draft"])
    token = secrets.token_urlsafe(32)
    db.add(
        WorkspaceMailApproval(
            user_id=user_id,
            run_id=run_id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            draft_digest=draft_digest(draft),
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            status="pending",
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "This exact paused action already has an approval") from exc
    return token


async def execute_approved(user_id, token: str, draft: EmailDraft, send):
    async with get_session_factory("api")() as db:
        approval = await db.scalar(
            select(WorkspaceMailApproval)
            .where(
                WorkspaceMailApproval.user_id == user_id,
                WorkspaceMailApproval.token_hash == hashlib.sha256(token.encode()).hexdigest(),
            )
            .with_for_update()
        )
        if approval is None or approval.draft_digest != draft_digest(draft):
            raise HTTPException(403, "Email approval does not match this exact draft")
        if approval.status == "sent":
            return approval.result
        if approval.status == "revoked":
            raise HTTPException(403, "Email approval was cancelled")
        if approval.status != "pending":
            raise HTTPException(409, "Email outcome is uncertain; this approval cannot be retried")
        if approval.expires_at <= datetime.now(UTC):
            raise HTTPException(403, "Email approval expired")
        approval.status = "executing"
        await db.commit()
        try:
            result = await send(draft)
        except BaseException:
            approval.status = "unknown"
            await db.commit()
            raise
        approval.status = "sent"
        # Only delivery metadata persists; message content is not copied here.
        approval.result = (
            {key: result[key] for key in ("success", "message_id", "status") if key in result}
            if isinstance(result, dict)
            else {"status": "sent"}
        )
        await db.commit()
        return result


async def revoke_pending_approvals(db, user_id, run_id: str | None = None):
    """Cancellation withdraws unused API capabilities in the caller's transaction."""
    from sqlalchemy import update

    statement = update(WorkspaceMailApproval).where(
        WorkspaceMailApproval.user_id == user_id, WorkspaceMailApproval.status == "pending"
    )
    if run_id is not None:
        statement = statement.where(WorkspaceMailApproval.run_id == run_id)
    await db.execute(statement.values(status="revoked"))
    await db.flush()

"""Manage the student's METU connection and which campus MCP tools it powers.

Credentials arrive here in plaintext exactly once per change — over TLS, from
the onboarding form — are checked against METU SSO, and are stored encrypted.
They are never read back out: every response shape in this module is a
description of the connection, not the connection itself.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import manager
from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.campus import service as campus_service
from app.campus.credentials import secrets_for
from app.campus.verify import normalize_username, verify_metu_credentials
from app.config import get_settings
from app.db.models import AgentStatus
from app.db.session import get_db
from app.logging import get_logger
from app.observability import capture_exception
from app.observability.client import report_exception
from app.planning.mcp_bridge import sync_student_context_from_sais
from app.schemas import (
    CampusConnectionIn,
    CampusConnectionOut,
    CampusVerifyIn,
    CampusVerifyOut,
)

router = APIRouter()
logger = get_logger(__name__)


async def _connection_out(db: AsyncSession, user_id) -> CampusConnectionOut:
    credential = await campus_service.get_credential(db, user_id)
    return CampusConnectionOut.from_model(
        credential,
        secrets_for(credential),
        campus_service.enabled_tool_ids(credential),
    )


@router.get("/connection", response_model=CampusConnectionOut)
async def get_connection(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampusConnectionOut:
    return await _connection_out(db, user.id)


@router.post("/connection/verify", response_model=CampusVerifyOut)
async def verify_connection(
    body: CampusVerifyIn,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CampusVerifyOut:
    """Check credentials without storing them, so the form can validate inline."""
    result = await verify_metu_credentials(body.metu_username, body.metu_password)
    return CampusVerifyOut(ok=result.ok, unreachable=result.unreachable, detail=result.detail)


@router.put("/connection", response_model=CampusConnectionOut)
async def put_connection(
    body: CampusConnectionIn,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampusConnectionOut:
    username = normalize_username(body.metu_username)
    if not username:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A METU username is required")

    existing = await campus_service.get_credential(db, user.id)
    # A password is required the first time; later saves (toggling tools,
    # switching locale) may omit it and keep the stored one.
    if body.metu_password is None and (existing is None or existing.metu_password_enc is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A METU password is required")

    verified = False
    verification_error: str | None = None
    if body.metu_password and not body.skip_verification:
        result = await verify_metu_credentials(username, body.metu_password)
        verified = result.ok
        if not result.ok and not result.unreachable:
            # A definite "no" from METU is a user error worth blocking on;
            # an unreachable SSO is not, and falls through to being saved
            # unverified with the reason recorded.
            raise HTTPException(status.HTTP_400_BAD_REQUEST, result.detail or "METU rejected these credentials")
        verification_error = result.detail if not result.ok else None
    elif body.metu_password is None and existing is not None:
        # Unchanged secret: carry the previous verification result forward
        # rather than silently downgrading the connection to "unverified".
        verified = existing.verified_at is not None
        verification_error = existing.verification_error

    credential = await campus_service.upsert_credential(
        db,
        user.id,
        metu_username=username,
        metu_password=body.metu_password,
        odtuclass_token=body.odtuclass_token,
        odtuclass_base_url=body.odtuclass_base_url,
        locale=body.locale,
        enabled_tools=body.enabled_tools,
        verified=verified,
        verification_error=verification_error,
    )

    await _reconfigure_agent_if_running(db, user.id)
    await _sync_student_context(user.id, verified=verified)
    return CampusConnectionOut.from_model(
        credential,
        secrets_for(credential),
        campus_service.enabled_tool_ids(credential),
    )


@router.delete("/connection", response_model=CampusConnectionOut)
async def delete_connection(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampusConnectionOut:
    """Forget the stored credentials and strip the campus tools from the agent."""
    await campus_service.delete_credential(db, user.id)
    await _reconfigure_agent_if_running(db, user.id)
    return await _connection_out(db, user.id)


@router.post("/apply", response_model=CampusConnectionOut)
async def apply_connection(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampusConnectionOut:
    """Invalidate any resident runtime so the next turn uses current tools."""
    agent = await manager.get_or_create_agent(db, user.id)
    await manager.apply_campus_config(db, agent)
    return await _connection_out(db, user.id)


async def _reconfigure_agent_if_running(db: AsyncSession, user_id) -> None:
    """Best-effort rebuild so a saved change is live without a second click.

    A failure here is not a failure of the save — the credentials are stored
    and ``config_dirty`` stays set, so the UI can offer a retry via
    ``POST /campus/apply`` instead of losing the student's input.
    """
    agent = await manager.get_agent(db, user_id)
    if agent is None or agent.status != AgentStatus.running:
        return
    try:
        await manager.apply_campus_config(db, agent)
    except Exception as exc:
        logger.warning("campus_apply_failed", user_id=str(user_id), error=str(exc))
        # The save still succeeds, so nothing surfaces to the student and
        # nothing surfaced to us either: a campus config that silently never
        # reached the running agent looked identical to one that did.
        report_exception(exc, distinct_id=str(user_id), handler="campus_apply", operation="apply_campus_config")
        # The save succeeded and config_dirty stays set, so the student can
        # retry — but a rebuild that fails every time needs to be visible.
        capture_exception(
            exc,
            distinct_id=str(user_id),
            **{"$exception_fingerprint": ["campus_apply_failed"]},
        )


async def _sync_student_context(user_id, *, verified: bool) -> None:
    """Best-effort read of the academic context SAIS already knows.

    Only worth attempting on a verified connection: unverified credentials
    would just spawn campus servers that cannot sign in. Like the rebuild
    above, a failure here is not a failure of the save — the student can still
    fill the context in by hand, and the planner refreshes it in-turn anyway.
    """
    if not verified or not get_settings().campus_context_sync_on_connect:
        return
    try:
        await sync_student_context_from_sais(user_id)
    except Exception as exc:
        logger.warning("student_context_sync_failed", user_id=str(user_id), error=str(exc))
        report_exception(
            exc,
            distinct_id=str(user_id),
            handler="campus_connect",
            operation="sync_student_context_from_sais",
            dependency="sais",
        )
        capture_exception(
            exc,
            distinct_id=str(user_id),
            **{"$exception_fingerprint": ["student_context_sync_failed"]},
        )

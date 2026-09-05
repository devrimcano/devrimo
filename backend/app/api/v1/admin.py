import base64
import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, case, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.audit import record_event
from app.admin.auth import AdminPermission, AdminPrincipal, get_admin_principal, require
from app.admin.cleanup import purge_agno_user
from app.admin.directory import METU_ID, ensure_metu
from app.admin.schemas import AgentActionIn, DeleteUserIn, InviteIn, MembershipIn, ReasonIn, RuntimeSettingsIn
from app.admin.supabase import SupabaseAdmin, parse_auth_time
from app.agents import manager
from app.agents.pool import get_pool
from app.agents.runtime import get_runtime_config
from app.campus.catalog import CAMPUS_TOOLS
from app.campus.manifest import commits_by_slug
from app.config import get_settings
from app.db.models import (
    AccountDirectory,
    AccountStatus,
    AdminAuditEvent,
    AdminMembership,
    AdminRole,
    Agent,
    AgentRuntimeSettings,
    AgentStatus,
    CampusCredential,
    ChatSession,
    UserProfile,
)
from app.db.session import get_db
from app.observability.client import report_exception

router = APIRouter()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _account_state(account: AccountDirectory) -> dict:
    return {"status": account.status.value, "suspended_at": _iso(account.suspended_at)}


def _encode_cursor(created_at: datetime, row_id: UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        timestamp, row_id = json.loads(raw)
        return datetime.fromisoformat(timestamp), UUID(row_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor") from exc


def _scope(principal: AdminPrincipal):
    return AccountDirectory.organization_id == principal.organization_id if principal.organization_id else text("1=1")


async def _token_usage(principal: AdminPrincipal, db: AsyncSession) -> dict:
    """Aggregate Agno run metrics without loading prompts or responses."""
    table = "ai.agno_runs"
    if not bool(await db.scalar(text("SELECT to_regclass('ai.agno_runs') IS NOT NULL"))):
        return {
            "runs": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "last_24h_tokens": 0,
            "last_7d_tokens": 0,
            "estimated_cost_usd": 0.0,
            "primary_model_tokens": 0,
            "compression_tokens": 0,
            "learning_tokens": 0,
        }
    metric = lambda key: f"COALESCE(CAST(run_data -> 'metrics' ->> '{key}' AS BIGINT), 0)"  # noqa: E731

    organization_filter = ""
    params: dict[str, object] = {
        "day_cutoff": int(datetime.now(UTC).timestamp()) - 86_400,
        "week_cutoff": int(datetime.now(UTC).timestamp()) - 604_800,
    }
    if principal.organization_id is not None:
        organization_filter = (
            "WHERE EXISTS (SELECT 1 FROM account_directory ad "
            "WHERE r.user_id ~* '^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?"
            "[0-9a-f]{4}-?[0-9a-f]{12}$' AND ad.user_id = CAST(r.user_id AS UUID) "
            "AND CAST(ad.organization_id AS TEXT) = :organization_id)"
        )
        params["organization_id"] = str(principal.organization_id)

    input_tokens = metric("input_tokens")
    output_tokens = metric("output_tokens")
    # Some providers omit total_tokens while still reporting both components.
    total_tokens = f"({input_tokens} + {output_tokens})"
    row = (
        await db.execute(
            text(
                f"""
                SELECT
                    COUNT(*) AS runs,
                    COALESCE(SUM({input_tokens}), 0) AS input_tokens,
                    COALESCE(SUM({output_tokens}), 0) AS output_tokens,
                    COALESCE(SUM({total_tokens}), 0) AS total_tokens,
                    COALESCE(SUM(CASE WHEN created_at >= :day_cutoff THEN {total_tokens} ELSE 0 END), 0) AS day_tokens,
                    COALESCE(SUM(CASE WHEN created_at >= :week_cutoff THEN {total_tokens} ELSE 0 END), 0) AS week_tokens
                FROM {table} r
                {organization_filter}
                """
            ),
            params,
        )
    ).one()
    role_source = (
        "JOIN LATERAL jsonb_each(COALESCE((r.run_data::jsonb) -> 'metrics' -> 'details', '{}'::jsonb)) "
        "role(key, value) ON TRUE JOIN LATERAL jsonb_array_elements(role.value) entry(value) ON TRUE"
    )
    role_input = "COALESCE(CAST(entry.value ->> 'input_tokens' AS BIGINT), 0)"
    role_output = "COALESCE(CAST(entry.value ->> 'output_tokens' AS BIGINT), 0)"
    role_rows = (
        await db.execute(
            text(
                f"""
                SELECT role.key, COALESCE(SUM({role_input} + {role_output}), 0) AS tokens
                FROM {table} r {role_source}
                {organization_filter}
                GROUP BY role.key
                """
            ),
            params,
        )
    ).all()
    tokens_by_role = {str(role): int(tokens or 0) for role, tokens in role_rows}
    runtime = await get_runtime_config(db)
    estimated_cost = float(row.input_tokens or 0) * runtime.input_token_price + float(
        row.output_tokens or 0
    ) * runtime.output_token_price
    return {
        "runs": int(row.runs or 0),
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "total_tokens": int(row.total_tokens or 0),
        "last_24h_tokens": int(row.day_tokens or 0),
        "last_7d_tokens": int(row.week_tokens or 0),
        "estimated_cost_usd": round(estimated_cost, 6),
        "primary_model_tokens": tokens_by_role.get("model", 0),
        "compression_tokens": tokens_by_role.get("compression_model", 0),
        "learning_tokens": tokens_by_role.get("learning_model", 0),
    }


@router.get("/me")
async def admin_me(principal: AdminPrincipal = Depends(get_admin_principal)) -> dict:
    return {
        "user_id": str(principal.user.id),
        "email": principal.user.email,
        "role": principal.role.value,
        "organization_id": str(principal.organization_id) if principal.organization_id else None,
        "permissions": sorted(permission.value for permission in principal.permissions),
        "bootstrap": principal.bootstrap,
    }


@router.get("/overview")
async def overview(
    principal: AdminPrincipal = Depends(require(AdminPermission.overview_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    scope = _scope(principal)
    totals = (
        await db.execute(
            select(
                func.count(AccountDirectory.user_id),
                func.sum(case((AccountDirectory.status == AccountStatus.active, 1), else_=0)),
                func.sum(case((UserProfile.onboarding_completed_at.is_not(None), 1), else_=0)),
                func.sum(case((CampusCredential.user_id.is_not(None), 1), else_=0)),
            )
            .select_from(AccountDirectory)
            .outerjoin(UserProfile, UserProfile.user_id == AccountDirectory.user_id)
            .outerjoin(CampusCredential, CampusCredential.user_id == AccountDirectory.user_id)
            .where(scope, AccountDirectory.status != AccountStatus.deleted)
        )
    ).one()
    agent_counts = (
        await db.execute(
            select(Agent.status, func.count(Agent.id))
            .join(AccountDirectory, AccountDirectory.user_id == Agent.user_id)
            .where(scope)
            .group_by(Agent.status)
        )
    ).all()
    statuses = {row[0].value: row[1] for row in agent_counts}
    attention = (
        await db.execute(
            select(AccountDirectory.user_id, AccountDirectory.email, AccountDirectory.status, Agent.status)
            .outerjoin(Agent, Agent.user_id == AccountDirectory.user_id)
            .where(
                scope,
                or_(
                    AccountDirectory.status != AccountStatus.active,
                    Agent.status == AgentStatus.error,
                ),
            )
            .order_by(AccountDirectory.updated_at.desc())
            .limit(8)
        )
    ).all()
    usage = await _token_usage(principal, db)
    return {
        "users": totals[0] or 0,
        "active_users": totals[1] or 0,
        "onboarding_completed": totals[2] or 0,
        "campus_connected": totals[3] or 0,
        "agents": statuses,
        "resident_agents": get_pool().size(),
        "usage": usage,
        "attention": [
            {
                "user_id": str(row[0]),
                "email": row[1],
                "account_status": row[2].value,
                "agent_status": row[3].value if row[3] else None,
            }
            for row in attention
        ],
        "fresh_at": datetime.now(UTC).isoformat(),
    }


@router.get("/users")
async def users(
    q: str | None = Query(default=None, max_length=200),
    account_status: AccountStatus | None = None,
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    principal: AdminPrincipal = Depends(require(AdminPermission.users_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conditions = [_scope(principal), AccountDirectory.status != AccountStatus.deleted]
    if account_status:
        conditions.append(AccountDirectory.status == account_status)
    if q:
        needle = f"%{q.strip().lower()}%"
        conditions.append(
            or_(
                AccountDirectory.email_normalized.like(needle),
                func.lower(UserProfile.display_name).like(needle),
            )
        )
    if cursor:
        created_at, row_id = _decode_cursor(cursor)
        conditions.append(
            or_(
                AccountDirectory.created_at < created_at,
                and_(AccountDirectory.created_at == created_at, AccountDirectory.user_id < row_id),
            )
        )
    rows = (
        await db.execute(
            select(AccountDirectory, UserProfile.display_name, UserProfile.onboarding_completed_at, Agent.status)
            .outerjoin(UserProfile, UserProfile.user_id == AccountDirectory.user_id)
            .outerjoin(Agent, Agent.user_id == AccountDirectory.user_id)
            .where(*conditions)
            .order_by(AccountDirectory.created_at.desc(), AccountDirectory.user_id.desc())
            .limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "user_id": str(account.user_id),
            "email": account.email,
            "display_name": display_name,
            "status": account.status.value,
            "onboarding_completed": onboarding_at is not None,
            "agent_status": agent_status.value if agent_status else None,
            "last_seen_at": _iso(account.last_seen_at),
            "created_at": _iso(account.created_at),
        }
        for account, display_name, onboarding_at, agent_status in rows
    ]
    next_cursor = _encode_cursor(rows[-1][0].created_at, rows[-1][0].user_id) if has_more and rows else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/exports/users")
async def export_users(
    principal: AdminPrincipal = Depends(require(AdminPermission.users_read)),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    rows = (
        await db.execute(
            select(AccountDirectory, UserProfile.display_name, Agent.status)
            .outerjoin(UserProfile, UserProfile.user_id == AccountDirectory.user_id)
            .outerjoin(Agent, Agent.user_id == AccountDirectory.user_id)
            .where(_scope(principal), AccountDirectory.status != AccountStatus.deleted)
            .order_by(AccountDirectory.created_at.desc())
            .limit(10000)
        )
    ).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["user_id", "email", "display_name", "account_status", "agent_status", "created_at"])
    for account, display_name, agent_status in rows:
        writer.writerow(
            [
                account.user_id,
                account.email or "",
                display_name or "",
                account.status.value,
                agent_status.value if agent_status else "",
                _iso(account.created_at),
            ]
        )
    await record_event(db, actor_user_id=principal.user.id, action="users.export", result="success")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=admin-users.csv"},
    )


@router.get("/users/{user_id}")
async def user_detail(
    user_id: UUID,
    principal: AdminPrincipal = Depends(require(AdminPermission.users_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (
        await db.execute(
            select(AccountDirectory, UserProfile, Agent, CampusCredential)
            .outerjoin(UserProfile, UserProfile.user_id == AccountDirectory.user_id)
            .outerjoin(Agent, Agent.user_id == AccountDirectory.user_id)
            .outerjoin(CampusCredential, CampusCredential.user_id == AccountDirectory.user_id)
            .where(AccountDirectory.user_id == user_id, _scope(principal))
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    account, profile, agent, campus = row
    session_count, last_session_at = (
        await db.execute(
            select(func.count(ChatSession.id), func.max(ChatSession.updated_at)).where(ChatSession.user_id == user_id)
        )
    ).one()
    return {
        "user_id": str(user_id),
        "email": account.email,
        "display_name": profile.display_name if profile else None,
        "locale": profile.locale if profile else None,
        "status": account.status.value,
        "last_seen_at": _iso(account.last_seen_at),
        "created_at": _iso(account.created_at),
        "onboarding_completed_at": _iso(profile.onboarding_completed_at) if profile else None,
        "agent": {
            "status": agent.status.value,
            "last_active_at": _iso(agent.last_active_at),
            "resident": get_pool().is_resident(user_id),
            "has_error": bool(agent.error_detail),
        }
        if agent
        else None,
        "campus": {
            "connected": campus is not None,
            "verified_at": _iso(campus.verified_at) if campus else None,
            "verification_failed": bool(campus and campus.verification_error),
            "enabled_tools": campus.enabled_tools if campus else [],
            "needs_restart": bool(campus and campus.config_dirty),
        },
        "sessions": {"count": session_count, "last_active_at": _iso(last_session_at)},
    }


@router.post("/invitations", status_code=status.HTTP_201_CREATED)
async def invite_user(
    body: InviteIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.users_invite)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await ensure_metu(db)
    try:
        auth_user = await SupabaseAdmin().invite(body.email)
        raw_user = auth_user.get("user", auth_user)
        user_id = UUID(raw_user["id"])
        account = AccountDirectory(
            user_id=user_id,
            organization_id=principal.organization_id or METU_ID,
            email=body.email,
            email_normalized=body.email,
            auth_created_at=parse_auth_time(raw_user.get("created_at")),
        )
        db.add(account)
        await db.commit()
        await record_event(
            db,
            actor_user_id=principal.user.id,
            target_user_id=user_id,
            organization_id=account.organization_id,
            action="user.invite",
            result="success",
            after={"status": "active"},
        )
        return {"user_id": str(user_id), "email": body.email, "status": "invited"}
    except Exception as exc:
        await db.rollback()
        await record_event(db, actor_user_id=principal.user.id, action="user.invite", result="failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invitation could not be sent") from exc


async def _account_for_action(db: AsyncSession, user_id: UUID, principal: AdminPrincipal) -> AccountDirectory:
    account = await db.get(AccountDirectory, user_id)
    if account is None or (principal.organization_id and account.organization_id != principal.organization_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return account


@router.post("/users/{user_id}/suspend")
async def suspend_user(
    user_id: UUID,
    body: ReasonIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.users_suspend)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user_id == principal.user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot suspend yourself")
    account = await _account_for_action(db, user_id, principal)
    before = _account_state(account)
    account.status = AccountStatus.suspended
    account.suspended_at = datetime.now(UTC)
    account.suspended_reason = body.reason
    await db.commit()  # local deny takes effect before the remote ban
    await get_pool().invalidate(user_id)
    result = "success"
    try:
        await SupabaseAdmin().update_user(user_id, ban_duration="876000h")
    except Exception as exc:
        # The local deny already committed, so the student is locked out of the
        # broker — but their Supabase session is still valid. A suspension that
        # is only half applied is exactly the state worth an issue.
        result = "partial"
        report_exception(
            exc,
            distinct_id=str(principal.user.id),
            handler="admin_user_suspend",
            dependency="supabase_admin",
            target_user_id=str(user_id),
        )
    await record_event(
        db,
        actor_user_id=principal.user.id,
        target_user_id=user_id,
        organization_id=account.organization_id,
        action="user.suspend",
        result=result,
        reason=body.reason,
        before=before,
        after=_account_state(account),
    )
    return {"status": account.status.value, "result": result}


@router.post("/users/{user_id}/reactivate")
async def reactivate_user(
    user_id: UUID,
    body: ReasonIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.users_suspend)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    account = await _account_for_action(db, user_id, principal)
    before = _account_state(account)
    try:
        await SupabaseAdmin().update_user(user_id, ban_duration="none")
    except Exception as exc:
        await record_event(
            db,
            actor_user_id=principal.user.id,
            target_user_id=user_id,
            action="user.reactivate",
            result="failed",
            reason=body.reason,
            before=before,
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Supabase account could not be reactivated") from exc
    account.status = AccountStatus.active
    account.suspended_at = None
    account.suspended_reason = None
    await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        target_user_id=user_id,
        organization_id=account.organization_id,
        action="user.reactivate",
        result="success",
        reason=body.reason,
        before=before,
        after=_account_state(account),
    )
    return {"status": "active"}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: UUID,
    body: DeleteUserIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.users_delete)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    settings = get_settings()
    if user_id == principal.user.id or str(user_id) in settings.admin_bootstrap_ids:
        raise HTTPException(status.HTTP_409_CONFLICT, "Protected admin accounts cannot be deleted")
    account = await _account_for_action(db, user_id, principal)
    if account.status == AccountStatus.deleted:
        return {"status": "deleted", "result": "already_complete"}
    if account.status not in {AccountStatus.suspended, AccountStatus.deletion_pending}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Suspend the account before permanent deletion")
    if not account.email or body.confirm_email.strip().lower() != account.email_normalized:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Typed email does not match")
    membership = await db.get(AdminMembership, user_id)
    if membership and membership.role == AdminRole.super_admin:
        super_count = await db.scalar(
            select(func.count()).select_from(AdminMembership).where(AdminMembership.role == AdminRole.super_admin)
        )
        if (super_count or 0) <= 1 and not settings.admin_bootstrap_ids:
            raise HTTPException(status.HTTP_409_CONFLICT, "The last super admin cannot be deleted")
    account.status = AccountStatus.deletion_pending
    await db.commit()
    try:
        await SupabaseAdmin().delete_user(user_id)
        await get_pool().invalidate(user_id)
        await purge_agno_user(str(user_id))
        await db.execute(delete(ChatSession).where(ChatSession.user_id == user_id))
        await db.execute(delete(CampusCredential).where(CampusCredential.user_id == user_id))
        await db.execute(delete(UserProfile).where(UserProfile.user_id == user_id))
        await db.execute(delete(Agent).where(Agent.user_id == user_id))
        await db.execute(delete(AdminMembership).where(AdminMembership.user_id == user_id))
        digest = hashlib.sha256(str(user_id).encode()).hexdigest()[:16]
        account.email = f"deleted-{digest}@redacted.invalid"
        account.email_normalized = account.email
        account.status = AccountStatus.deleted
        account.deleted_at = datetime.now(UTC)
        account.suspended_reason = None
        await db.commit()
        result = "success"
    except Exception as exc:
        await db.rollback()
        await record_event(
            db,
            actor_user_id=principal.user.id,
            target_user_id=user_id,
            action="user.delete",
            result="partial",
            reason=body.reason,
            after={"status": "deletion_pending"},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Deletion is incomplete and can be retried") from exc
    await record_event(
        db,
        actor_user_id=principal.user.id,
        target_user_id=user_id,
        organization_id=account.organization_id,
        action="user.delete",
        result=result,
        reason=body.reason,
        after={"status": "deleted"},
    )
    return {"status": "deleted", "result": result}


@router.get("/agents")
async def agents(
    principal: AdminPrincipal = Depends(require(AdminPermission.agents_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(
            select(Agent, AccountDirectory.email, UserProfile.display_name)
            .join(AccountDirectory, AccountDirectory.user_id == Agent.user_id)
            .outerjoin(UserProfile, UserProfile.user_id == Agent.user_id)
            .where(_scope(principal), AccountDirectory.status != AccountStatus.deleted)
            .order_by(Agent.updated_at.desc())
            .limit(200)
        )
    ).all()
    return {
        "items": [
            {
                "user_id": str(agent.user_id),
                "email": email,
                "display_name": name,
                "status": agent.status.value,
                "resident": get_pool().is_resident(agent.user_id),
                "last_active_at": _iso(agent.last_active_at),
                "has_error": bool(agent.error_detail),
            }
            for agent, email, name in rows
        ]
    }


@router.post("/agents/{user_id}/action")
async def agent_action(
    user_id: UUID,
    body: AgentActionIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.agents_restart)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _account_for_action(db, user_id, principal)
    if body.action in {"start", "stop", "destroy"} and AdminPermission.agents_manage not in principal.permissions:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a safe restart is allowed")
    agent = await manager.get_agent_or_404(db, user_id)
    if body.action == "start":
        await manager.start(db, agent)
    elif body.action == "stop":
        await manager.stop(db, agent)
    elif body.action == "destroy":
        await manager.destroy(db, agent)
    else:
        await get_pool().invalidate(user_id)
        agent.status = AgentStatus.running
        agent.error_detail = None
        await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        target_user_id=user_id,
        action=f"agent.{body.action}",
        result="success",
        reason=body.reason,
    )
    return {"status": "destroyed" if body.action == "destroy" else agent.status.value}


@router.get("/integrations")
async def integrations(
    principal: AdminPrincipal = Depends(require(AdminPermission.integrations_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(
            select(CampusCredential.enabled_tools, CampusCredential.verified_at, CampusCredential.verification_error)
            .join(AccountDirectory, AccountDirectory.user_id == CampusCredential.user_id)
            .where(_scope(principal), AccountDirectory.status != AccountStatus.deleted)
        )
    ).all()
    items = []
    for tool in CAMPUS_TOOLS:
        adopted = sum(tool.id in (row[0] or []) for row in rows)
        failures = sum(tool.id in (row[0] or []) and bool(row[2]) for row in rows)
        items.append(
            {
                "id": tool.id,
                "name_en": tool.name_en,
                "name_tr": tool.name_tr,
                "adopted": adopted,
                "verification_failures": failures,
            }
        )
    return {"connected_accounts": len(rows), "items": items, "commits": commits_by_slug(get_settings().campus_mcp_root)}


@router.get("/audit")
async def audit_events(
    action: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    principal: AdminPrincipal = Depends(require(AdminPermission.audit_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    conditions = []
    if principal.organization_id:
        conditions.append(AdminAuditEvent.organization_id == principal.organization_id)
    if action:
        conditions.append(AdminAuditEvent.action == action)
    rows = (
        (
            await db.execute(
                select(AdminAuditEvent).where(*conditions).order_by(AdminAuditEvent.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_audit_dict(row) for row in rows]}


def _audit_dict(row: AdminAuditEvent) -> dict:
    return {
        "id": str(row.id),
        "actor_user_id": str(row.actor_user_id),
        "target_user_id": str(row.target_user_id) if row.target_user_id else None,
        "action": row.action,
        "result": row.result,
        "reason": row.reason,
        "before": row.before_state,
        "after": row.after_state,
        "created_at": _iso(row.created_at),
    }


@router.get("/audit/export")
async def export_audit(
    principal: AdminPrincipal = Depends(require(AdminPermission.audit_export)),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    conditions = [AdminAuditEvent.organization_id == principal.organization_id] if principal.organization_id else []
    rows = (
        (
            await db.execute(
                select(AdminAuditEvent).where(*conditions).order_by(AdminAuditEvent.created_at.desc()).limit(10000)
            )
        )
        .scalars()
        .all()
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "actor_user_id", "target_user_id", "action", "result", "reason"])
    for row in rows:
        writer.writerow(
            [
                _iso(row.created_at),
                row.actor_user_id,
                row.target_user_id or "",
                row.action,
                row.result,
                row.reason or "",
            ]
        )
    await record_event(db, actor_user_id=principal.user.id, action="audit.export", result="success")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=admin-audit.csv"},
    )


@router.get("/memberships")
async def memberships(
    principal: AdminPrincipal = Depends(require(AdminPermission.memberships_manage)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(
            select(AdminMembership, AccountDirectory.email)
            .outerjoin(AccountDirectory, AccountDirectory.user_id == AdminMembership.user_id)
            .order_by(AdminMembership.created_at)
        )
    ).all()
    return {
        "items": [
            {
                "user_id": str(row.user_id),
                "email": email,
                "role": row.role.value,
                "organization_id": str(row.organization_id) if row.organization_id else None,
                "bootstrap": str(row.user_id) in get_settings().admin_bootstrap_ids,
            }
            for row, email in rows
        ]
    }


@router.put("/memberships/{user_id}")
async def put_membership(
    user_id: UUID,
    body: MembershipIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.memberships_manage)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.user_id != user_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "User IDs do not match")
    if user_id == principal.user.id:
        # Same reasoning as removal: an operator must not be able to demote
        # themselves out of the permission that would let them undo it.
        raise HTTPException(status.HTTP_409_CONFLICT, "Protected admin membership")
    await ensure_metu(db)
    account = await db.get(AccountDirectory, user_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    # A null organization means unscoped in `_scope`, so a campus role always
    # resolves to a concrete campus rather than silently going organization-wide.
    organization_id = body.organization_id
    if body.role == AdminRole.campus_admin and organization_id is None:
        organization_id = account.organization_id
    existing = await db.get(AdminMembership, user_id)
    before = {"role": existing.role.value} if existing else None
    if existing is None:
        existing = AdminMembership(
            user_id=user_id, role=body.role, organization_id=organization_id, granted_by=principal.user.id
        )
        db.add(existing)
    else:
        existing.role = body.role
        existing.organization_id = organization_id
        existing.granted_by = principal.user.id
    await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        target_user_id=user_id,
        action="membership.set",
        result="success",
        reason=body.reason,
        before=before,
        after={"role": body.role.value},
    )
    return {"user_id": str(user_id), "role": body.role.value}


@router.delete("/memberships/{user_id}")
async def remove_membership(
    user_id: UUID,
    body: ReasonIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.memberships_manage)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user_id == principal.user.id or str(user_id) in get_settings().admin_bootstrap_ids:
        raise HTTPException(status.HTTP_409_CONFLICT, "Protected admin membership")
    membership = await db.get(AdminMembership, user_id)
    if membership is None:
        return {"removed": False}
    if membership.role == AdminRole.super_admin:
        count = await db.scalar(
            select(func.count()).select_from(AdminMembership).where(AdminMembership.role == AdminRole.super_admin)
        )
        if (count or 0) <= 1 and not get_settings().admin_bootstrap_ids:
            raise HTTPException(status.HTTP_409_CONFLICT, "The last super admin cannot be removed")
    before = {"role": membership.role.value}
    await db.delete(membership)
    await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        target_user_id=user_id,
        action="membership.remove",
        result="success",
        reason=body.reason,
        before=before,
    )
    return {"removed": True}


@router.get("/runtime-settings")
async def runtime_settings(
    principal: AdminPrincipal = Depends(require(AdminPermission.runtime_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await get_runtime_config(db)
    row = await db.get(AgentRuntimeSettings, "default")
    return {
        **config.as_dict(),
        "has_database_override": bool(row and row.model_id),
        "updated_at": _iso(row.updated_at) if row else None,
        "editable": AdminPermission.runtime_write in principal.permissions,
    }


@router.put("/runtime-settings")
async def put_runtime_settings(
    body: RuntimeSettingsIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.runtime_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    before = (await get_runtime_config(db)).as_dict()
    row = await db.get(AgentRuntimeSettings, "default")
    if row is None:
        row = AgentRuntimeSettings(id="default", revision=1)
        db.add(row)
    row.model_id = body.model_id.strip()
    row.profile = body.profile
    row.max_tokens = body.max_tokens
    row.legacy_history_runs = body.legacy_history_runs
    row.scholar_history_runs = body.scholar_history_runs
    row.tool_call_limit = body.tool_call_limit
    row.learning_enabled = body.learning_enabled
    row.input_token_price = body.input_token_price
    row.output_token_price = body.output_token_price
    row.revision = (row.revision or 0) + 1
    row.updated_by = principal.user.id
    await db.commit()
    await get_pool().close_all()
    after = (await get_runtime_config(db)).as_dict()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        action="runtime.update",
        result="success",
        reason=body.reason,
        before=before,
        after=after,
    )
    return {**after, "editable": True}


@router.post("/directory/sync")
async def sync_directory_endpoint(
    principal: AdminPrincipal = Depends(require(AdminPermission.directory_sync)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    count = await sync_directory(db)
    await record_event(
        db, actor_user_id=principal.user.id, action="directory.sync", result="success", after={"synced": count}
    )
    return {"synced": count, "fresh_at": datetime.now(UTC).isoformat()}


async def sync_directory(db: AsyncSession) -> int:
    await ensure_metu(db)
    admin = SupabaseAdmin()
    users: list[dict] = []
    page = 1
    while True:
        batch = await admin.list_users(page=page, per_page=1000)
        users.extend(batch)
        if len(batch) < 1000:
            break
        page += 1
    for auth_user in users:
        user_id = UUID(auth_user["id"])
        account = await db.get(AccountDirectory, user_id)
        email = auth_user.get("email")
        if account is None:
            account = AccountDirectory(user_id=user_id, organization_id=METU_ID)
            db.add(account)
        if account.status != AccountStatus.deleted:
            account.email = email
            account.email_normalized = email.strip().lower() if email else None
            account.auth_created_at = parse_auth_time(auth_user.get("created_at"))
    await db.commit()
    return len(users)


@router.get("/system")
async def system_health(
    principal: AdminPrincipal = Depends(require(AdminPermission.system_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    database = "ok"
    try:
        await db.execute(select(1))
    except Exception as exc:
        database = "error"
        report_exception(exc, distinct_id=str(principal.user.id), handler="admin_system_health", dependency="database")
    settings = get_settings()
    return {
        "broker": "ok",
        "database": database,
        "posthog": "configured" if settings.posthog_configured else "not_configured",
        "posthog_dashboard_url": settings.posthog_dashboard_url or None,
        "supabase_admin": "configured" if settings.supabase_secret_key else "not_configured",
        "agent_runtime": settings.agent_runtime,
        "resident_agents": get_pool().size(),
        "pool_capacity": settings.agent_pool_max_size,
        "campus_commits": commits_by_slug(settings.campus_mcp_root),
        "checked_at": datetime.now(UTC).isoformat(),
    }

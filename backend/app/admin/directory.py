from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccountDirectory, AccountStatus, Organization

METU_ID = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT_TOUCH_INTERVAL = timedelta(minutes=5)


async def ensure_metu(db: AsyncSession) -> Organization:
    organization = await db.get(Organization, METU_ID)
    if organization is None:
        await db.execute(
            insert(Organization)
            .values(id=METU_ID, slug="metu", name="Middle East Technical University")
            # Both id and slug are unique. Concurrent bootstrap requests can
            # encounter either constraint before the winning row is visible.
            .on_conflict_do_nothing()
        )
        organization = await db.get(Organization, METU_ID)
        if organization is None:
            raise RuntimeError("METU organization could not be initialized")
    return organization


async def touch_account(db: AsyncSession, user_id: UUID, email: str | None) -> AccountDirectory:
    """Ensure the local account exists without writing on every API request."""
    account = await db.get(AccountDirectory, user_id)
    if account is None:
        # Migrations seed the organization in production. Keeping this lazy
        # guard makes clean/test databases robust without touching it on the
        # common authenticated-request path.
        await ensure_metu(db)
        now = datetime.now(UTC)
        account = AccountDirectory(
            user_id=user_id,
            organization_id=METU_ID,
            email=email,
            email_normalized=email.strip().lower() if email else None,
            status=AccountStatus.active,
            last_seen_at=now,
        )
        db.add(account)
        try:
            await db.commit()
            return account
        except IntegrityError:
            # Two first requests for the same newly-created Supabase account
            # can race. The winning insert is the canonical row.
            await db.rollback()
            account = await db.get(AccountDirectory, user_id)
            if account is None:
                raise
    if account.status != AccountStatus.active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is not active")

    now = datetime.now(UTC)
    normalized = email.strip().lower() if email else None
    email_changed = bool(email and (account.email != email or account.email_normalized != normalized))
    touch_due = account.last_seen_at is None or account.last_seen_at <= now - ACCOUNT_TOUCH_INTERVAL
    if email_changed or touch_due:
        if email:
            account.email = email
            account.email_normalized = normalized
        account.last_seen_at = now
        await db.commit()
    return account


async def active_account(db: AsyncSession, user_id: UUID) -> AccountDirectory | None:
    return (
        await db.execute(
            select(AccountDirectory).where(
                AccountDirectory.user_id == user_id,
                AccountDirectory.status == AccountStatus.active,
            )
        )
    ).scalar_one_or_none()


async def synchronize_directory(db: AsyncSession) -> int:
    from app.admin.supabase import SupabaseAdmin, parse_auth_time

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

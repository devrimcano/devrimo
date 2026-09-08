"""Database access for user profiles and campus credentials.

Sits between the API routes and the models so that both the routes and the
agent manager have one place to ask "what campus tools should this student's
container be built with?" without either of them growing its own queries.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.campus.catalog import DEFAULT_ENABLED_TOOL_IDS, normalize_tool_ids
from app.campus.credentials import CampusSecrets, secrets_for
from app.campus.mcp_config import CampusServerSpec, build_server_specs
from app.campus.validation import validate_odtuclass_base_url
from app.config import get_settings
from app.core.crypto import encrypt_secret
from app.db.models import CampusCredential, UserProfile


async def get_profile(db: AsyncSession, user_id: UUID) -> UserProfile | None:
    result = await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    return result.scalar_one_or_none()


async def get_or_create_profile(db: AsyncSession, user_id: UUID) -> UserProfile:
    profile = await get_profile(db, user_id)
    if profile is not None:
        return profile

    profile = UserProfile(user_id=user_id)
    db.add(profile)
    try:
        await db.commit()
    except Exception:
        # Two tabs finishing onboarding at once both try to create the row;
        # the loser just reads what the winner wrote.
        await db.rollback()
        existing = await get_profile(db, user_id)
        if existing is None:
            raise
        return existing
    await db.refresh(profile)
    return profile


async def get_credential(db: AsyncSession, user_id: UUID) -> CampusCredential | None:
    result = await db.execute(select(CampusCredential).where(CampusCredential.user_id == user_id))
    return result.scalar_one_or_none()


async def upsert_credential(
    db: AsyncSession,
    user_id: UUID,
    *,
    metu_username: str,
    metu_password: str | None,
    odtuclass_token: str | None,
    odtuclass_base_url: str | None,
    locale: str,
    enabled_tools: list[str] | None,
    verified: bool,
    verification_error: str | None,
) -> CampusCredential:
    """Create or replace this student's campus connection.

    ``metu_password`` and ``odtuclass_token`` of ``None`` mean "leave the
    stored value alone" — the frontend re-sends the username and tool
    selection when toggling tools, and should not have to re-prompt for a
    password to do it. Passing an empty string clears the stored secret.
    """
    credential = await get_credential(db, user_id)
    if credential is None:
        credential = CampusCredential(
            user_id=user_id,
            metu_username=metu_username,
            enabled_tools=[],
            credential_revision=1,
        )
        db.add(credential)
    else:
        credential.credential_revision += 1

    credential.metu_username = metu_username
    if metu_password is not None:
        credential.metu_password_enc = encrypt_secret(metu_password) if metu_password else None
    if odtuclass_token is not None:
        credential.odtuclass_token_enc = encrypt_secret(odtuclass_token) if odtuclass_token else None
    credential.odtuclass_base_url = validate_odtuclass_base_url(odtuclass_base_url)
    credential.locale = locale
    credential.enabled_tools = normalize_tool_ids(
        enabled_tools if enabled_tools is not None else credential.enabled_tools or list(DEFAULT_ENABLED_TOOL_IDS)
    )
    credential.verified_at = datetime.now(UTC) if verified else None
    credential.verification_error = verification_error
    # Integration leases validate the current credential revision; no model
    # restart is required, including while assistant execution is stopped.
    credential.config_dirty = False

    await db.commit()
    await db.refresh(credential)
    from app.campus.session_pool import retire_user

    await retire_user(user_id)
    return credential


async def delete_credential(db: AsyncSession, user_id: UUID) -> bool:
    credential = await get_credential(db, user_id)
    if credential is None:
        return False
    await db.delete(credential)
    await db.commit()
    from app.campus.session_pool import retire_user

    await retire_user(user_id)
    return True


async def mark_config_applied(db: AsyncSession, user_id: UUID) -> None:
    credential = await get_credential(db, user_id)
    if credential is not None and credential.config_dirty:
        credential.config_dirty = False
        await db.commit()


def enabled_tool_ids(credential: CampusCredential | None) -> list[str]:
    if credential is None:
        return []
    return normalize_tool_ids(credential.enabled_tools or [])


async def users_with_tool(db: AsyncSession, tool_id: str) -> list[UUID]:
    """Students whose credentials are verified and include ``tool_id``.

    Only verified credentials, and only ones carrying a password: an account
    that cannot log in would spend the warm-up's budget on failures and get
    the job stopped for everyone.
    """
    rows = (
        (
            await db.execute(
                select(CampusCredential).where(
                    CampusCredential.verified_at.is_not(None),
                    CampusCredential.metu_password_enc.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return [row.user_id for row in rows if tool_id in enabled_tool_ids(row)]


async def campus_server_specs(db: AsyncSession, user_id: UUID) -> list[CampusServerSpec]:
    """The campus MCP servers this student's agent should be launched with.

    A student with no credentials gets an empty list, which is a real state
    worth building an agent for: they can still talk to it, just without any
    campus tools attached.
    """
    settings = get_settings()
    credential = await get_credential(db, user_id)
    secrets: CampusSecrets | None = secrets_for(credential)
    return build_server_specs(
        user_id,
        enabled_tool_ids(credential),
        secrets,
        mcp_root=settings.campus_mcp_root,
        state_root=settings.campus_state_root,
    )


async def credential_revision(db: AsyncSession, user_id: UUID) -> int:
    credential = await get_credential(db, user_id)
    return credential.credential_revision if credential is not None else 0

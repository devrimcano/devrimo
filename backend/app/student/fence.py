"""Database fencing for campus-derived student data.

The fence is kept on the account row because deletion and a post-fetch write
must serialize across API processes and workers. A process captures the value
before making a campus request, then locks the same row and checks the value
again before committing the fetched data. Deletion advances the value while
holding that row lock.
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccountDirectory, AccountStatus


async def capture_academic_data_fence(db: AsyncSession, user_id: UUID) -> int | None:
    """Capture the current fence only for an active account."""
    return await db.scalar(
        select(AccountDirectory.academic_data_fence).where(
            AccountDirectory.user_id == user_id,
            AccountDirectory.status == AccountStatus.active,
        )
    )


async def lock_current_academic_data_fence(
    db: AsyncSession, user_id: UUID, expected_fence: int
) -> bool:
    """Lock the account row and authorize a write for one captured read."""
    row = (
        await db.execute(
            select(AccountDirectory.status, AccountDirectory.academic_data_fence)
            .where(AccountDirectory.user_id == user_id)
            .with_for_update()
        )
    ).one_or_none()
    return bool(
        row is not None
        and row.status == AccountStatus.active
        and row.academic_data_fence == expected_fence
    )


async def advance_academic_data_fence(db: AsyncSession, user_id: UUID) -> int | None:
    """Advance the erasure fence atomically while locking the account row."""
    return await db.scalar(
        update(AccountDirectory)
        .where(AccountDirectory.user_id == user_id)
        .values(academic_data_fence=AccountDirectory.academic_data_fence + 1)
        .returning(AccountDirectory.academic_data_fence)
    )

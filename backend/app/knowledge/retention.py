"""Reclamation of rows that expire but are never read again.

``schedule_data_cache`` is the one table in the schema whose rows have a
deadline and no owner watching them. Nothing else reclaims them: the read path
reports an expired row as a miss and deliberately leaves it in place, so a
refresh that fails still has something to fall back on. A plan key includes the
requested course pool, so a student who tries five different pools leaves four
rows no read will ever return to; the table would otherwise grow with attempts
rather than with students.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import ScheduleDataCache

# Deleting in bounded batches keeps one sweep off a long row lock on a table the
# schedule page reads synchronously. A backlog just takes several passes.
SWEEP_BATCH_SIZE = 1000

# How long an expired row is kept before it is reclaimed.
#
# The read path reports an expired row as a miss but no longer deletes it,
# because the refresh that miss triggers can fail — a campus server that times
# out answers with an empty page, not an error — and a slightly stale course
# list is worth more than nothing while that is being retried. This is the
# window in which that fallback still exists. It costs a few days of rows on a
# table whose entries are a few kilobytes each.
STALE_GRACE = timedelta(days=3)


async def sweep_expired_schedule_cache(db: AsyncSession, *, batch_size: int = SWEEP_BATCH_SIZE) -> int:
    """Delete expired cache rows, returning how many went. Commits its own work."""
    # Aliased because the subquery reads the table the DELETE targets: without
    # it SQLAlchemy auto-correlates the inner SELECT away and the statement
    # stops meaning "the oldest N expired rows".
    stale = aliased(ScheduleDataCache)
    expired = (
        select(stale.key_hash)
        .where(stale.expires_at <= datetime.now(UTC) - STALE_GRACE)
        .order_by(stale.expires_at)
        .limit(batch_size)
        .scalar_subquery()
    )
    result = await db.execute(
        delete(ScheduleDataCache)
        .where(ScheduleDataCache.key_hash.in_(expired))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount or 0

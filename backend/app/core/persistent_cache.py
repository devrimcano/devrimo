"""A cache that survives a restart, for answers that cost a campus round trip.

The in-process :class:`~app.core.ttl_cache.TTLCache` is the right tool for a
single request storm — it collapses concurrent misses and costs nothing — but
it dies with the worker and is never shared between replicas. Catalog data is
the opposite shape: the course names for a department are the same for every
student who asks, they change when the registrar publishes rather than between
page loads, and fetching them spawns that student's whole campus toolkit.

So the two are layered rather than swapped. The in-process cache stays in front
for the stampede; this one sits behind it so a restart, a second replica, or a
different student does not pay the round trip again.

Every failure here is swallowed. A cache that cannot be read or written must
degrade to "no cache", never to a failed request.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScheduleDataCache
from app.db.session import SessionLocal
from app.logging import get_logger

logger = get_logger(__name__)


# How long an empty first answer is held. Short on purpose: see the comment at
# the write site. Long enough that a page opening ten courses at once does not
# hit the campus ten times for the same failing read.
EMPTY_FIRST_WRITE_TTL_SECONDS = 15 * 60


def _unwrap(payload: Any) -> Any:
    """The stored value, without the object wrapper the JSON column needs."""
    if isinstance(payload, dict) and set(payload) == {"value"}:
        return payload["value"]
    return payload


def _is_empty(value: Any) -> bool:
    """Whether an answer carries nothing worth keeping.

    Not the same as falsy: a stored ``0`` or ``False`` is a real answer. This
    is about a campus read that produced no rows — an empty list, an empty
    object, an empty string, or a wrapper containing one of those.
    """
    value = _unwrap(value)
    if value is None:
        return True
    if isinstance(value, (list, tuple, set, str)):
        return len(value) == 0
    if isinstance(value, dict):
        return not value or all(_is_empty(item) for item in value.values())
    return False


async def read_cached(key_hash: str) -> Any | None:
    """The stored payload for ``key_hash``, or ``None`` if absent or expired."""
    try:
        async with SessionLocal() as db:
            row = await db.get(ScheduleDataCache, key_hash)
            if row is None:
                return None
            expires_at = row.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                # Reported as a miss but left in place. The refresh it triggers
                # can fail — the campus server times out, the page comes back
                # empty — and a stale course list is worth more than none while
                # that is happening. The sweep reclaims it after its grace
                # period; deleting it here removed the fallback at exactly the
                # moment it was about to be needed.
                return None
            payload = row.payload
            # Stored as a JSON object because the column is one; the wrapper is
            # unwrapped here so callers can cache lists and scalars too.
            if isinstance(payload, dict) and set(payload) == {"value"}:
                return payload["value"]
            return payload
    except Exception as exc:
        logger.warning("persistent_cache_read_failed", key=key_hash[:12], error=str(exc))
        return None


async def cached_expiry(key_hash: str) -> datetime | None:
    """Return the authoritative expiry for a live persistent cache row.

    Planner evidence signs this deadline so a token issued from a nearly
    expired Course Info answer cannot extend the answer's seven-day lifetime.
    The payload itself is intentionally not returned here.
    """

    try:
        async with SessionLocal() as db:
            row = await db.get(ScheduleDataCache, key_hash)
            if row is None:
                return None
            expires_at = row.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            return expires_at if expires_at > datetime.now(UTC) else None
    except Exception as exc:
        logger.warning("persistent_cache_expiry_read_failed", key=key_hash[:12], error=str(exc))
        return None


async def read_many_cached(key_hashes: list[str]) -> dict[str, Any]:
    """Every live row among ``key_hashes``, in one query.

    Searching course titles across the university means reading one cached
    listing per department. One round trip for all of them is the difference
    between a usable search box and a hundred and fifty sequential reads per
    keystroke. Missing and expired keys are simply absent from the result:
    the caller decides whether to fetch them, and a search must never block on
    a hundred and fifty campus round trips.
    """
    if not key_hashes:
        return {}
    try:
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(ScheduleDataCache).where(ScheduleDataCache.key_hash.in_(key_hashes))
                )
            ).scalars().all()
    except Exception as exc:
        logger.warning("persistent_cache_bulk_read_failed", keys=len(key_hashes), error=str(exc))
        return {}
    now = datetime.now(UTC)
    found: dict[str, Any] = {}
    for row in rows:
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            continue
        payload = row.payload
        if isinstance(payload, dict) and set(payload) == {"value"}:
            payload = payload["value"]
        found[row.key_hash] = payload
    return found


async def write_cached(
    key_hash: str,
    payload: Any,
    *,
    namespace: str,
    ttl_seconds: float,
    owner_hash: str | None = None,
) -> None:
    """Store ``payload`` under ``key_hash`` for ``ttl_seconds``.

    ``owner_hash`` is what makes a row deletable when a student erases their
    data, so it must be set for anything derived from one student and left
    ``None`` for shared catalog data that belongs to nobody.
    """
    body = payload if isinstance(payload, dict) else {"value": payload}
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    try:
        async with SessionLocal() as db:
            row = await db.get(ScheduleDataCache, key_hash)
            if row is not None and _is_empty(payload) and not _is_empty(_unwrap(row.payload)):
                # A refresh that came back with nothing does not get to replace
                # what is already stored. Campus reads fail by returning an
                # empty page far more often than a department genuinely loses
                # all its courses, and writing that emptiness would keep every
                # student reading it for the whole TTL. The row is kept and its
                # life extended, so the next refresh gets another turn.
                row.expires_at = expires_at
                await db.commit()
                logger.info("persistent_cache_kept_over_empty", key=key_hash[:12], namespace=namespace)
                return
            if row is None:
                if _is_empty(payload):
                    # Nothing stored yet, and the answer is empty. The guard
                    # above cannot help — there is no good row to keep — and
                    # writing this at the caller's lifetime would hold a campus
                    # read that returned an empty page for up to thirty days.
                    # Worse, ``read_cached`` would then answer ``[]`` rather
                    # than a miss, so nothing would ever refetch it: one bad
                    # minute at METU and a department has no courses until the
                    # TTL runs out. Kept briefly instead — long enough to
                    # collapse a burst of identical requests, short enough that
                    # the next reader tries again.
                    expires_at = datetime.now(UTC) + timedelta(
                        seconds=min(ttl_seconds, EMPTY_FIRST_WRITE_TTL_SECONDS)
                    )
                    logger.info(
                        "persistent_cache_empty_first_write", key=key_hash[:12], namespace=namespace
                    )
                db.add(
                    ScheduleDataCache(
                        key_hash=key_hash,
                        owner_hash=owner_hash,
                        namespace=namespace,
                        payload=body,
                        expires_at=expires_at,
                    )
                )
            else:
                row.payload = body
                row.owner_hash = owner_hash
                row.namespace = namespace
                row.expires_at = expires_at
            await db.commit()
    except Exception as exc:
        logger.warning("persistent_cache_write_failed", key=key_hash[:12], error=str(exc))


async def purge_namespace(db: AsyncSession, namespace: str) -> int:
    """Drop every row in one namespace. Used when a cached shape changes."""
    from sqlalchemy import delete

    result = await db.execute(
        delete(ScheduleDataCache)
        .where(ScheduleDataCache.namespace == namespace)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount or 0

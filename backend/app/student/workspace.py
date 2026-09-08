"""Student-owned editable resources shared by HTTP and assistant adapters."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.digest import stable_digest
from app.db.models import (
    AccountDirectory,
    CampusKnowledgeRecord,
    CampusSource,
    StudentResourceRevision,
    UserPreference,
    UserUpdateState,
)
from app.student.service import validate_preference


def _resource(kind: str, key: str) -> str:
    if kind not in {"preference", "update"} or not key or len(key) > 96:
        raise HTTPException(422, "Unsupported editable resource")
    return f"{kind}:{key}"


async def _current(db: AsyncSession, user_id: UUID, kind: str, key: str) -> dict:
    if kind == "preference":
        row = await db.scalar(
            select(UserPreference).where(UserPreference.user_id == user_id, UserPreference.key == key)
        )
        return {"key": key, "value": row.value if row else None}
    try:
        record_id = UUID(key)
    except ValueError as exc:
        raise HTTPException(422, "Invalid update reference") from exc
    accessible = await db.scalar(
        select(CampusKnowledgeRecord.id)
        .join(CampusSource)
        .join(
            AccountDirectory,
            AccountDirectory.organization_id == CampusSource.organization_id,
        )
        .where(
            AccountDirectory.user_id == user_id,
            CampusKnowledgeRecord.id == record_id,
            CampusKnowledgeRecord.is_current.is_(True),
            CampusSource.enabled.is_(True),
            CampusSource.status == "published",
        )
    )
    if accessible is None:
        raise HTTPException(404, "Update not found")
    row = await db.get(UserUpdateState, (user_id, record_id))
    return {"record_id": key, "read": bool(row and row.read_at), "dismissed": bool(row and row.dismissed_at)}


async def read_resource(db: AsyncSession, user_id: UUID, kind: str, key: str) -> dict:
    resource = _resource(kind, key)
    payload = await _current(db, user_id, kind, key)
    latest = await db.scalar(
        select(StudentResourceRevision)
        .where(
            StudentResourceRevision.user_id == user_id,
            StudentResourceRevision.resource == resource,
        )
        .order_by(StudentResourceRevision.revision.desc())
        .limit(1)
    )
    return {"revision": latest.revision if latest else 0, "data": payload}


async def update_resource(
    db: AsyncSession,
    user_id: UUID,
    kind: str,
    key: str,
    changes: dict,
    expected_revision: int | None,
    idempotency_key: str,
) -> dict:
    resource = _resource(kind, key)
    if not 1 <= len(idempotency_key) <= 128:
        raise HTTPException(422, "An idempotency key of at most 128 characters is required")
    if kind == "preference":
        if set(changes) != {"value"} or not isinstance(changes["value"], (dict, type(None))):
            raise HTTPException(422, "Preference changes must contain value (object or null to delete)")
        try:
            validate_preference(key, changes["value"] or {})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    elif not changes or set(changes) - {"read", "dismissed"} or any(type(v) is not bool for v in changes.values()):
        raise HTTPException(422, "Update state accepts read and dismissed booleans only")
    request_hash = stable_digest(changes)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"student-resource:{user_id}:{resource}"},
    )
    previous = await db.scalar(
        select(StudentResourceRevision).where(
            StudentResourceRevision.user_id == user_id,
            StudentResourceRevision.resource == resource,
            StudentResourceRevision.idempotency_key == idempotency_key,
        )
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Idempotency key was already used for different changes")
        return {"revision": previous.revision, "data": previous.payload}
    current = await read_resource(db, user_id, kind, key)
    if expected_revision is not None and expected_revision != current["revision"]:
        raise HTTPException(409, {"error": "revision_conflict", "current": current})
    now = datetime.now(UTC)
    if kind == "preference":
        row = await db.scalar(
            select(UserPreference).where(UserPreference.user_id == user_id, UserPreference.key == key)
        )
        if changes["value"] is None:
            await db.execute(delete(UserPreference).where(UserPreference.user_id == user_id, UserPreference.key == key))
        elif row is None:
            db.add(
                UserPreference(user_id=user_id, key=key, value=changes["value"], provenance="explicit", confidence=1)
            )
        else:
            row.value, row.provenance, row.confidence = changes["value"], "explicit", 1
    else:
        row = await db.get(UserUpdateState, (user_id, UUID(key)))
        if row is None:
            row = UserUpdateState(user_id=user_id, record_id=UUID(key))
            db.add(row)
        if "read" in changes:
            row.read_at = now if changes["read"] else None
        if "dismissed" in changes:
            row.dismissed_at = now if changes["dismissed"] else None
    await db.flush()
    payload = await _current(db, user_id, kind, key)
    revision = current["revision"] + 1
    db.add(
        StudentResourceRevision(
            user_id=user_id,
            resource=resource,
            revision=revision,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            payload=payload,
        )
    )
    await db.commit()
    return {"revision": revision, "data": payload}

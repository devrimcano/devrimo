from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.campus.course_info import forget_user
from app.db.models import StudentAcademicSnapshot, StudentContext, UserPreference, UserUpdateState
from app.db.session import get_db
from app.logging import get_logger
from app.observability.client import report_exception
from app.planning.groups import get_course_group
from app.planning.mcp_bridge import sync_planning_snapshot_from_sais, sync_student_context_from_sais
from app.planning.service import SemesterPlanRequest, plan_semester
from app.student import service
from app.student.updates import get_updates

router = APIRouter()
logger = get_logger(__name__)


class ContextIn(BaseModel):
    department: str | None = Field(default=None, max_length=255)
    degree_level: Literal["undergraduate", "masters", "doctoral", "exchange", "other"] | None = None
    program_code: str | None = Field(default=None, max_length=32)
    campus: str | None = Field(default=None, max_length=255)
    confirm_verified: bool = False


class PreferenceIn(BaseModel):
    value: dict[str, Any]


class GroupRequestIn(BaseModel):
    term: str = Field(min_length=3, max_length=32)
    course_code: str = Field(min_length=2, max_length=32)
    section: str | None = Field(default=None, max_length=16)


class AcademicSyncIn(BaseModel):
    term: str = Field(min_length=3, max_length=32)
    force: bool = False


def _context_out(context: StudentContext) -> dict:
    return {
        "department": context.department,
        "degree_level": context.degree_level,
        "program_code": context.program_code,
        "campus": context.campus,
        "source": context.source,
        "verified_at": context.verified_at,
        "confirmed_at": context.confirmed_at,
        "needs_confirmation": context.verified_at is not None and context.confirmed_at is None,
    }


def _course_code(item: Any) -> str | None:
    if isinstance(item, str):
        return item.strip() or None
    if not isinstance(item, dict):
        return None
    for key in ("course_code", "courseCode", "code", "course", "ders_kodu"):
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


async def _academic_data_out(db: AsyncSession, user_id: UUID) -> dict:
    context = await db.get(StudentContext, user_id)
    snapshots = list(
        (
            await db.scalars(
                select(StudentAcademicSnapshot)
                .where(StudentAcademicSnapshot.user_id == user_id)
                .order_by(StudentAcademicSnapshot.fetched_at.desc())
            )
        ).all()
    )
    return {
        "context": _context_out(context) if context else None,
        "snapshots": [
            {
                "term": snapshot.term,
                "completed_course_count": len(snapshot.completed_courses),
                "completed_course_codes": [
                    code for item in snapshot.completed_courses if (code := _course_code(item))
                ],
                "enrolled_course_count": len(snapshot.enrolled_courses),
                "fetched_at": snapshot.fetched_at,
                "source": snapshot.source,
            }
            for snapshot in snapshots
        ],
        "has_cached_data": bool(
            snapshots or (context and (context.department or context.program_code or context.degree_level))
        ),
    }


@router.get("/academic-data")
async def academic_data_get(
    user: AuthenticatedUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return await _academic_data_out(db, user.id)


@router.post("/academic-data/sync")
async def academic_data_sync(
    body: AcademicSyncIn,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    context = await db.get(StudentContext, user.id)
    latest_snapshot = await db.scalar(
        select(StudentAcademicSnapshot)
        .where(StudentAcademicSnapshot.user_id == user.id)
        .order_by(StudentAcademicSnapshot.fetched_at.desc())
        .limit(1)
    )
    try:
        # A transcript describes accumulated course history. Reuse the newest
        # snapshot across planning terms until the user explicitly refreshes it.
        # A student with nothing on their transcript yet still syncs
        # successfully: the reply carries an empty snapshot, not an error.
        if body.force or latest_snapshot is None:
            reached_sais = await sync_planning_snapshot_from_sais(user.id, body.term)
        elif context is None or not (context.department or context.program_code):
            reached_sais = await sync_student_context_from_sais(user.id)
        else:
            reached_sais = True
    except Exception as exc:
        logger.warning("academic_data_sync_failed", user_id=str(user.id), error=str(exc))
        report_exception(
            exc,
            distinct_id=str(user.id),
            handler="academic_data_sync",
            dependency="sais",
            forced=body.force,
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Academic data could not be fetched from SAIS") from exc
    if not reached_sais:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "SAIS did not answer. Check that your METU connection is still valid, then try again.",
        )
    forget_user(user.id)
    await db.rollback()
    return await _academic_data_out(db, user.id)


@router.delete("/academic-data")
async def academic_data_delete(
    user: AuthenticatedUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    await db.execute(delete(StudentAcademicSnapshot).where(StudentAcademicSnapshot.user_id == user.id))
    await db.execute(delete(StudentContext).where(StudentContext.user_id == user.id))
    await db.commit()
    # Removing the rows is not enough on its own: the catalog cache still holds
    # answers derived from this student's department until they expire.
    forget_user(user.id)
    return {"deleted": True}


@router.get("/context")
async def context_get(
    user: AuthenticatedUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return _context_out(await service.get_context(db, user.id))


@router.put("/context")
async def context_put(
    body: ContextIn,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    context = await service.get_context(db, user.id)
    if body.confirm_verified:
        if context.verified_at is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "There is no verified SAIS context to confirm")
        context.confirmed_at = datetime.now(UTC)
    else:
        context.department = body.department
        context.degree_level = body.degree_level
        context.program_code = body.program_code
        context.campus = body.campus
        context.source = "manual"
        context.verified_at = None
        context.confirmed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(context)
    return _context_out(context)


@router.get("/preferences")
async def preferences_get(
    user: AuthenticatedUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    preferences = await service.list_preferences(db, user.id)
    return {
        "items": [
            {
                "key": item.key,
                "value": item.value,
                "provenance": item.provenance,
                "confidence": float(item.confidence),
                "updated_at": item.updated_at,
            }
            for item in preferences
        ],
        "allowed_keys": sorted(service.ALLOWED_PREFERENCE_KEYS),
    }


@router.put("/preferences/{key}")
async def preference_put(
    key: str,
    body: PreferenceIn,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        item = await service.save_preference(
            db, user.id, key=key, value=body.value, provenance="explicit", confidence=1
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {"key": item.key, "value": item.value, "provenance": item.provenance, "confidence": 1}


@router.delete("/preferences/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def preference_delete(
    key: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await db.execute(delete(UserPreference).where(UserPreference.user_id == user.id, UserPreference.key == key))
    await db.commit()


@router.get("/updates")
async def updates_get(
    digest: bool = False,
    limit: int = Query(default=30, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await get_updates(db, user.id, digest=digest, limit=limit)


@router.put("/updates/{record_id}/state")
async def update_state(
    record_id: UUID,
    read: bool | None = None,
    dismissed: bool | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    state = await db.get(UserUpdateState, (user.id, record_id))
    if state is None:
        state = UserUpdateState(user_id=user.id, record_id=record_id)
        db.add(state)
    now = datetime.now(UTC)
    if read is not None:
        state.read_at = now if read else None
    if dismissed is not None:
        state.dismissed_at = now if dismissed else None
    await db.commit()
    return {"record_id": str(record_id), "read": state.read_at is not None, "dismissed": state.dismissed_at is not None}


@router.post("/plan")
async def semester_plan(
    body: SemesterPlanRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    snapshot = await db.get(StudentAcademicSnapshot, (user.id, body.term))
    if snapshot is None:
        try:
            if await sync_planning_snapshot_from_sais(user.id, body.term):
                # The refresh commits in a separate short-lived session. End
                # this read transaction so the planner sees the new snapshot.
                await db.rollback()
        except Exception as exc:
            # Preserve the planner's established needs_academic_snapshot
            # response when SAIS is disconnected or temporarily unavailable.
            logger.warning("planning_snapshot_sync_failed", user_id=str(user.id), error=str(exc))
            report_exception(
                exc,
                distinct_id=str(user.id),
                handler="planning_snapshot_sync",
                dependency="sais",
            )
    return await plan_semester(db, user.id, body)


@router.post("/course-group")
async def course_group(
    body: GroupRequestIn,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await get_course_group(
        db, user.id, term=body.term, course_code=body.course_code, section=body.section
    )

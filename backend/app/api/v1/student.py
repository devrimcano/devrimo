from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.campus.course_info import forget_user
from app.campus.eligibility import tr_upper
from app.db.models import StudentAcademicSnapshot, StudentContext
from app.db.session import get_db
from app.logging import get_logger
from app.observability.client import report_exception
from app.planning.groups import get_course_group
from app.planning.mcp_bridge import sync_planning_snapshot_from_sais, sync_student_context_from_sais
from app.planning.service import SemesterPlanRequest, plan_semester
from app.student import service, workspace
from app.student.purge import purge_academic_data
from app.student.updates import get_updates

router = APIRouter()
logger = get_logger(__name__)


class ContextIn(BaseModel):
    department: str | None = Field(default=None, max_length=255)
    surname_prefix: str | None = Field(default=None, max_length=8)
    degree_level: Literal["undergraduate", "masters", "doctoral", "exchange", "other"] | None = None
    year_of_study: int | None = Field(default=None, ge=1, le=9)
    program_code: str | None = Field(default=None, max_length=32)
    campus: str | None = Field(default=None, max_length=255)


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
        # Two letters only - see StudentContext.surname_prefix. Enough to
        # answer a section's surname range, not enough to be a name.
        "surname_prefix": context.surname_prefix,
        "degree_level": context.degree_level,
        # Compared against a section's min/max year band.
        "year_of_study": context.year_of_study,
        "program_code": context.program_code,
        "campus": context.campus,
        "source": context.source,
        "verified_at": context.verified_at,
        "confirmed_at": context.confirmed_at,
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
            snapshots
            or (
                context
                and (
                    context.department
                    or context.program_code
                    or context.degree_level
                    or context.surname_prefix
                    or context.year_of_study
                )
            )
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
    await purge_academic_data(db, user.id)
    await db.commit()
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
    # There is no separate "confirm what SAIS said" call any more: a SAIS read
    # stores its own confirmation, because a student cannot meaningfully
    # confirm their own registrar. This endpoint is the correction path.
    #
    # A patch, not a replacement. Every field used to be assigned from the body
    # unconditionally, so any request that omitted one set it to NULL — and a
    # save from a form that had not finished loading wiped a department SAIS
    # had just read, silently and unrecoverably. ``model_fields_set`` is the
    # difference between "the client did not mention this" and "the client
    # asked to clear it": an omitted key is left alone, an explicit null still
    # clears.
    supplied = body.model_fields_set
    if "department" in supplied:
        context.department = body.department
    if "surname_prefix" in supplied:
        # The form may send a whole surname; only the first two letters are
        # ever stored, because that is all a section's range compares.
        context.surname_prefix = tr_upper("".join((body.surname_prefix or "").split()))[:2] or None
    if "degree_level" in supplied:
        context.degree_level = body.degree_level
    if "year_of_study" in supplied:
        context.year_of_study = body.year_of_study
    if "program_code" in supplied:
        context.program_code = body.program_code
    if "campus" in supplied:
        context.campus = body.campus
    if not supplied:
        # Nothing was asked for. Saying so beats recording a "manual" edit that
        # changed nothing and threw away the SAIS provenance on the way.
        return _context_out(context)
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
    result = await workspace.update_resource(db, user.id, "preference", key, {"value": body.value}, None, str(uuid4()))
    return {**result["data"], "provenance": "explicit", "confidence": 1, "revision": result["revision"]}


@router.delete("/preferences/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def preference_delete(
    key: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await workspace.update_resource(db, user.id, "preference", key, {"value": None}, None, str(uuid4()))


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
    changes = {key: value for key, value in {"read": read, "dismissed": dismissed}.items() if value is not None}
    if not changes:
        result = await workspace.read_resource(db, user.id, "update", str(record_id))
    else:
        result = await workspace.update_resource(db, user.id, "update", str(record_id), changes, None, str(uuid4()))
    return {**result["data"], "revision": result["revision"]}


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

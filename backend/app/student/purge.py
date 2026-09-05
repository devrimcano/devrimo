"""Erasure of a student's stored campus data, in one place.

Two callers need this and they used to disagree about what "delete" covers.
``DELETE /student/academic-data`` cleared the transcript snapshot, the context
and the schedule cache; the admin's permanent account deletion cleared none of
them, so a transcript — completed courses, credits and grade points, which
:class:`~app.db.models.StudentAcademicSnapshot` documents as sensitive —
outlived the account it belonged to. Splitting the work into an academic tier
and a whole-account tier keeps the student-facing endpoint narrow while giving
account deletion something that is complete by construction.

Neither function commits: the caller owns the transaction, because account
deletion has to succeed or fail as one unit with the rest of its work.
"""

from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.campus.course_info import forget_user
from app.core.digest import owner_digest
from app.db.models import (
    ScheduleDataCache,
    StudentAcademicSnapshot,
    StudentContext,
    StudentTimetable,
    UserMailFact,
    UserPreference,
    UserUpdateState,
)


async def purge_academic_data(db: AsyncSession, user_id: UUID) -> None:
    """Everything derived from the student's SAIS record, including caches.

    Removing the rows is not enough on its own: the in-process catalog cache
    still holds answers derived from this student's department until they
    expire, so it is forgotten here too.
    """
    await db.execute(delete(StudentAcademicSnapshot).where(StudentAcademicSnapshot.user_id == user_id))
    await db.execute(delete(StudentContext).where(StudentContext.user_id == user_id))
    await db.execute(delete(StudentTimetable).where(StudentTimetable.user_id == user_id))
    await db.execute(delete(ScheduleDataCache).where(ScheduleDataCache.owner_hash == owner_digest(user_id)))
    forget_user(user_id)


async def purge_student_data(db: AsyncSession, user_id: UUID) -> None:
    """The academic tier plus everything else keyed to this account.

    Called when the account itself goes away, so the standard is "nothing keyed
    to this user id remains", not "the tables this feature happens to know
    about". ``agent_tool_audit`` is deliberately not included: it holds no
    campus data — status, duration, and a SHA-256 argument digest — and is the
    record of external actions taken on the student's behalf.
    """
    await purge_academic_data(db, user_id)
    await db.execute(delete(UserPreference).where(UserPreference.user_id == user_id))
    await db.execute(delete(UserMailFact).where(UserMailFact.user_id == user_id))
    await db.execute(delete(UserUpdateState).where(UserUpdateState.user_id == user_id))

"""Course catalog access for the visual schedule builder.

The catalog reads go straight to the connected Course Info MCP server through
:mod:`app.campus.course_info` — no Agent run, language model, prompt, memory or
learning pass is involved, and they cost no tokens. Only ``/ai-plan`` starts an
agent, and it holds the same turn lock a chat turn does.
"""

import asyncio
import json
import re
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import manager
from app.api.v1.schedule_prompt import PLANNER_PROMPT
from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.campus import service as campus_service
from app.campus.course_info import (
    call_course_info,
    department_for_course,
    json_value,
    resolve_department,
)
from app.core.ttl_cache import TTLCache
from app.db.models import StudentAcademicSnapshot, StudentContext
from app.db.session import get_db
from app.logging import get_logger
from app.observability.client import report_exception
from app.planning.mcp_bridge import sync_student_context_from_sais

router = APIRouter()
logger = get_logger(__name__)

# Filling a student's context spawns their whole campus toolkit as subprocesses,
# so the schedule page mounting must not be able to start one per render. The
# outcome is remembered either way: a student whose SAIS reports no department
# would otherwise pay for four subprocess launches on every page view.
#
# Keyed by campus credential revision, not by user alone. Remembering a failure
# is the point, but a student who fails this lookup, connects METU, and comes
# back must not be told "no department" for the rest of the window — connecting
# bumps the revision, which retires the old key on the spot.
_context_syncs = TTLCache(ttl_seconds=5 * 60, max_entries=1024)

_AGENT_RUN_TIMEOUT_SECONDS = 180


class AiScheduleCourse(BaseModel):
    code: str = Field(min_length=3, max_length=20)


class AiScheduleRequest(BaseModel):
    department: str = Field(min_length=3, max_length=20)
    semester: str = Field(min_length=4, max_length=20)
    courses: list[AiScheduleCourse] = Field(default_factory=list, max_length=20)


async def _student_department(
    db: AsyncSession, user_id, context: StudentContext | None
) -> tuple[str | None, str | None]:
    """The student's department as ``(query, three-digit code)``.

    A program code that already carries the department — three digits, or the
    seven-digit form whose first three are the department — is authoritative.
    Anything else is a name, and a name is resolved by asking the catalog
    rather than by pattern-matching digits out of it.
    """
    if context is None:
        return None, None
    query = context.department or context.program_code
    digits = re.sub(r"\D", "", context.program_code or "")
    if len(digits) == 3:
        return query, digits
    if len(digits) == 7:
        return query, digits[:3]
    if not query:
        return None, None
    try:
        return query, await resolve_department(db, user_id, query)
    except HTTPException:
        return query, None


@router.get("/student-context")
async def student_context(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    context = await db.get(StudentContext, user.id)
    if context is None or not (context.department or context.program_code):
        # Single-flight: concurrent mounts of the schedule page wait on one
        # sync instead of each spawning the student's campus servers.
        revision = await campus_service.credential_revision(db, user.id)
        await _context_syncs.run((str(user.id), revision), lambda: _sync_context(user.id))
        await db.rollback()
        context = await db.get(StudentContext, user.id)

    query, code = await _student_department(db, user.id, context)
    return {
        "student": (
            {
                "department": context.department,
                "degree_level": context.degree_level,
                "program_code": context.program_code,
                "campus": context.campus,
                "source": context.source,
            }
            if context
            else None
        ),
        "department_query": query,
        "department_code": code,
    }


async def _sync_context(user_id) -> bool:
    try:
        return await sync_student_context_from_sais(user_id)
    except Exception as exc:
        logger.warning("schedule_context_sync_failed", user_id=str(user_id), error=str(exc))
        report_exception(
            exc,
            distinct_id=str(user_id),
            handler="schedule_context_sync",
            operation="sync_student_context_from_sais",
            dependency="sais",
        )
        return False


@router.get("/departments/search")
async def search_departments(
    query: str = Query(min_length=1, max_length=100),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return {"data": await call_course_info(db, user.id, "search_departments", {"query": query})}


@router.get("/courses")
async def courses(
    department: str = Query(min_length=1, max_length=20),
    semester: str = Query(min_length=1, max_length=20),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return {
        "data": await call_course_info(
            db, user.id, "list_program_courses", {"department": department, "semester": semester}
        )
    }


@router.get("/courses/{course_code}")
async def course_sections(
    course_code: str = Path(min_length=3, max_length=20),
    department: str = Query(min_length=1, max_length=20),
    semester: str = Query(min_length=1, max_length=20),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    compact_course = course_code.upper().replace(" ", "").replace("-", "")
    lookup_department = await department_for_course(db, user.id, compact_course, department)
    # A METU course code is its owning department's three digits followed by a
    # four-digit course number. Expand the short forms a student may type into
    # that, against the department that actually owns the course.
    if not compact_course.isdigit() and (number := re.search(r"(\d{3,4})$", compact_course)):
        compact_course = f"{lookup_department}{number.group(1).zfill(4)}"
    elif compact_course.isdigit() and len(compact_course) in (3, 4):
        compact_course = f"{lookup_department}{compact_course.zfill(4)}"
    return {
        "data": await call_course_info(
            db,
            user.id,
            "get_course_info",
            {"department": lookup_department, "semester": semester, "course": compact_course},
        )
    }


def _json_from_agent(value: Any) -> dict[str, Any]:
    content = getattr(value, "content", value)
    if not isinstance(content, str):
        content = json.dumps(json_value(content), ensure_ascii=False)
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "Schedule assistant returned an invalid response"
            ) from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Schedule assistant returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Schedule assistant response must be an object")
    return parsed


def _completed_codes(snapshot: StudentAcademicSnapshot | None) -> list[str]:
    codes: list[str] = []
    for item in snapshot.completed_courses if snapshot else []:
        if isinstance(item, str) and item.strip():
            codes.append(item.strip())
        elif isinstance(item, dict):
            for key in ("course_code", "courseCode", "code", "course", "ders_kodu"):
                value = item.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    codes.append(str(value).strip())
                    break
    return codes


def _prompt(department: str, semester: str, requested: list[str], completed: list[str]) -> str:
    return PLANNER_PROMPT.format(
        department=department,
        semester=semester,
        requested=json.dumps(requested, ensure_ascii=False),
        completed=json.dumps(completed, ensure_ascii=False),
    )


@router.post("/ai-plan")
async def ai_schedule_plan(
    body: AiScheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Use one bounded agent run to fill gaps left by direct catalog calls."""
    snapshot = await db.scalar(
        select(StudentAcademicSnapshot)
        .where(StudentAcademicSnapshot.user_id == user.id)
        .order_by(StudentAcademicSnapshot.fetched_at.desc())
        .limit(1)
    )
    agent_record = await manager.get_agent_or_404(db, user.id)
    lock_owner = f"schedule-{uuid4()}"
    if not await manager.acquire_turn_lock(db, agent_record, lock_owner):
        raise HTTPException(status.HTTP_409_CONFLICT, "Agent is busy with another message")
    lease = None
    try:
        # The lock's lease is shorter than this run is allowed to take, so it
        # has to be renewed for as long as the run holds it — otherwise it
        # expires mid-run and a chat turn is free to drive the same agent.
        async with manager.turn_lock_held(agent_record.id, lock_owner):
            lease = await manager.lease_for(db, agent_record)
            from app.agents.scholar.context import build_run_dependencies

            dependencies = await build_run_dependencies(db, user.id, lease.resident)
            # Only course identifiers enter the instruction. Display names can
            # be user-authored, so keeping them out prevents prompt-shaped names
            # from changing the planner contract.
            result = await asyncio.wait_for(
                lease.agent.arun(
                    input=_prompt(
                        body.department,
                        body.semester,
                        [item.code for item in body.courses],
                        _completed_codes(snapshot),
                    ),
                    session_id=f"schedule-{uuid4()}",
                    user_id=str(user.id),
                    dependencies=dependencies,
                    stream=False,
                ),
                timeout=_AGENT_RUN_TIMEOUT_SECONDS,
            )
        payload = _json_from_agent(result)
        # The shape is instructed, not enforced: a model that answers with a
        # null or an object where a list belongs must not become a 502.
        raw_warnings = payload.get("warnings")
        raw_courses = payload.get("courses")
        warnings = [
            warning
            for warning in (raw_warnings if isinstance(raw_warnings, list) else [])
            if isinstance(warning, str)
            and not re.search(r"section|meeting time|şube|ders saat", warning, re.IGNORECASE)
        ]
        return {
            "courses": [
                course
                for course in (raw_courses if isinstance(raw_courses, list) else [])
                if isinstance(course, dict)
            ],
            "warnings": warnings,
            "source": "ai_verified",
        }
    except TimeoutError as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "Course recommendation timed out; try again") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("schedule_agent_failed", user_id=str(user.id), error=str(exc))
        report_exception(exc, distinct_id=str(user.id), handler="schedule_recommendation", dependency="agent")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The course recommendation service failed. Department and term were not changed; try this step again.",
        ) from exc
    finally:
        if lease is not None:
            await lease.release()
        # A failed run can leave the request's session mid-transaction, and the
        # lock must come off even then: releasing it on a fresh session keeps a
        # broken transaction from stranding the agent for the whole lease.
        await manager.release_turn_lock_isolated(agent_record.id, lock_owner)

"""Broad first-party tools backed by the shared campus intelligence services."""

from datetime import datetime
from uuid import UUID

from fastapi import HTTPException

from app.admin.directory import active_account
from app.campus import departments as department_directory
from app.campus.course_info import call_course_info, catalog_session
from app.db.session import SessionLocal
from app.knowledge.retrieval import SearchFilters, search_knowledge
from app.knowledge.retrieval import read_campus_page as read_indexed_page
from app.planning.catalog import normalize_sections
from app.planning.catalog_service import (
    course_grade,
    expand_course_code,
    load_student_profile,
    section_verdict,
)
from app.planning.groups import get_course_group as resolve_course_group
from app.planning.mcp_bridge import sync_planning_snapshot_from_sais
from app.student import service as student_service


def _unknown_course_code(course_code: str) -> dict[str, str]:
    return {
        "status": "unknown_course_code",
        "course": course_code,
        "detail": "Use a department abbreviation with a course number (PHYS213) or the full seven-digit code.",
    }


def build_domain_reads(user_id: UUID) -> dict:
    async def search_campus_knowledge(
        query: str,
        record_types: list[str] | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search indexed campus facts with verified student-audience filters and citations."""
        async with SessionLocal() as db:
            account = await active_account(db, user_id)
            if account is None:
                return []
            context = await student_service.get_context(db, user_id)
            return await search_knowledge(
                db,
                query,
                SearchFilters(
                    record_types=tuple(record_types or []),
                    campus=context.campus,
                    department=context.department,
                    degree_level=context.degree_level,
                    starts_after=datetime.fromisoformat(starts_after) if starts_after else None,
                    starts_before=datetime.fromisoformat(starts_before) if starts_before else None,
                ),
                organization_id=account.organization_id,
                limit=max(1, min(limit, 25)),
            )

    async def read_campus_page(url: str) -> dict:
        """Read one already-approved, indexed campus page by its canonical URL."""
        async with SessionLocal() as db:
            account = await active_account(db, user_id)
            result = await read_indexed_page(db, url, organization_id=account.organization_id) if account else None
            return result or {"status": "not_indexed", "url": url}

    async def get_course_group(course: str, term: str, section: str | None = None) -> dict:
        """Return a curated course-group invite only after code-level enrollment checks."""
        await sync_planning_snapshot_from_sais(user_id, term)
        async with SessionLocal() as db:
            return await resolve_course_group(db, user_id, term=term, course_code=course, section=section)

    async def lookup_department(value: str) -> dict:
        """Resolve a METU department code, abbreviation, name or course code to one department."""
        found = department_directory.resolve(value)
        if found is None:
            return {"status": "not_found", "query": value}
        return {
            "code": found.code,
            "abbreviation": found.abbreviation,
            "name_en": found.name_en,
            "name_tr": found.name_tr,
        }

    async def get_course_sections(course_code: str, semester: str) -> dict:
        """Sections, instructors, days, rooms and credits for a course, from the cached catalog.

        Accepts either form a student uses: "PHYS213" or "2300213".
        """
        async with SessionLocal() as db:
            async with catalog_session(db, user_id) as catalog:
                try:
                    full_code, owner = await expand_course_code(db, user_id, course_code, session=catalog)
                except HTTPException as exc:
                    if exc.status_code == 404:
                        return _unknown_course_code(course_code)
                    raise
                data = await call_course_info(
                    db,
                    user_id,
                    "get_course_info",
                    {"department": owner.code, "semester": semester, "course": full_code},
                    session=catalog,
                )
            return {
                "course": full_code,
                "department": owner.code,
                "department_name": owner.name_en,
                "semester": semester,
                "data": data,
                "sections": normalize_sections(data),
            }

    async def check_section_eligibility(course_code: str, semester: str, section: str) -> dict:
        """Whether this student may register for one section, per METU's own eligibility table.

        Answers from the table SAIS publishes for the section — the departments
        it admits and their surname, CGPA and year ranges — not from anything
        the student says in chat.
        """
        async with SessionLocal() as db:
            async with catalog_session(db, user_id) as catalog:
                try:
                    full_code, owner = await expand_course_code(db, user_id, course_code, session=catalog)
                except HTTPException as exc:
                    if exc.status_code == 404:
                        return _unknown_course_code(course_code)
                    raise
                profile = await load_student_profile(db, user_id)
                payload = await call_course_info(
                    db,
                    user_id,
                    "get_section_constraints",
                    {
                        "department": owner.code,
                        "semester": semester,
                        "course": full_code,
                        "section": section,
                    },
                    session=catalog,
                )
        held = course_grade(profile, course_code, owner, full_code)
        rows, eligible, reason = section_verdict(
            payload,
            profile=profile,
            supplied_course=course_code,
            owner=owner,
            full_course=full_code,
            held_grade=held,
        )
        return {
            "course": full_code,
            "section": section,
            "student_department": profile.department.abbreviation if profile.department else None,
            "your_grade_in_this_course": held,
            "eligible": eligible,
            "reason": reason,
            "constraints": rows,
            "constraints_verified": eligible is not None,
        }

    return {
        fn.__name__: fn
        for fn in [
            search_campus_knowledge,
            read_campus_page,
            get_course_group,
            lookup_department,
            get_course_sections,
            check_section_eligibility,
        ]
    }

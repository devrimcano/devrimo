"""Broad first-party tools backed by the shared campus intelligence services."""

from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.admin.directory import active_account
from app.campus import prerequisites
from app.campus import departments as department_directory
from app.campus.course_info import call_course_info, catalog_answer_expires_at
from app.campus.eligibility import course_candidates, prior_grade, validated_constraint_rows
from app.campus.eligibility import evaluate as evaluate_eligibility
from app.db.models import StudentAcademicSnapshot, StudentContext
from app.db.session import SessionLocal
from app.knowledge.retrieval import SearchFilters, search_knowledge
from app.knowledge.retrieval import read_campus_page as read_indexed_page
from app.planning.groups import get_course_group as resolve_course_group
from app.planning.catalog import normalize_sections
from app.planning.eligibility import academic_evidence_fresh, issue_eligibility_token, planning_context_fingerprint
from app.planning.mcp_bridge import sync_planning_snapshot_from_sais
from app.student import service as student_service


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
        expanded = department_directory.expand_course_code(course_code)
        if expanded is None:
            return {
                "status": "unknown_course_code",
                "course": course_code,
                "detail": "Use a department abbreviation with a course number (PHYS213) or the full seven-digit code.",
            }
        full_code, owner = expanded
        async with SessionLocal() as db:
            return {
                "course": full_code,
                "department": owner.code,
                "department_name": owner.name_en,
                "semester": semester,
                "data": await call_course_info(
                    db,
                    user_id,
                    "get_course_info",
                    {"department": owner.code, "semester": semester, "course": full_code},
                ),
            }

    async def check_section_eligibility(course_code: str, semester: str, section: str) -> dict:
        """Whether this student may register for one section, per METU's own eligibility table.

        Answers from the table SAIS publishes for the section — the departments
        it admits and their surname, CGPA and year ranges — not from anything
        the student says in chat.
        """
        expanded = department_directory.expand_course_code(course_code)
        if expanded is None:
            return {
                "status": "unknown_course_code",
                "course": course_code,
                "detail": "Use a department abbreviation with a course number (PHYS213) or the full seven-digit code.",
            }
        full_code, owner = expanded
        compact_input = "".join(str(course_code).upper().split())
        signed_course_code = compact_input if not compact_input.isdigit() else full_code
        if full_code.isdigit() and len(full_code) == 7:
            signed_course_code = f"{owner.abbreviation}{full_code[3:].lstrip('0') or '0'}"
        async with SessionLocal() as db:
            prerequisite_expires_at = None
            try:
                constraint_values = {
                    "department": owner.code,
                    "semester": semester,
                    "course": full_code,
                    "section": section,
                }
                payload = await call_course_info(
                    db,
                    user_id,
                    "get_section_constraints",
                    constraint_values,
                )
                constraint_expires_at = await catalog_answer_expires_at(
                    db, user_id, "get_section_constraints", constraint_values
                )
                rows = validated_constraint_rows(payload)
                if rows is None:
                    return {
                        "course": full_code,
                        "section": section,
                        "student_department": None,
                        "eligible": None,
                        "eligibility_status": "unavailable",
                        "reason": "Eligibility data was not returned in a valid form",
                        "constraints": [],
                    }
                info_values = {"department": owner.code, "semester": semester, "course": full_code}
                course_payload = await call_course_info(
                    db,
                    user_id,
                    "get_course_info",
                    info_values,
                )
                info_expires_at = await catalog_answer_expires_at(db, user_id, "get_course_info", info_values)
                section_rows = {
                    str(row.get("section") or ""): row
                    for row in normalize_sections(course_payload)
                    if isinstance(row, dict)
                }
                selected_section = section_rows.get(section)
                if selected_section is None:
                    return {
                        "course": full_code,
                        "section": section,
                        "student_department": None,
                        "eligible": None,
                        "eligibility_status": "unavailable",
                        "reason": "Section meeting data is temporarily unavailable",
                        "constraints": rows,
                    }
                prerequisite_payload = await call_course_info(
                    db,
                    user_id,
                    "get_course_prerequisites",
                    {"department": owner.code, "semester": semester, "course": full_code},
                )
                prerequisite_expires_at = await catalog_answer_expires_at(
                    db,
                    user_id,
                    "get_course_prerequisites",
                    {"department": owner.code, "semester": semester, "course": full_code},
                )
                prerequisite_rows = prerequisites._rows(prerequisite_payload)
            except HTTPException as exc:
                # A missing or failed catalog read is a third state. Returning
                # ``eligible=False`` here would let an assistant treat an
                # outage as a registrar decision; returning ``True`` would
                # let it save an unverified section.
                return {
                    "course": full_code,
                    "section": section,
                    "student_department": None,
                    "eligible": None,
                    "eligibility_status": "unavailable",
                    "reason": "Eligibility data is temporarily unavailable",
                    "constraints": [],
                    "detail": str(exc.detail),
                }
            except ValueError:
                return {
                    "course": full_code,
                    "section": section,
                    "student_department": None,
                    "eligible": None,
                    "eligibility_status": "unavailable",
                    "reason": "Eligibility or prerequisite data was not returned in a valid form",
                    "constraints": [],
                }
            context = await db.get(StudentContext, user_id)
            snapshot = await db.scalar(
                select(StudentAcademicSnapshot)
                .where(StudentAcademicSnapshot.user_id == user_id)
                .order_by(StudentAcademicSnapshot.fetched_at.desc())
                .limit(1)
            )
        student = department_directory.resolve((context.department or context.program_code) if context else None)
        cgpa = None
        if snapshot and snapshot.current_credits:
            cgpa = float(snapshot.current_grade_points) / float(snapshot.current_credits)
        # A section restricted to students who still need the course is not
        # open to one who has already passed it, and the transcript is the
        # only place that says which they are.
        held = prior_grade(
            snapshot.completed_courses if snapshot else [],
            course_candidates(course_code, owner, full_code),
        )
        verdict = evaluate_eligibility(
            rows,
            department=student.abbreviation if student else None,
            surname=context.surname_prefix if context else None,
            cgpa=cgpa,
            year=context.year_of_study if context else None,
            prior_grade=held,
        )
        # ``evaluate`` is intentionally permissive for explanations: missing
        # values are not treated as a rejection. A normal timetable needs a
        # stronger server-owned verdict. Mirror the schedule endpoint's
        # tri-state contract here so assistant proposals cannot certify a
        # section from partial context or a failed transcript read.
        verified = bool(
            context
            and student
            and academic_evidence_fresh(
                context.verified_at,
                snapshot.fetched_at if snapshot else None,
            )
        )
        for row in rows:
            grade_band = " ".join(
                str(row.get(key) or "").strip()
                for key in ("start_grade", "startGrade", "end_grade", "endGrade")
            ).strip()
            if grade_band and snapshot is None:
                verified = False
            if any(
                row.get(key) not in (None, "")
                for key in ("start_char", "startChar", "end_char", "endChar")
            ) and not (context and context.surname_prefix):
                verified = False
            if any(
                row.get(key) not in (None, "")
                for key in ("min_cgpa", "minCgpa", "max_cgpa", "maxCgpa")
            ) and cgpa is None:
                verified = False
            if any(
                row.get(key) not in (None, "")
                for key in ("min_year", "minYear", "max_year", "maxYear")
            ) and not (context and context.year_of_study is not None):
                verified = False
        missing = prerequisites.unmet_prerequisites(
            prerequisite_rows,
            snapshot.completed_courses if snapshot else [],
        )
        if prerequisite_rows and snapshot is None:
            verified = False
        if not selected_section.get("meetings"):
            verified = False
        if missing:
            verdict = verdict.__class__(
                eligible=False,
                reason=f"Missing prerequisite: {prerequisites.display_code(missing[0])}",
            )
        token = (
            issue_eligibility_token(
                user_id,
                semester,
                signed_course_code,
                section,
                eligibility_status="verified",
                eligible=verdict.eligible,
                context_verified_at=context.verified_at if context else None,
                snapshot_fetched_at=snapshot.fetched_at if snapshot else None,
                meetings=selected_section.get("meetings", []),
                source_expires_at=min(
                    value for value in (info_expires_at, constraint_expires_at, prerequisite_expires_at) if value is not None
                ) if any(value is not None for value in (info_expires_at, constraint_expires_at, prerequisite_expires_at)) else None,
                context_fingerprint=planning_context_fingerprint(
                    {
                        "department": getattr(context, "department", None),
                        "program_code": getattr(context, "program_code", None),
                        "degree_level": getattr(context, "degree_level", None),
                        "year_of_study": getattr(context, "year_of_study", None),
                        "surname_prefix": getattr(context, "surname_prefix", None),
                        "campus": getattr(context, "campus", None),
                        "verified_at": getattr(context, "verified_at", None),
                        "confirmed_at": getattr(context, "confirmed_at", None),
                    }
                ),
            )
            if verified and verdict.eligible
            else None
        )
        return {
            "course": full_code,
            "section": section,
            "student_department": student.abbreviation if student else None,
            "your_grade_in_this_course": held,
            "eligible": verdict.eligible if verified else None,
            "eligibility_status": "verified" if verified else "unverified",
            "reason": verdict.reason if verified else "Student context or transcript data is not verified",
            "eligibility_token": token,
            "eligibility_course_code": signed_course_code,
            "eligibility_raw_code": full_code,
            "constraints": rows,
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

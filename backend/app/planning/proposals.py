"""Project a semester proposal into the canonical timetable command contract."""

from typing import Any

from app.planning.catalog import normalize_sections
from app.planning.models import PlanChanges, PlanCourse, PlanEntry


def _proposal_verdicts(proposal: dict) -> dict[tuple[str, str], dict[str, Any]]:
    """Read the server-produced section verdicts attached to a proposal."""

    verdicts: dict[tuple[str, str], dict[str, Any]] = {}
    raw = proposal.get("section_verdicts")
    if not isinstance(raw, list):
        return verdicts
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = "".join(str(item.get("course_code") or item.get("code") or "").upper().split())
        section = str(item.get("section") or "").strip()
        if code and section:
            verdicts[(code, section)] = item
    return verdicts


def proposal_changes(proposal: dict, *, require_verified: bool = False) -> PlanChanges | None:
    """Offer one explicit replacement only when every selected meeting is known.

    A proposal is not persisted state. Its application goes through the same
    revision, validation, idempotency and undo path as every visual planner edit.
    """
    if proposal.get("status") != "ok" or not proposal.get("courses"):
        return None
    entries = []
    verdicts = _proposal_verdicts(proposal)
    if require_verified and not verdicts:
        return None
    pool: list[PlanCourse] = []
    sections: dict[str, list[dict[str, Any]]] = {}
    try:
        for course_index, course in enumerate(proposal["courses"]):
            schedule = course.get("schedule")
            if not isinstance(schedule, list) or not schedule:
                return None
            code = "".join(str(course["course_code"]).upper().split())
            section_number = str(course["section"])
            verdict = verdicts.get((code, section_number))
            if require_verified and (
                verdict is None
                or verdict.get("eligibility_status") != "verified"
                or verdict.get("eligible") is not True
            ):
                return None
            normalized = normalize_sections([{"section": section_number, "schedule": schedule}])
            meetings = normalized[0]["meetings"] if normalized else []
            # Never silently drop a malformed or unsupported meeting on apply.
            if len(meetings) != len(schedule):
                return None
            sections.setdefault(code, []).append(
                {
                    "section": section_number,
                    "meetings": meetings,
                    "eligibility_status": verdict.get("eligibility_status") if verdict else "unverified",
                    "eligible": verdict.get("eligible") if verdict else None,
                    "reason": verdict.get("reason") if verdict else "",
                    "eligibility_token": verdict.get("eligibility_token") if verdict else None,
                    "eligibility_course_code": verdict.get("eligibility_course_code") if verdict else None,
                    "eligibility_raw_code": verdict.get("eligibility_raw_code") if verdict else None,
                }
            )
            pool.append(
                PlanCourse(
                    code=code,
                    name=str(course.get("title") or ""),
                    credits=float(course["credits"]),
                    required=True,
                )
            )
            for index, meeting in enumerate(meetings):
                entries.append(PlanEntry(
                    id=f"proposal:{course_index}:{index}",
                    code=code, name=course.get("title") or "",
                    section=section_number,
                    credits=course["credits"] if index == 0 else 0,
                    color=course_index % 8, **meeting,
                ))
        return PlanChanges(
            operation="set_entries",
            entries=entries,
            pool=pool if require_verified else None,
            sections=sections if require_verified else None,
        )
    except (KeyError, TypeError, ValueError):
        return None

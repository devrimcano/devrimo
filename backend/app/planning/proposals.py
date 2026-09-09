"""Project a semester proposal into the canonical timetable command contract."""

from app.planning.catalog import normalize_sections
from app.planning.models import PlanChanges, PlanCourse, PlanEntry


def proposal_changes(proposal: dict) -> PlanChanges | None:
    """Project a proposal into the canonical timetable command contract.

    A proposal is not persisted state. Its application goes through the same
    revision, validation, idempotency and undo path as every visual planner edit.
    An explicitly untimed course is selected for credit planning but has no
    calendar entry; mixed proposals therefore carry it in the selected pool
    alongside the timed entries.
    """
    if proposal.get("status") != "ok" or not proposal.get("courses"):
        return None
    entries = []
    selected_pool: list[PlanCourse] = []
    try:
        courses = proposal["courses"]
        if not isinstance(courses, list) or any(not isinstance(item, dict) for item in courses):
            return None
        has_untimed = any(
            str(item.get("timing_status") or "").strip().casefold()
            in {"untimed", "explicitly_untimed"}
            for item in courses
        )
        for course_index, course in enumerate(courses):
            schedule = course.get("schedule")
            timing_status = str(course.get("timing_status") or "").strip().casefold()
            if timing_status in {"untimed", "explicitly_untimed"}:
                # An untimed response must not also carry meeting data. A
                # contradictory catalog response is safer to reject than to
                # schedule under an unverifiable status.
                if not isinstance(schedule, list) or schedule:
                    return None
                selected_pool.append(
                    PlanCourse(
                        code=str(course["course_code"]),
                        name=str(course.get("title") or ""),
                        credits=course["credits"],
                        raw_code=str(course["course_code"]),
                        selected=True,
                        timing_status="untimed",
                        selected_section=str(course["section"]),
                    )
                )
                continue
            if not isinstance(schedule, list) or not schedule:
                return None
            sections = normalize_sections([{"section": course["section"], "schedule": schedule}])
            meetings = sections[0]["meetings"] if sections else []
            # Never silently drop a malformed or unsupported meeting on apply.
            if len(meetings) != len(schedule):
                return None
            if has_untimed:
                selected_pool.append(
                    PlanCourse(
                        code=str(course["course_code"]),
                        name=str(course.get("title") or ""),
                        credits=course["credits"],
                        raw_code=str(course["course_code"]),
                        selected=True,
                        selected_section=str(course["section"]),
                    )
                )
            for index, meeting in enumerate(meetings):
                entries.append(PlanEntry(
                    id=f"proposal:{course_index}:{index}",
                    code=course["course_code"], name=course.get("title") or "",
                    section=str(course["section"]),
                    credits=course["credits"] if index == 0 else 0,
                    color=course_index % 8, **meeting,
                ))
        if has_untimed:
            return PlanChanges(operation="apply_proposal", entries=entries, pool=selected_pool)
        return PlanChanges(operation="set_entries", entries=entries)
    except (KeyError, TypeError, ValueError):
        return None

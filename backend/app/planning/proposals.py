"""Project a semester proposal into the canonical timetable command contract."""

from app.planning.catalog import normalize_sections
from app.planning.models import PlanChanges, PlanEntry


def proposal_changes(proposal: dict) -> PlanChanges | None:
    """Offer one explicit replacement only when every selected meeting is known.

    A proposal is not persisted state. Its application goes through the same
    revision, validation, idempotency and undo path as every visual planner edit.
    """
    if proposal.get("status") != "ok" or not proposal.get("courses"):
        return None
    entries = []
    try:
        for course_index, course in enumerate(proposal["courses"]):
            schedule = course.get("schedule")
            if not isinstance(schedule, list) or not schedule:
                return None
            sections = normalize_sections([{"section": course["section"], "schedule": schedule}])
            meetings = sections[0]["meetings"] if sections else []
            # Never silently drop a malformed or unsupported meeting on apply.
            if len(meetings) != len(schedule):
                return None
            for index, meeting in enumerate(meetings):
                entries.append(PlanEntry(
                    id=f"proposal:{course_index}:{index}",
                    code=course["course_code"], name=course.get("title") or "",
                    section=str(course["section"]),
                    credits=course["credits"] if index == 0 else 0,
                    color=course_index % 8, **meeting,
                ))
        return PlanChanges(operation="set_entries", entries=entries)
    except (KeyError, TypeError, ValueError):
        return None

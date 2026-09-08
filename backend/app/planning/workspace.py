"""Owner service for the canonical, revisioned timetable resource.

The assistant and the schedule UI call this module.  Neither caller writes the
planning tables directly.  The database revision and idempotency constraints
are the final concurrency boundary; the service also takes a row lock so two
requests in one broker process observe the same revision ordering.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CampusCredential, StudentAcademicSnapshot, StudentContext, StudentTimetable, StudentTimetableRevision
from app.planning.models import (
    PlanChanges,
    PlanConflictError,
    PlanEntry,
    PlanEnvelope,
    PlanMeeting,
    PlanState,
    PlanValidationError,
    check_plan_state_budget,
)
from app.planning.eligibility import academic_evidence_fresh, planning_context_fingerprint, verify_eligibility_token
from app.planning.solver import SolverGroup, enumerate_solutions

MAX_ALTERNATIVES = 200
MAX_SOLVER_NODES = 150_000
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")


def _user_value(user_id: UUID | str) -> UUID | str:
    """Keep UUID objects for SQLAlchemy while accepting gateway string IDs."""

    return user_id


def _identity(value: Any) -> str:
    return "".join(str(value or "").upper().split()).replace("-", "")


def _row_revision(row: Any) -> int:
    return int(getattr(row, "revision", 0) or 0)


def _row_payload(row: Any) -> dict[str, Any]:
    payload = getattr(row, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _row_updated_at(row: Any) -> datetime | None:
    value = getattr(row, "updated_at", None)
    return value if isinstance(value, datetime) else None


async def _row(
    db: AsyncSession,
    user_id: UUID | str,
    term: str,
    *,
    for_update: bool = False,
) -> Any | None:
    query = select(StudentTimetable).where(
        StudentTimetable.user_id == _user_value(user_id),
        StudentTimetable.term == term,
    )
    if for_update:
        query = query.with_for_update()
    row = (await db.execute(query)).scalar_one_or_none()
    if row is not None:
        return row

    return None


async def _eligibility_versions(
    db: AsyncSession, user_id: UUID | str
) -> tuple[datetime | None, datetime | None, str]:
    """Read the SAIS versions bound into a normal-plan section verdict."""

    context = await db.get(StudentContext, _user_value(user_id))
    snapshot = await db.scalar(
        select(StudentAcademicSnapshot)
        .where(StudentAcademicSnapshot.user_id == _user_value(user_id))
        .order_by(StudentAcademicSnapshot.fetched_at.desc())
        .limit(1)
    )
    return (
        getattr(context, "verified_at", None),
        getattr(snapshot, "fetched_at", None),
        planning_context_fingerprint(
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


async def _require_planner_context(db: AsyncSession, user_id: UUID | str) -> None:
    """Require an authenticated account and a current SAIS identity for writes.

    Authentication establishes the account at the API boundary. This second
    guard is deliberately repeated at the owner service because assistant,
    legacy PUT and direct gateway callers all share the same revisioned write
    path. What-if is an exploration policy, not a way around account or SAIS
    verification.
    """

    credential = await db.scalar(
        select(CampusCredential).where(
            CampusCredential.user_id == _user_value(user_id),
            CampusCredential.verified_at.is_not(None),
            CampusCredential.metu_password_enc.is_not(None),
        )
    )
    context = await db.get(StudentContext, _user_value(user_id))
    if (
        credential is None
        or context is None
        or context.verified_at is None
        or not academic_evidence_fresh(context.verified_at)
    ):
        raise PlanValidationError(
            "A current verified METU account and SAIS academic context are required before saving a plan"
        )


async def _resource_lock(db: AsyncSession, user_id: UUID | str, term: str) -> None:
    """Serialize first writes as well as updates to an existing row.

    ``student_timetables`` is keyed by ``(user_id, term)``. A row lock alone
    cannot protect the create path because there is no row to lock yet, so two
    first writes could both observe revision zero and race on the unique
    history/current-row constraints. PostgreSQL transaction advisory locks
    give the resource one lock key before either read, including that path.
    """

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"planning:timetable:{user_id}:{term}"},
    )


def _default_state() -> PlanState:
    return PlanState()


def _state(row: Any | None) -> PlanState:
    return PlanState.from_legacy_payload(_row_payload(row)) if row is not None else _default_state()


async def _history_for_key(
    db: AsyncSession, user_id: UUID | str, term: str, idempotency_key: str
) -> Any | None:
    return (
        await db.execute(
            select(StudentTimetableRevision).where(
                StudentTimetableRevision.user_id == _user_value(user_id),
                StudentTimetableRevision.term == term,
                StudentTimetableRevision.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


async def _history_revision(
    db: AsyncSession, user_id: UUID | str, term: str, revision: int
) -> Any | None:
    return (
        await db.execute(
            select(StudentTimetableRevision).where(
                StudentTimetableRevision.user_id == _user_value(user_id),
                StudentTimetableRevision.term == term,
                StudentTimetableRevision.revision == revision,
            )
        )
    ).scalar_one_or_none()


async def _has_undo(db: AsyncSession, user_id: UUID | str, term: str, revision: int) -> bool:
    if revision <= 0:
        return False
    previous = await _history_revision(db, user_id, term, revision - 1)
    return previous is not None


async def _envelope(
    db: AsyncSession,
    user_id: UUID | str,
    term: str,
    row: Any | None,
    *,
    operation: str | None = None,
    idempotency_key: str | None = None,
    revision_override: int | None = None,
    payload_override: dict[str, Any] | None = None,
    updated_at_override: datetime | None = None,
) -> PlanEnvelope:
    revision = _row_revision(row) if revision_override is None else revision_override
    payload = _row_payload(row) if payload_override is None else payload_override
    return PlanEnvelope(
        user_id=str(user_id),
        term=term,
        revision=revision,
        updated_at=updated_at_override if updated_at_override is not None else _row_updated_at(row),
        state=PlanState.from_legacy_payload(payload),
        can_undo=await _has_undo(db, user_id, term, revision),
        previous_revision=revision - 1 if revision > 0 else None,
        operation=operation,
        idempotency_key=idempotency_key,
    )


async def read_timetable(db: AsyncSession, user_id: UUID | str, term: str) -> PlanEnvelope:
    """Read only the authenticated account's plan for ``term``."""

    row = await _row(db, user_id, term)
    return await _envelope(db, user_id, term, row)


def _entry_meeting(entry: PlanEntry) -> tuple[str, int, int]:
    return entry.day, entry.start_minute, entry.start_minute + entry.duration_minutes


def _overlap(left: PlanEntry, right: PlanEntry) -> bool:
    day, start, end = _entry_meeting(left)
    other_day, other_start, other_end = _entry_meeting(right)
    return day == other_day and start < other_end and other_start < end


def _validate_state(
    state: PlanState,
    *,
    user_id: UUID | str | None = None,
    term: str | None = None,
    context_verified_at: datetime | None = None,
    snapshot_fetched_at: datetime | None = None,
    context_fingerprint: str | None = None,
) -> None:
    # ``model_copy(update=...)`` deliberately avoids re-running Pydantic
    # validators. Re-check nested request fields at the final owner boundary
    # before any revision or what-if backup can be serialized.
    try:
        check_plan_state_budget(state)
    except ValueError as exc:
        # Budget failures can originate from ``model_copy(update=...)``, which
        # intentionally skips Pydantic validators. Keep malformed client state
        # on the normal 422 owner-service path instead of leaking a 500.
        raise PlanValidationError(str(exc)) from exc
    seen: set[str] = set()
    for entry in state.entries:
        if entry.id in seen:
            raise PlanValidationError(f"duplicate timetable entry id: {entry.id}")
        seen.add(entry.id)
        if entry.day in state.empty_days:
            raise PlanValidationError(f"entry falls on a selected empty day: {entry.day}")
    if state.avoid_conflicts:
        for index, entry in enumerate(state.entries):
            if any(_overlap(entry, other) for other in state.entries[index + 1 :]):
                raise PlanValidationError("timetable entries overlap while conflict avoidance is enabled")
    for alternative in state.alternatives:
        for entry in alternative:
            if entry.day in state.empty_days:
                raise PlanValidationError(f"alternative entry falls on a selected empty day: {entry.day}")

    # ``ignore_constraints`` is a legacy compatibility field, not a second
    # escape hatch. Constraint responses are server generated by /constraints.
    # A normal plan needs a positive, verified verdict; an absent row or a
    # ``None`` verdict is unknown and cannot be smuggled in through a browser
    # or agent write. Explicit what-if plans are the only place exploration is
    # allowed.
    if state.ignore_constraints and not state.what_if:
        raise PlanValidationError("ignore_constraints requires explicit what-if mode")
    if not state.what_if:
        for entry in state.entries:
            if entry.kind != "course":
                continue
            if not entry.section:
                raise PlanValidationError("normal timetable courses must include a server-verified section")
            course = next(
                (
                    candidate
                    for candidate in state.pool
                    if _identity(candidate.code) == _identity(entry.code)
                    or (candidate.raw_code and _identity(candidate.raw_code) == _identity(entry.code))
                ),
                None,
            )
            section_keys = {_identity(entry.code)}
            if course is not None:
                section_keys.add(_identity(course.code))
                if course.raw_code:
                    section_keys.add(_identity(course.raw_code))
            course_rows: list[dict[str, Any]] = []
            for key in section_keys:
                raw_rows = state.sections.get(key, [])
                if isinstance(raw_rows, dict):
                    raw_rows = list(raw_rows.values())
                if isinstance(raw_rows, list):
                    course_rows.extend(row for row in raw_rows if isinstance(row, dict))
            if isinstance(course_rows, dict):
                course_rows = list(course_rows.values())
            matching = [
                row for row in course_rows
                if isinstance(row, dict) and str(row.get("section", "")) == entry.section
            ]
            if course is None:
                raise PlanValidationError(
                    f"course {entry.code} is not in the selected catalog pool; add it in what-if mode or remove it"
                )
            if not matching:
                raise PlanValidationError(
                    f"section {entry.section} has no server eligibility verdict; switch to what-if to explore it"
                )
            row = matching[0]
            if row.get("eligibility_status") != "verified" or row.get("eligible") is not True:
                reason = str(row.get("reason") or "section eligibility is unverified").strip()
                raise PlanValidationError(f"section {entry.section} is not verified for this student: {reason}")
            signed_course_code = str(row.get("eligibility_course_code") or "").strip()
            if not signed_course_code or _identity(entry.code) != _identity(signed_course_code):
                raise PlanValidationError(
                    f"section {entry.section} eligibility evidence is bound to a different course"
                )
            evidence_raw_code = str(row.get("eligibility_raw_code") or "").strip()
            if evidence_raw_code and course.raw_code and _identity(course.raw_code) != _identity(evidence_raw_code):
                raise PlanValidationError(
                    f"section {entry.section} eligibility evidence is bound to a different catalog identity"
                )
            published_meetings = {
                meeting
                for raw_meeting in (row.get("meetings") or [])
                if (meeting := _meeting_from_value(raw_meeting)) is not None
            }
            if (entry.day, entry.start_minute, entry.start_minute + entry.duration_minutes, entry.room) not in published_meetings:
                raise PlanValidationError(
                    f"section {entry.section} meeting data does not match the current catalog; refresh the section"
                )
            if user_id is not None and term is not None:
                if not verify_eligibility_token(
                    row.get("eligibility_token"),
                    user_id,
                    term,
                    signed_course_code,
                    entry.section,
                    eligibility_status="verified",
                    eligible=True,
                    context_verified_at=context_verified_at,
                    snapshot_fetched_at=snapshot_fetched_at,
                    meetings=row.get("meetings"),
                    context_fingerprint=context_fingerprint,
                ):
                    raise PlanValidationError(
                        f"section {entry.section} eligibility evidence is missing or expired; refresh the section"
                    )


def _set_entries(state: PlanState, entries: Iterable[PlanEntry]) -> PlanState:
    return state.model_copy(update={"entries": list(entries)})


def _set_pool(state: PlanState, pool: list[Any], sections: dict[str, list[dict[str, Any]]] | None) -> PlanState:
    """Replace the selected pool while removing state tied to deleted courses."""

    next_pool = list(pool)
    next_codes = {
        code
        for course in next_pool
        for code in (_identity(course.code), _identity(course.raw_code))
        if code
    }
    # A newly empty pool is an explicit clear action. When a pool exists, keep
    # only entries for courses that remain selected; personal blocks survive.
    entries = [
        entry
        for entry in state.entries
        if entry.kind == "block" or _identity(entry.code) in next_codes
    ]
    locked = {
        key: value
        for key, value in state.locked_sections.items()
        if _identity(key) in next_codes
    }
    allowed_section_keys = next_codes
    next_sections = {
        str(key): value
        for key, value in (sections if sections is not None else state.sections).items()
        if _identity(key) in allowed_section_keys
    }
    return state.model_copy(
        update={
            "pool": next_pool,
            "sections": next_sections,
            "entries": entries,
            "alternatives": [],
            "alternative_index": 0,
            "locked_sections": locked,
            "unscheduled_courses": [],
            "generation_error": "",
        }
    )


def _projection_state(projection: dict[str, Any]) -> PlanState:
    return PlanState.from_legacy_payload(projection)


def _switch_mode(current: PlanState, requested: PlanState) -> PlanState:
    """Keep what-if exploration in a persisted branch of the normal draft."""
    if requested.what_if and not current.what_if:
        backup = current.model_dump(mode="python", exclude={"what_if", "what_if_backup", "ignore_constraints"})
        return requested.model_copy(update={
            "what_if": True,
            "ignore_constraints": True,
            "what_if_backup": backup,
        })
    if requested.what_if and current.what_if:
        # Browser replacements do not carry the private backup field. Keep the
        # server copy while the exploratory branch changes.
        return requested.model_copy(update={
            "what_if": True,
            "ignore_constraints": True,
            "what_if_backup": current.what_if_backup,
        })
    if not requested.what_if and current.what_if and current.what_if_backup:
        restored = PlanState.model_validate({
            **current.what_if_backup,
            "what_if": False,
            "ignore_constraints": False,
            "what_if_backup": None,
        })
        return restored
    return requested.model_copy(update={"what_if": False, "ignore_constraints": False, "what_if_backup": None})


def _apply_changes(state: PlanState, changes: PlanChanges) -> PlanState:
    operation = changes.operation
    if operation in {"replace", "import_legacy"}:
        if changes.state is None:
            raise PlanValidationError("replace requires state")
        requested = PlanState.model_validate(changes.state.model_dump(mode="python"))
        return _switch_mode(state, requested) if requested.what_if != state.what_if else requested.model_copy(update={"what_if_backup": state.what_if_backup})
    if operation == "replace_projection":
        if changes.projection is None:
            raise PlanValidationError("replace_projection requires projection")
        imported = _projection_state(changes.projection)
        # Legacy projection writes only courses and blocks. Preserve the rest
        # of the already canonical planner state for old PUT callers.
        return state.model_copy(update={"entries": imported.entries})
    if operation == "set_entries":
        update: dict[str, Any] = {"entries": changes.entries or []}
        # Assistant proposals carry the same section verdicts as the visual
        # planner. Apply them atomically with the entries so the normal write
        # validator can enforce the server's tri-state eligibility result.
        # Older hand-entered blocks omit these optional fields and retain the
        # one-release compatibility path for user-created entries.
        if changes.pool is not None:
            update["pool"] = changes.pool
        if changes.sections is not None:
            update["sections"] = changes.sections
        return state.model_copy(update=update)
    if operation == "add_entry":
        if changes.entry is None:
            raise PlanValidationError("add_entry requires entry")
        return _set_entries(state, [*state.entries, changes.entry])
    if operation == "remove_entry":
        if not changes.entry_id:
            raise PlanValidationError("remove_entry requires entry_id")
        return _set_entries(state, [entry for entry in state.entries if entry.id != changes.entry_id])
    if operation == "set_pool":
        return _set_pool(state, changes.pool or [], changes.sections)
    if operation == "set_options":
        update: dict[str, Any] = {}
        if changes.empty_days is not None:
            update["empty_days"] = changes.empty_days
        if changes.avoid_conflicts is not None:
            update["avoid_conflicts"] = changes.avoid_conflicts
        if changes.ignore_constraints is not None:
            update["ignore_constraints"] = changes.ignore_constraints
        if changes.what_if is not None:
            update["what_if"] = changes.what_if
            # Keep the legacy flag in sync for one release so old assistant
            # clients cannot accidentally turn a normal plan into a bypass.
            update["ignore_constraints"] = changes.what_if
        requested = state.model_copy(update=update)
        return _switch_mode(state, requested) if requested.what_if != state.what_if else requested
    if operation == "set_mode":
        if changes.what_if is None:
            raise PlanValidationError("set_mode requires what_if")
        requested = state.model_copy(update={"what_if": changes.what_if, "ignore_constraints": changes.what_if})
        return _switch_mode(state, requested)
    if operation == "set_locks":
        return state.model_copy(update={"locked_sections": changes.locked_sections or {}})
    if operation == "regenerate":
        if changes.pool is None or changes.sections is None:
            raise PlanValidationError("regenerate requires the selected pool and section evidence")
        requested = _set_pool(state, changes.pool, changes.sections)
        # Keep the last successful calendar as the fallback if this solve has
        # no complete result. The attempted entries are inputs to the solver,
        # not a successful revision that should replace what the student was
        # looking at while an unavailable/closed course is explained.
        preserved_entries = list(requested.entries)
        if changes.entries is not None:
            # A block drag/resize is submitted together with the same solve as
            # a course-pool edit. Course rows are accepted only for courses
            # still in the requested pool; removed courses cannot return in a
            # late browser payload.
            selected_codes = {
                code
                for course in requested.pool
                for code in (_identity(course.code), _identity(course.raw_code))
                if code
            }
            requested = requested.model_copy(update={
                "entries": [
                    entry for entry in changes.entries
                    if entry.kind == "block" or _identity(entry.code) in selected_codes
                ]
            })
        if changes.empty_days is not None:
            requested = requested.model_copy(update={"empty_days": changes.empty_days})
        if changes.avoid_conflicts is not None:
            requested = requested.model_copy(update={"avoid_conflicts": changes.avoid_conflicts})
        if changes.what_if is not None:
            mode_state = requested.model_copy(
                update={"what_if": changes.what_if, "ignore_constraints": changes.what_if}
            )
            requested = _switch_mode(state, mode_state) if changes.what_if != state.what_if else mode_state.model_copy(
                update={"what_if_backup": state.what_if_backup}
            )
            # Leaving what-if restores the private normal branch. That branch
            # is the successful calendar to preserve if the strict solve then
            # fails; never replace it with the exploratory entries.
            if changes.what_if is False and state.what_if:
                preserved_entries = list(requested.entries)
        # Leaving what-if restores the complete normal branch, including its
        # section locks. Do not let exploratory lock state overwrite it.
        if changes.locked_sections is not None and not (changes.what_if is False and state.what_if):
            requested = requested.model_copy(update={"locked_sections": changes.locked_sections})
        # Solving inside this command means the mode, pool, locks and entries
        # become one revision. A failed solve leaves ``requested.entries``
        # (the last successful calendar) in place with explicit diagnostics.
        solved = solve_plan_state(requested)
        if solved.generation_error:
            return requested.model_copy(
                update={
                    "entries": preserved_entries,
                    "alternatives": [],
                    "alternative_index": 0,
                    "unscheduled_courses": solved.unscheduled_courses,
                    "generation_error": solved.generation_error,
                }
            )
        return solved
    if operation == "set_alternatives":
        return state.model_copy(
            update={
                "alternatives": changes.alternatives or [],
                "alternative_index": changes.alternative_index or 0,
            }
        )
    if operation == "select_alternative":
        if not state.alternatives:
            raise PlanValidationError("there are no alternatives to select")
        index = changes.alternative_index if changes.alternative_index is not None else 0
        if index >= len(state.alternatives):
            raise PlanValidationError("alternative index is out of range")
        return state.model_copy(update={"entries": state.alternatives[index], "alternative_index": index})
    if operation == "favorite":
        if not state.entries:
            return state
        favorites = [*state.favorites, list(state.entries)][-10:]
        return state.model_copy(update={"favorites": favorites, "favorite_index": len(favorites) - 1})
    if operation == "next_favorite":
        if not state.favorites:
            return state
        index = (state.favorite_index + 1) % len(state.favorites)
        return state.model_copy(update={"entries": state.favorites[index], "favorite_index": index})
    if operation == "clear_pool":
        return state.model_copy(
            update={
                "pool": [],
                "sections": {},
                "entries": [entry for entry in state.entries if entry.kind == "block"],
                "alternatives": [],
                "alternative_index": 0,
                "locked_sections": {},
                "unscheduled_courses": [],
                "generation_error": "",
            }
        )
    if operation == "solve":
        return solve_plan_state(state)
    raise PlanValidationError(f"unsupported timetable operation: {operation}")


def _meeting_from_value(value: Any) -> tuple[str, int, int, str] | None:
    if not isinstance(value, dict):
        return None
    try:
        meeting = PlanMeeting.model_validate(value)
    except Exception:
        return None
    return meeting.day, meeting.start_minute, meeting.start_minute + meeting.duration_minutes, meeting.room


def _section_values(state: PlanState, course: Any) -> list[dict[str, Any]]:
    keys = {_identity(course.code)}
    if course.raw_code:
        keys.add(_identity(course.raw_code))
    values: list[dict[str, Any]] = []
    for key in keys:
        raw = state.sections.get(key, [])
        if isinstance(raw, dict):
            raw = list(raw.values())
        if isinstance(raw, list):
            values.extend(item for item in raw if isinstance(item, dict))
    return values


def _section_meetings(section: dict[str, Any]) -> list[tuple[str, int, int, str]]:
    raw = section.get("meetings") or section.get("schedule") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    return [meeting for item in raw if (meeting := _meeting_from_value(item)) is not None]


def _entry_for_choice(course: Any, section: dict[str, Any], course_index: int) -> list[PlanEntry]:
    meetings = _section_meetings(section)
    output: list[PlanEntry] = []
    for meeting_index, (day, start, end, room) in enumerate(meetings):
        output.append(
            PlanEntry(
                id=str(uuid4()),
                code=course.code,
                name=course.name,
                section=str(section.get("section") or section.get("section_number") or ""),
                credits=course.credits if meeting_index == 0 else 0,
                color=course_index,
                kind="course",
                instructor=str(section.get("instructor") or ""),
                day=day,
                start_minute=start,
                duration_minutes=end - start,
                room=room,
            )
        )
    return output


def _shape(entries: list[PlanEntry]) -> tuple[int, int, tuple[str, ...]]:
    used = {entry.day for entry in entries}
    gaps = 0
    for day in used:
        rows = [entry for entry in entries if entry.day == day]
        first = min(entry.start_minute for entry in rows)
        last = max(entry.start_minute + entry.duration_minutes for entry in rows)
        taught = sum(entry.duration_minutes for entry in rows)
        gaps += last - first - taught
    return len(used), gaps, tuple(day for day in _DAYS if day not in used)


def solve_plan_state(state: PlanState) -> PlanState:
    """Build deterministic alternatives from the canonical pool and sections.

    The implementation deliberately runs on minute values.  A UI may draw an
    hour grid, but it cannot change which section fits or silently round a
    conflict away.
    """

    groups: list[SolverGroup[dict[str, Any]]] = []
    courses_by_key: dict[str, Any] = {}
    unscheduled: list[str] = []
    pool_keys = {
        code
        for course in state.pool
        for code in (_identity(course.code), _identity(course.raw_code))
        if code
    }
    preferred_sections: dict[str, str] = {}
    for entry in state.entries:
        if entry.kind == "course" and _identity(entry.code) in pool_keys and entry.section:
            preferred_sections.setdefault(_identity(entry.code), entry.section)
    fixed_entries = [
        entry for entry in state.entries
        if entry.kind == "block" or _identity(entry.code) not in pool_keys
    ]
    fixed_meetings = [
        (entry.day, entry.start_minute, entry.start_minute + entry.duration_minutes)
        for entry in fixed_entries
    ]
    for course in state.pool:
        options = []
        for section in _section_values(state, course):
            status = section.get("eligibility_status")
            if not state.what_if and (
                (status is not None and str(status).lower() != "verified")
                or (status is not None and section.get("eligible") is not True)
                or (status is None and section.get("eligible") is not True)
            ):
                continue
            meetings = _section_meetings(section)
            if not meetings or any(day in state.empty_days for day, _, _, _ in meetings):
                continue
            locked = state.locked_sections.get(_identity(course.code)) or state.locked_sections.get(course.code)
            if locked and str(section.get("section") or section.get("section_number") or "") != str(locked):
                continue
            options.append(section)
        if not options:
            unscheduled.append(course.code)
        else:
            preferred = preferred_sections.get(_identity(course.code)) or preferred_sections.get(_identity(course.raw_code))
            if preferred:
                options.sort(
                    key=lambda section: 0
                    if str(section.get("section") or section.get("section_number") or "") == preferred
                    else 1
                )
        # Keep empty required groups in the solver. Omitting them lets the
        # bounded search silently return a partial timetable.
        groups.append(SolverGroup(key=course.code, options=tuple(options)))
        courses_by_key[course.code] = course
    solutions = enumerate_solutions(
        groups,
        lambda section: [(day, start, end) for day, start, end, _ in _section_meetings(section)],
        avoid_conflicts=state.avoid_conflicts,
        max_solutions=MAX_ALTERNATIVES,
        max_nodes=MAX_SOLVER_NODES,
        allow_skip=lambda group: not courses_by_key[group.key].required,
        initial_meetings=fixed_meetings,
    )
    if not solutions:
        missing = list(dict.fromkeys(unscheduled or [course.code for course in state.pool]))
        return state.model_copy(
            update={
                "alternatives": [],
                "alternative_index": 0,
                "unscheduled_courses": missing,
                "generation_error": "No complete schedule satisfies the selected courses and constraints.",
            }
        )
    alternatives: list[list[PlanEntry]] = []
    for solution in solutions:
        entries: list[PlanEntry] = list(fixed_entries)
        for course_index, choice in enumerate(solution):
            entries.extend(_entry_for_choice(courses_by_key[choice.key], choice.option, course_index))
        alternatives.append(entries)
    alternatives.sort(
        key=lambda entries: (
            -sum(
                1
                for entry in entries
                if entry.kind == "course" and preferred_sections.get(_identity(entry.code)) == entry.section
            ),
            -len({entry.code for entry in entries if entry.kind == "course"}),
            _shape(entries),
        )
    )
    scheduled = {_identity(entry.code) for entry in alternatives[0] if entry.kind == "course"}
    unscheduled = list(dict.fromkeys([course.code for course in state.pool if _identity(course.code) not in scheduled]))
    return state.model_copy(
        update={
            "entries": alternatives[0] if alternatives else [],
            "alternatives": alternatives,
            "alternative_index": 0,
            "unscheduled_courses": unscheduled,
            "generation_error": "" if not unscheduled else "Some selected courses could not be placed.",
        }
    )


async def _commit(
    db: AsyncSession,
    user_id: UUID | str,
    term: str,
    state: PlanState,
    *,
    current: Any | None,
    expected_revision: int,
    idempotency_key: str,
    operation: str,
) -> PlanEnvelope:
    next_revision = expected_revision + 1
    payload = state.to_payload()
    # A pre-canonical row is revision zero, but its payload is a real student
    # plan. Preserve it before the first write so undo returns that plan rather
    # than an empty object. A brand new resource gets the same empty baseline,
    # which makes its first explicit edit undoable as well.
    if expected_revision == 0 and await _history_revision(db, user_id, term, 0) is None:
        baseline = _row_payload(current) if current is not None else PlanState().to_payload()
        db.add(
            StudentTimetableRevision(
                user_id=_user_value(user_id),
                term=term,
                revision=0,
                payload=baseline,
                operation="seed",
                idempotency_key=f"__seed__:{term}",
            )
        )
    if current is None:
        kwargs: dict[str, Any] = {"user_id": _user_value(user_id), "term": term, "payload": payload}
        kwargs["revision"] = next_revision
        current = StudentTimetable(**kwargs)
        db.add(current)
    else:
        current.payload = payload
        current.term = term
        current.revision = next_revision

    db.add(
        StudentTimetableRevision(
            user_id=_user_value(user_id),
            term=term,
            revision=next_revision,
            payload=payload,
            operation=operation,
            idempotency_key=idempotency_key,
        )
    )
    await db.flush()
    await db.commit()
    await db.refresh(current)
    return await _envelope(
        db,
        user_id,
        term,
        current,
        operation=operation,
        idempotency_key=idempotency_key,
    )


async def update_timetable(
    db: AsyncSession,
    user_id: UUID | str,
    term: str,
    changes: PlanChanges,
    expected_revision: int,
    idempotency_key: str,
) -> PlanEnvelope:
    """Apply one explicit owner request with optimistic concurrency."""

    if expected_revision < 0:
        raise PlanValidationError("expected_revision must be non-negative")
    if not idempotency_key or len(idempotency_key) > 128:
        raise PlanValidationError("idempotency_key is required and must be at most 128 characters")

    await _require_planner_context(db, user_id)

    # Lock before the idempotency lookup so concurrent first writes are
    # serialized too. The transaction lock is released by commit/rollback.
    await _resource_lock(db, user_id, term)
    previous = await _history_for_key(db, user_id, term, idempotency_key)
    if previous is not None:
        return await _envelope(
            db,
            user_id,
            term,
            None,
            revision_override=int(previous.revision),
            payload_override=previous.payload,
            operation=str(previous.operation),
            idempotency_key=idempotency_key,
            updated_at_override=getattr(previous, "created_at", None),
        )

    current = await _row(db, user_id, term, for_update=True)
    current_revision = _row_revision(current)
    if current_revision != expected_revision:
        raise PlanConflictError(await _envelope(db, user_id, term, current))
    current_state = _state(current)
    next_state = _apply_changes(current_state, changes)
    context_verified_at, snapshot_fetched_at, context_fingerprint = await _eligibility_versions(db, user_id)
    _validate_state(
        next_state,
        user_id=user_id,
        term=term,
        context_verified_at=context_verified_at,
        snapshot_fetched_at=snapshot_fetched_at,
        context_fingerprint=context_fingerprint,
    )
    return await _commit(
        db,
        user_id,
        term,
        next_state,
        current=current,
        expected_revision=current_revision,
        idempotency_key=idempotency_key,
        operation=changes.operation,
    )


async def undo_timetable(
    db: AsyncSession,
    user_id: UUID | str,
    term: str,
    expected_revision: int,
    idempotency_key: str,
) -> PlanEnvelope:
    """Restore the preceding snapshot as a new revision."""

    if not idempotency_key or len(idempotency_key) > 128:
        raise PlanValidationError("idempotency_key is required and must be at most 128 characters")
    await _require_planner_context(db, user_id)
    await _resource_lock(db, user_id, term)
    previous = await _history_for_key(db, user_id, term, idempotency_key)
    if previous is not None:
        return await _envelope(
            db,
            user_id,
            term,
            None,
            revision_override=int(previous.revision),
            payload_override=previous.payload,
            operation=str(previous.operation),
            idempotency_key=idempotency_key,
            updated_at_override=getattr(previous, "created_at", None),
        )
    current = await _row(db, user_id, term, for_update=True)
    current_revision = _row_revision(current)
    if current_revision != expected_revision:
        raise PlanConflictError(await _envelope(db, user_id, term, current))
    if current_revision <= 0:
        raise PlanValidationError("there is no earlier timetable revision")
    prior = await _history_revision(db, user_id, term, current_revision - 1)
    if prior is None:
        raise PlanValidationError("the earlier timetable revision is unavailable")
    state = PlanState.from_legacy_payload(prior.payload)
    context_verified_at, snapshot_fetched_at, context_fingerprint = await _eligibility_versions(db, user_id)
    _validate_state(
        state,
        user_id=user_id,
        term=term,
        context_verified_at=context_verified_at,
        snapshot_fetched_at=snapshot_fetched_at,
        context_fingerprint=context_fingerprint,
    )
    return await _commit(
        db,
        user_id,
        term,
        state,
        current=current,
        expected_revision=current_revision,
        idempotency_key=idempotency_key,
        operation="undo",
    )


def projection_from_state(state: PlanState) -> dict[str, Any]:
    """Build the small timetable projection consumed by the chat context."""

    courses: dict[tuple[str, str], dict[str, Any]] = {}
    blocks: dict[str, dict[str, Any]] = {}
    for entry in state.entries:
        meeting = {
            "day": entry.day,
            "start": entry.start_minute // 60,
            "duration": max(1, (entry.duration_minutes + 59) // 60),
            "start_minute": entry.start_minute,
            "duration_minutes": entry.duration_minutes,
            "room": entry.room,
        }
        if entry.kind == "block":
            block = blocks.setdefault(entry.name or entry.id, {"name": entry.name, "meetings": []})
            block["meetings"].append(meeting)
            continue
        key = (entry.code, entry.section)
        course = courses.setdefault(
            key,
            {
                "code": entry.code,
                "name": entry.name,
                "section": entry.section,
                "credits": 0,
                "instructor": entry.instructor,
                "meetings": [],
            },
        )
        course["credits"] += entry.credits
        course["meetings"].append(meeting)
    return {"courses": list(courses.values()), "busy_blocks": list(blocks.values())}

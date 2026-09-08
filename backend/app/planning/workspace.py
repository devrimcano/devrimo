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

from app.core.digest import stable_digest
from app.db.models import StudentTimetable, StudentTimetableRevision
from app.planning.models import (
    PlanChanges,
    PlanConflictError,
    PlanEntry,
    PlanEnvelope,
    PlanIdempotencyError,
    PlanMeeting,
    PlanState,
    PlanValidationError,
)
from app.planning.solver import SolverGroup, enumerate_solutions

MAX_ALTERNATIVES = 200
MAX_SOLVER_NODES = 150_000
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
# History written before 0032 has no way to recover the request body from the
# resulting state snapshot. The migration marks those rows with this value so
# a retry fails closed and cannot accidentally replay an unknown request.
LEGACY_REQUEST_DIGEST = "0" * 64


def _user_value(user_id: UUID | str) -> UUID | str:
    """Keep UUID objects for SQLAlchemy while accepting gateway string IDs."""

    return user_id


def _identity(value: Any) -> str:
    return "".join(str(value or "").upper().split()).replace("-", "")


def _request_digest(changes: PlanChanges, expected_revision: int) -> str:
    """Hash the complete timetable update request under the shared JSON rules."""

    return stable_digest(
        {
            "changes": changes.model_dump(mode="json", exclude_none=True),
            "expected_revision": expected_revision,
        }
    )


def _undo_request_digest(expected_revision: int) -> str:
    return stable_digest({"operation": "undo", "expected_revision": expected_revision})


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


def _validate_state(state: PlanState) -> None:
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

    # Constraint responses are server generated by /constraints.  If one is
    # already present in the canonical state, respect it on writes too, even
    # when the request came from the agent rather than this browser.
    if not state.ignore_constraints:
        for entry in state.entries:
            if entry.kind != "course" or not entry.section:
                continue
            course_rows = state.sections.get(_identity(entry.code), [])
            if isinstance(course_rows, dict):
                course_rows = list(course_rows.values())
            for row in course_rows:
                if not isinstance(row, dict) or str(row.get("section", "")) != entry.section:
                    continue
                if row.get("eligible") is False:
                    reason = str(row.get("reason") or "section restrictions").strip()
                    raise PlanValidationError(f"section {entry.section} is not eligible: {reason}")


def _set_entries(state: PlanState, entries: Iterable[PlanEntry]) -> PlanState:
    return state.model_copy(update={"entries": list(entries)})


def _projection_state(projection: dict[str, Any]) -> PlanState:
    return PlanState.from_legacy_payload(projection)


def _apply_changes(state: PlanState, changes: PlanChanges) -> PlanState:
    operation = changes.operation
    if operation == "replace":
        if changes.state is None:
            raise PlanValidationError("replace requires state")
        return PlanState.model_validate(changes.state.model_dump(mode="python"))
    if operation == "import_legacy":
        if changes.projection is not None:
            return _projection_state(changes.projection)
        # One older browser release sent the already converted state. Keep
        # accepting that shape while the browser moves to the server-owned
        # compatibility converter; raw legacy payloads always take precedence.
        if changes.state is not None:
            return PlanState.model_validate(changes.state.model_dump(mode="python"))
        raise PlanValidationError("import_legacy requires projection")
    if operation == "replace_projection":
        if changes.projection is None:
            raise PlanValidationError("replace_projection requires projection")
        imported = _projection_state(changes.projection)
        # Legacy projection writes only courses and blocks. Preserve the rest
        # of the already canonical planner state for old PUT callers.
        return state.model_copy(update={"entries": imported.entries})
    if operation == "set_entries":
        return _set_entries(state, changes.entries or [])
    if operation == "add_entry":
        if changes.entry is None:
            raise PlanValidationError("add_entry requires entry")
        return _set_entries(state, [*state.entries, changes.entry])
    if operation == "remove_entry":
        if not changes.entry_id:
            raise PlanValidationError("remove_entry requires entry_id")
        return _set_entries(state, [entry for entry in state.entries if entry.id != changes.entry_id])
    if operation == "set_pool":
        return state.model_copy(
            update={
                "pool": changes.pool or [],
                "sections": changes.sections if changes.sections is not None else state.sections,
            }
        )
    if operation == "set_options":
        update: dict[str, Any] = {}
        if changes.empty_days is not None:
            update["empty_days"] = changes.empty_days
        if changes.avoid_conflicts is not None:
            update["avoid_conflicts"] = changes.avoid_conflicts
        if changes.ignore_constraints is not None:
            update["ignore_constraints"] = changes.ignore_constraints
        return state.model_copy(update=update)
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
        return state.model_copy(update={"pool": [], "sections": {}, "alternatives": [], "alternative_index": 0})
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
    """This course's sections, read from the one key that describes it.

    A stored plan can hold the same course's sections twice: under the
    seven-digit course code and under the letter form. Only the seven-digit key
    carries eligibility, because that is how the verdicts are keyed; the letter
    entries are what an older client wrote and every round-trip preserves them.
    Reading both and concatenating therefore handed the solver a second,
    unjudged copy of every section, and an ineligible one was placed because
    its duplicate said nothing about it — the restriction check looked switched
    off while the same section was flagged red in the pool.

    So: one key wins, the seven-digit one when it has rows. Within it a section
    number appears once, and a decided verdict beats an undecided one.
    """
    keys = []
    if course.raw_code:
        keys.append(_identity(course.raw_code))
    keys.append(_identity(course.code))

    for key in keys:
        raw = state.sections.get(key, [])
        if isinstance(raw, dict):
            raw = list(raw.values())
        if not isinstance(raw, list):
            continue
        chosen: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            number = str(item.get("section") or item.get("section_number") or "").strip()
            # An unnumbered row cannot be compared with another, so it is kept
            # on its own rather than collapsing every one of them into one.
            identity = number or f"#{len(chosen)}"
            held = chosen.get(identity)
            if held is None or (held.get("eligible") is None and item.get("eligible") is not None):
                chosen[identity] = item
        if chosen:
            return list(chosen.values())
    return []


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
    for course in state.pool:
        timed_sections = [
            section
            for section in _section_values(state, course)
            if _section_meetings(section)
        ]
        # Courses whose meeting times have not been published cannot take part
        # in the timetable yet. Once a course has a timed section, however, it
        # is required: restrictions and empty-day preferences may reject the
        # whole solve, but must never turn it into a partial timetable.
        if not timed_sections:
            continue
        options = []
        for section in timed_sections:
            if section.get("eligible") is False and not state.ignore_constraints:
                continue
            meetings = _section_meetings(section)
            if any(day in state.empty_days for day, _, _, _ in meetings):
                continue
            options.append(section)
        if not options:
            return state.model_copy(update={"entries": [], "alternatives": [], "alternative_index": 0})
        groups.append(SolverGroup(key=course.code, options=tuple(options)))
        courses_by_key[course.code] = course
    if not groups:
        return state.model_copy(update={"entries": [], "alternatives": [], "alternative_index": 0})
    solutions = enumerate_solutions(
        groups,
        lambda section: [(day, start, end) for day, start, end, _ in _section_meetings(section)],
        avoid_conflicts=state.avoid_conflicts,
        max_solutions=MAX_ALTERNATIVES,
        max_nodes=MAX_SOLVER_NODES,
        allow_skip=False,
    )
    alternatives: list[list[PlanEntry]] = []
    for solution in solutions:
        entries: list[PlanEntry] = []
        for course_index, choice in enumerate(solution):
            entries.extend(_entry_for_choice(courses_by_key[choice.key], choice.option, course_index))
        alternatives.append(entries)
    alternatives.sort(
        key=lambda entries: (
            -len({entry.code for entry in entries if entry.kind == "course"}),
            _shape(entries),
        )
    )
    return state.model_copy(
        update={
            "entries": alternatives[0] if alternatives else [],
            "alternatives": alternatives,
            "alternative_index": 0,
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
    request_digest: str,
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
                request_digest=LEGACY_REQUEST_DIGEST,
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
            request_digest=request_digest,
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

    # Lock before the idempotency lookup so concurrent first writes are
    # serialized too. The transaction lock is released by commit/rollback.
    await _resource_lock(db, user_id, term)
    request_digest = _request_digest(changes, expected_revision)
    previous = await _history_for_key(db, user_id, term, idempotency_key)
    if previous is not None:
        previous_digest = getattr(previous, "request_digest", LEGACY_REQUEST_DIGEST)
        if previous_digest != request_digest:
            raise PlanIdempotencyError("Idempotency key was already used for different timetable changes")
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
    _validate_state(next_state)
    return await _commit(
        db,
        user_id,
        term,
        next_state,
        current=current,
        expected_revision=current_revision,
        idempotency_key=idempotency_key,
        operation=changes.operation,
        request_digest=request_digest,
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
    await _resource_lock(db, user_id, term)
    request_digest = _undo_request_digest(expected_revision)
    previous = await _history_for_key(db, user_id, term, idempotency_key)
    if previous is not None:
        previous_digest = getattr(previous, "request_digest", LEGACY_REQUEST_DIGEST)
        if previous_digest != request_digest:
            raise PlanIdempotencyError("Idempotency key was already used for a different undo request")
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
    _validate_state(state)
    return await _commit(
        db,
        user_id,
        term,
        state,
        current=current,
        expected_revision=current_revision,
        idempotency_key=idempotency_key,
        operation="undo",
        request_digest=request_digest,
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

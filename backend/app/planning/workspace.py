"""Owner service for the canonical, revisioned timetable resource.

The assistant and the schedule UI call this module.  Neither caller writes the
planning tables directly.  The database revision and idempotency constraints
are the final concurrency boundary; the service also takes a row lock so two
requests in one broker process observe the same revision ordering.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.digest import stable_digest
from app.db.models import StudentAcademicSnapshot, StudentTimetable, StudentTimetableRevision
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
_RELEASE_UNCHECKED = object()


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
    current_release_id: str | None | object = _RELEASE_UNCHECKED,
) -> PlanEnvelope:
    revision = _row_revision(row) if revision_override is None else revision_override
    payload = _row_payload(row) if payload_override is None else payload_override
    state = PlanState.from_legacy_payload(payload)
    release_id = state.catalog_release_id
    needs_revalidation = state.needs_revalidation
    if current_release_id is not _RELEASE_UNCHECKED:
        current = str(current_release_id) if current_release_id is not None else None
        # A read must never rewrite the saved plan just because publication
        # moved. Surface the mismatch in the envelope and leave the student's
        # exact snapshot available for an explicit revalidation/update.
        if release_id is not None and (current is None or current != release_id):
            needs_revalidation = True
        state = state.model_copy(update={"needs_revalidation": needs_revalidation})
    return PlanEnvelope(
        user_id=str(user_id),
        term=term,
        revision=revision,
        updated_at=updated_at_override if updated_at_override is not None else _row_updated_at(row),
        state=state,
        can_undo=await _has_undo(db, user_id, term, revision),
        previous_revision=revision - 1 if revision > 0 else None,
        operation=operation,
        idempotency_key=idempotency_key,
        catalog_release_id=release_id,
        needs_revalidation=needs_revalidation,
    )


async def _active_catalog_release(
    db: AsyncSession, user_id: UUID | str, term: str
) -> tuple[str | None, bool]:
    """Return the active release for a read without changing saved state.

    The second value records that the check was attempted. A temporary catalog
    outage should keep the timetable readable while marking a previously
    catalog-backed plan for revalidation instead of silently claiming it is
    current.
    """

    if not _published_mode():
        return None, False
    try:
        # A timetable read only needs the active release pointer. Loading all
        # offerings and rules here was both wasteful and capable of starting a
        # second, larger catalog read on the same request session. The catalog
        # service owns organization scoping and returns the immutable pointer.
        from app.academic_catalog.service import active_release_metadata

        metadata = await active_release_metadata(db, user_id, term)
        return _catalog_release(metadata), True
    except Exception:
        return None, True


async def read_timetable(db: AsyncSession, user_id: UUID | str, term: str) -> PlanEnvelope:
    """Read only the authenticated account's plan for ``term``."""

    row = await _row(db, user_id, term)
    release_id, checked = await _active_catalog_release(db, user_id, term)
    return await _envelope(
        db,
        user_id,
        term,
        row,
        current_release_id=release_id if checked else _RELEASE_UNCHECKED,
    )


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
                if row.get("eligible") is False and not entry.tentative:
                    reason = str(row.get("reason") or "section restrictions").strip()
                    raise PlanValidationError(f"section {entry.section} is not eligible: {reason}")


def _published_mode() -> bool:
    try:
        from app.config import get_settings

        return bool(getattr(get_settings(), "academic_catalog_reads_enabled", False))
    except Exception:
        return False


def _catalog_meetings(offering: Any) -> list[tuple[str, int, int, str]]:
    """Read canonical section meetings in the same minute contract as entries."""

    raw = getattr(offering, "schedule", None) or getattr(offering, "meetings", None) or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    result: list[tuple[str, int, int, str]] = []
    day_names = {
        "monday": "Mon", "mon": "Mon", "pazartesi": "Mon",
        "tuesday": "Tue", "tue": "Tue", "sali": "Tue", "salı": "Tue",
        "wednesday": "Wed", "wed": "Wed", "carsamba": "Wed", "çarşamba": "Wed",
        "thursday": "Thu", "thu": "Thu", "persembe": "Thu", "perşembe": "Thu",
        "friday": "Fri", "fri": "Fri", "cuma": "Fri",
    }
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_day = item.get("day") if item.get("day") is not None else item.get("weekday")
        if isinstance(raw_day, int) and not isinstance(raw_day, bool):
            day = ("Mon", "Tue", "Wed", "Thu", "Fri")[raw_day] if 0 <= raw_day <= 4 else ""
        else:
            day = day_names.get(str(raw_day or "").strip().casefold())
        if day is None:
            day = str(raw_day or "").strip()
        if not day:
            continue
        try:
            start_raw = item.get("start_minute", item.get("start"))
            end_raw = item.get("end_minute", item.get("end"))
            if end_raw is None and item.get("duration_minutes") is not None:
                end_raw = int(start_raw) + int(item["duration_minutes"])
            if isinstance(start_raw, str) and ":" in start_raw:
                hour, minute = start_raw.split(":", 1)
                start_raw = int(hour) * 60 + int(minute)
            if isinstance(end_raw, str) and ":" in end_raw:
                hour, minute = end_raw.split(":", 1)
                end_raw = int(hour) * 60 + int(minute)
            start, end = int(start_raw), int(end_raw)
        except (TypeError, ValueError, AttributeError):
            continue
        if not (0 <= start < end <= 24 * 60):
            continue
        result.append((day, start, end, str(item.get("room") or item.get("location") or "").strip()))
    return result


def _catalog_release(metadata: Any) -> str | None:
    if isinstance(metadata, dict):
        value = (
            metadata.get("catalog_release_id")
            or metadata.get("release_id")
            or metadata.get("revision_id")
            or metadata.get("id")
        )
    else:
        value = (
            getattr(metadata, "catalog_release_id", None)
            or getattr(metadata, "release_id", None)
            or getattr(metadata, "revision_id", None)
            or getattr(metadata, "id", None)
        )
    return str(value or "") or None


async def _validate_published_state(
    db: AsyncSession,
    user_id: UUID | str,
    term: str,
    state: PlanState,
) -> PlanState:
    """Resolve every course-linked entry against the published catalog.

    The browser's credits, title, instructor and meeting fields are input
    hints. Course entries are accepted only when they match a server-owned
    offering in the current immutable release; explicit tentative entries may
    remain as visible planning notes while awaiting verification.
    """

    from app.planning.service import (
        PLANNING_SNAPSHOT_MAX_AGE,
        _offering_decision_state,
        _published_plan_inputs,
    )

    course_entries = [
        entry
        for group in (state.entries, *state.alternatives, *state.favorites)
        for entry in group
        if entry.kind == "course" and not entry.tentative
    ]

    def mark_tentative(values: list[PlanEntry]) -> list[PlanEntry]:
        return [
            entry.model_copy(
                update={"tentative": True, "verification_status": "tentative", "catalog_release_id": None}
            )
            if entry.kind == "course" and entry.tentative
            else entry
            for entry in values
        ]

    # Blocks and explicitly tentative notes do not claim catalog facts. They
    # can be saved while the catalog or academic snapshot is unavailable. A
    # non-empty pool or section map is course-linked state too: otherwise a
    # client could inject ``eligible: true`` there and call ``solve`` later.
    if not course_entries and not state.pool and not state.sections:
        return state.model_copy(
            update={
                "entries": mark_tentative(state.entries),
                "alternatives": [mark_tentative(group) for group in state.alternatives],
                "favorites": [mark_tentative(group) for group in state.favorites],
                "sections": {},
            }
        )
    if state.sections and not state.pool and not course_entries:
        raise PlanValidationError("course sections require a published course pool")

    offerings, _, metadata = await _published_plan_inputs(db, user_id, term)
    release_id = _catalog_release(metadata)
    if release_id is None:
        raise PlanValidationError("published catalog release is unavailable")
    snapshot = await db.scalar(
        select(StudentAcademicSnapshot)
        .where(StudentAcademicSnapshot.user_id == user_id, StudentAcademicSnapshot.term == term)
        .order_by(StudentAcademicSnapshot.fetched_at.desc())
        .limit(1)
    )
    if snapshot is None:
        raise PlanValidationError("student academic snapshot is required to verify course entries")
    fetched_at = snapshot.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    snapshot_age = datetime.now(UTC) - fetched_at
    if snapshot_age < timedelta(0) or snapshot_age > PLANNING_SNAPSHOT_MAX_AGE:
        raise PlanValidationError("student academic snapshot is stale; refresh it before saving course entries")

    by_key: dict[tuple[str, str], Any] = {}
    by_course: dict[str, list[Any]] = {}

    def offering_code(offering: Any) -> str:
        return str(getattr(offering, "course_code", None) or getattr(offering, "code", "")).strip()

    for offering in offerings:
        code = offering_code(offering)
        if not code:
            continue
        aliases = [
            code,
            *(getattr(offering, "aliases", []) or []),
        ]
        for alias in aliases:
            by_key[(_identity(alias), str(getattr(offering, "section", "")))] = offering
            by_course.setdefault(_identity(alias), []).append(offering)

    def section_payload(offering: Any) -> dict[str, Any]:
        eligible = getattr(offering, "eligible", None)
        eligibility_status = (
            "eligible" if eligible is True else "ineligible" if eligible is False else "unknown"
        )
        return {
            "section": str(getattr(offering, "section", "")),
            "instructor": str(getattr(offering, "instructor", "") or ""),
            "meetings": [
                {
                    "day": day,
                    "start_minute": start,
                    "duration_minutes": end - start,
                    "room": room,
                }
                for day, start, end, room in _catalog_meetings(offering)
            ],
            "eligible": eligible,
            "eligibility_status": eligibility_status,
            "reason": "",
            "data_status": str(getattr(offering, "data_status", "unknown") or "unknown"),
            "fresh": getattr(offering, "fresh", False) is True,
            "meetings_status": str(
                getattr(offering, "meetings_status", "unknown") or "unknown"
            ),
            "complete": getattr(offering, "complete", False) is True,
            "catalog_release_id": release_id,
        }

    def resolve_pool_course(course: Any) -> tuple[Any, Any]:
        aliases = [getattr(course, "raw_code", None), getattr(course, "code", None)]
        candidates: list[Any] = []
        for alias in aliases:
            if alias:
                candidates.extend(by_course.get(_identity(alias), []))
        # Keep the result deterministic when a course carries both a short and
        # full spelling in its pool payload.
        unique = {id(item): item for item in candidates}
        candidates = sorted(unique.values(), key=lambda item: str(getattr(item, "section", "")))
        requested_section = str(getattr(course, "selected_section", "") or "").strip()
        if requested_section:
            candidates = [
                item for item in candidates if str(getattr(item, "section", "") or "").strip() == requested_section
            ]
        if not candidates:
            raise PlanValidationError(
                f"course {getattr(course, 'code', '')} is not in the published catalog"
            )
        canonical = candidates[0]
        canonical_code = offering_code(canonical)
        return canonical, canonical_code

    def resolve(entry: PlanEntry) -> PlanEntry:
        if entry.kind == "block":
            return entry
        if entry.tentative or entry.verification_status == "tentative":
            return entry.model_copy(
                update={
                    "tentative": True,
                    "verification_status": "tentative",
                    "catalog_release_id": None,
                }
            )
        offering = by_key.get((_identity(entry.code), str(entry.section)))
        if offering is None:
            raise PlanValidationError(
                f"course section {entry.code} {entry.section} is not in the published catalog"
            )
        if getattr(offering, "eligible", None) is not True:
            raise PlanValidationError(f"course section {entry.code} {entry.section} is not verified eligible")
        decision_state, reason = _offering_decision_state(offering)
        if decision_state != "usable":
            raise PlanValidationError(reason or f"course section {entry.code} {entry.section} is not schedulable")
        meetings = _catalog_meetings(offering)
        target = (entry.day, entry.start_minute, entry.start_minute + entry.duration_minutes)
        matching = next(
            (meeting for meeting in meetings if meeting[:3] == target),
            None,
        )
        if matching is None:
            raise PlanValidationError(
                f"meeting for course section {entry.code} {entry.section} does not match the published catalog"
            )
        canonical_credits = float(getattr(offering, "credits", 0) or 0)
        return entry.model_copy(
            update={
                "code": offering_code(offering) or entry.code,
                "name": str(getattr(offering, "title", entry.name) or entry.name),
                "credits": canonical_credits,
                "section": str(getattr(offering, "section", entry.section) or entry.section),
                "instructor": str(getattr(offering, "instructor", entry.instructor) or entry.instructor),
                "room": matching[3],
                "catalog_release_id": release_id,
                "verification_status": "verified",
                "tentative": False,
            }
        )

    def normalize_entries(values: list[PlanEntry]) -> list[PlanEntry]:
        normalized: list[PlanEntry] = []
        credited_courses: set[tuple[str, str]] = set()
        for entry in values:
            resolved = resolve(entry)
            if resolved.kind == "course" and not resolved.tentative:
                key = (_identity(resolved.code), resolved.section)
                if key in credited_courses:
                    resolved = resolved.model_copy(update={"credits": 0})
                else:
                    credited_courses.add(key)
            normalized.append(resolved)
        return normalized

    normalized_pool = []
    normalized_sections: dict[str, list[dict[str, Any]]] = {}
    for course in state.pool:
        canonical, canonical_code = resolve_pool_course(course)
        canonical_credits = float(getattr(canonical, "credits", 0) or 0)
        normalized_pool.append(
            course.model_copy(
                update={
                    "name": str(getattr(canonical, "title", course.name) or course.name),
                    "credits": canonical_credits,
                    "raw_code": canonical_code,
                }
            )
        )
        normalized_sections[_identity(canonical_code)] = [
            section_payload(item) for item in by_course.get(_identity(canonical_code), [])
        ]

    return state.model_copy(
        update={
            "entries": normalize_entries(state.entries),
            "alternatives": [normalize_entries(group) for group in state.alternatives],
            "favorites": [normalize_entries(group) for group in state.favorites],
            "pool": normalized_pool,
            "sections": normalized_sections,
            "catalog_release_id": release_id,
            "academic_snapshot_fetched_at": snapshot.fetched_at if snapshot is not None else state.academic_snapshot_fetched_at,
            "needs_revalidation": False,
        }
    )


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
    if operation == "apply_proposal":
        if changes.entries is None or changes.pool is None:
            raise PlanValidationError("apply_proposal requires entries and pool")
        # A proposal replaces scheduled course entries, while student-authored
        # busy blocks remain part of the timetable. The selected pool carries
        # credit-bearing untimed courses without manufacturing a PlanEntry.
        blocks = [entry for entry in state.entries if entry.kind == "block"]
        return state.model_copy(
            update={
                "entries": [*blocks, *changes.entries],
                "pool": list(changes.pool),
                "sections": changes.sections if changes.sections is not None else {},
                "alternatives": [],
                "alternative_index": 0,
            }
        )
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
        if _published_mode():
            # A missing verdict is unknown. It may be shown to the student for
            # manual review, but an automatic solve can only use an explicit
            # server-computed eligible=True section.
            eligible_sections = [section for section in timed_sections if section.get("eligible") is True]
        else:
            eligible_sections = [
                section
                for section in timed_sections
                if state.ignore_constraints or section.get("eligible") is not False
            ]
        # A course for which every timed section is closed to the student is
        # reported separately by the client. It cannot be made schedulable by
        # choosing another combination, so it must not suppress valid plans
        # for the remaining courses.
        if not eligible_sections:
            continue
        options = []
        for section in eligible_sections:
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
    if _published_mode():
        next_state = await _validate_published_state(db, user_id, term, next_state)
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
    if _published_mode():
        state = await _validate_published_state(db, user_id, term, state)
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
                "tentative": entry.tentative,
                "verification_status": entry.verification_status,
                "catalog_release_id": entry.catalog_release_id,
            },
        )
        course["credits"] += entry.credits
        course["meetings"].append(meeting)
        if entry.tentative:
            course["tentative"] = True
            course["verification_status"] = "tentative"
        elif course.get("catalog_release_id") is None and entry.catalog_release_id:
            course["catalog_release_id"] = entry.catalog_release_id

    # A selected untimed course is a credit-planning fact, not a calendar
    # event. Keep it in the projection with an empty meeting list and an
    # explicit status so downstream consumers do not mistake missing schedule
    # data for either a fabricated meeting or a silently dropped course.
    for pool_course in state.pool:
        if getattr(pool_course, "selected", False) is not True:
            continue
        aliases = {
            _identity(getattr(pool_course, "code", "")),
            _identity(getattr(pool_course, "raw_code", "")),
        }
        aliases.discard("")
        raw_sections = state.sections.get(_identity(getattr(pool_course, "raw_code", "")), [])
        if not raw_sections:
            raw_sections = state.sections.get(_identity(getattr(pool_course, "code", "")), [])
        if isinstance(raw_sections, dict):
            raw_sections = list(raw_sections.values())
        rows = [row for row in raw_sections if isinstance(row, dict)]
        selected_section = str(getattr(pool_course, "selected_section", "") or "").strip()
        untimed_rows = []
        for row in rows:
            status = str(row.get("meetings_status") or row.get("meeting_status") or "").strip().casefold()
            if status not in {"untimed", "explicitly_untimed"}:
                continue
            row_section = str(row.get("section") or row.get("section_number") or "").strip()
            if selected_section and row_section != selected_section:
                continue
            if _section_meetings(row):
                # Contradictory catalog evidence stays out of the projection;
                # the write boundary rejects it rather than inventing a
                # scheduling interpretation.
                continue
            untimed_rows.append(row)
        if not selected_section and len(untimed_rows) > 1:
            # A pool course without a selected section is a candidate set, not
            # enough information to claim that every untimed section was
            # selected. Leave the ambiguity visible in state for the caller
            # to resolve instead of duplicating credits in the projection.
            untimed_rows = []
        if not untimed_rows:
            status = str(getattr(pool_course, "timing_status", "") or "").strip().casefold()
            if status in {"untimed", "explicitly_untimed"} and not rows:
                untimed_rows = [{}]
        for row in untimed_rows:
            section = str(row.get("section") or row.get("section_number") or "")
            if any(
                _identity(code) in aliases and (not section or stored_section == section)
                for code, stored_section in courses
            ):
                continue
            key = (str(getattr(pool_course, "code", "")), section)
            eligible = row.get("eligible")
            data_status = str(row.get("data_status") or "").strip().casefold()
            meeting_status = str(
                row.get("meetings_status") or row.get("meeting_status") or ""
            ).strip().casefold()
            verified = (
                eligible is True
                and data_status in {"fresh", "verified", "current"}
                and meeting_status in {"untimed", "explicitly_untimed"}
            )
            courses[key] = {
                "code": getattr(pool_course, "code", ""),
                "name": getattr(pool_course, "name", ""),
                "section": section,
                "credits": float(getattr(pool_course, "credits", 0) or 0),
                "instructor": str(row.get("instructor") or ""),
                "meetings": [],
                "tentative": False,
                "verification_status": "verified" if verified else "unknown",
                "catalog_release_id": state.catalog_release_id,
                "timing_status": "untimed",
                "eligible": eligible,
            }
    return {"courses": list(courses.values()), "busy_blocks": list(blocks.values())}

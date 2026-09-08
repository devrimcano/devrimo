import asyncio
from datetime import UTC, datetime

import pytest

from app.db.models import CampusCredential, StudentContext, StudentTimetable
from app.planning.eligibility import issue_eligibility_token, planning_context_fingerprint
from app.planning.models import PlanChanges, PlanState, PlanValidationError
from app.planning.workspace import _apply_changes, _switch_mode, _validate_state
from app.db.session import SessionLocal
from tests.conftest import auth_header, new_user_id


def _entry(entry_id: str = "math-mon", *, kind: str = "block") -> dict:
    return {
        "id": entry_id,
        "code": "MATH 101",
        "name": "Calculus",
        "section": "1",
        "credits": 3,
        "color": 0,
        "kind": kind,
        "instructor": "Prof X",
        "day": "Mon",
        "start_minute": 520,
        "duration_minutes": 110,
        "room": "B-101",
    }


async def _verified_planner_user(user_id):
    async with SessionLocal() as db:
        now = datetime.now(UTC)
        db.add(
            CampusCredential(
                user_id=user_id,
                metu_username="planner-user",
                metu_password_enc=b"encrypted",
                verified_at=now,
            )
        )
        db.add(
            StudentContext(
                user_id=user_id,
                department="MATH",
                verified_at=now,
                surname_prefix="AA",
            )
        )
        await db.commit()


def test_what_if_mode_preserves_and_restores_the_normal_draft():
    normal = PlanState(entries=[_entry()], what_if=False)
    exploratory = normal.model_copy(update={"what_if": True, "ignore_constraints": True, "entries": []})

    entered = _switch_mode(normal, exploratory)
    assert entered.what_if is True
    assert entered.what_if_backup is not None
    assert entered.what_if_backup["entries"][0]["id"] == "math-mon"

    changed = entered.model_copy(update={"entries": []})
    exiting = changed.model_copy(update={"what_if": False, "ignore_constraints": False})
    restored = _switch_mode(entered, exiting)
    assert restored.what_if is False
    assert [entry.id for entry in restored.entries] == ["math-mon"]
    assert restored.what_if_backup is None


def test_regenerate_leaving_what_if_restores_normal_section_locks():
    normal = PlanState(
        entries=[_entry()],
        locked_sections={"MATH119": "1"},
        what_if=False,
    )
    exploratory = _switch_mode(
        normal,
        normal.model_copy(update={"what_if": True, "ignore_constraints": True}),
    )
    result = _apply_changes(
        exploratory,
        PlanChanges(
            operation="regenerate",
            what_if=False,
            pool=[],
            sections={},
            locked_sections={"MATH119": "9"},
        ),
    )
    assert result.what_if is False
    assert result.locked_sections == {"MATH119": "1"}


def test_state_extras_are_dropped_and_nested_backup_is_bounded():
    state = PlanState.model_validate({"x": ["x"] * 20_001})
    assert "x" not in state.model_dump()
    with pytest.raises(ValueError, match="too many items"):
        PlanState.model_validate({"what_if_backup": {"x": ["x"] * 501}})


def test_owner_validation_converts_bypass_budget_failures_to_plan_errors():
    state = PlanState.model_construct(sections={"MATH101": [{}] * 501})
    with pytest.raises(PlanValidationError, match="too many items"):
        _validate_state(state)


def test_normal_state_rejects_unverified_catalog_section():
    state = PlanState(
        pool=[{"code": "MATH101", "name": "Math", "credits": 3}],
        sections={"MATH101": [{"section": "1", "eligible": True, "eligibility_status": "unverified", "meetings": []}]},
        entries=[_entry(kind="course")],
    )
    with pytest.raises(PlanValidationError):
        _validate_state(state)


def test_normal_state_accepts_a_real_short_code_and_numeric_catalog_alias():
    user_id = new_user_id()
    verified_at = datetime.now(UTC)
    context_fingerprint = planning_context_fingerprint({
        "department": "MATH",
        "program_code": None,
        "degree_level": None,
        "year_of_study": None,
        "surname_prefix": None,
        "campus": None,
        "verified_at": verified_at,
        "confirmed_at": None,
    })
    meetings = [{"day": "Mon", "start_minute": 520, "duration_minutes": 110, "room": "A1"}]
    token = issue_eligibility_token(
        user_id,
        "20261",
        "MATH119",
        "1",
        eligibility_status="verified",
        eligible=True,
        context_verified_at=verified_at,
        snapshot_fetched_at=None,
        meetings=meetings,
        context_fingerprint=context_fingerprint,
    )
    state = PlanState(
        pool=[{"code": "MATH119", "raw_code": "2360119", "name": "Math"}],
        sections={"MATH119": [{
            "section": "1",
            "eligibility_status": "verified",
            "eligible": True,
            "eligibility_token": token,
            "eligibility_course_code": "MATH119",
            "eligibility_raw_code": "2360119",
            "meetings": meetings,
        }]},
        entries=[{**_entry(kind="course"), "code": "MATH119", "room": "A1"}],
    )
    _validate_state(
        state,
        user_id=user_id,
        term="20261",
        context_verified_at=verified_at,
        snapshot_fetched_at=None,
        context_fingerprint=context_fingerprint,
    )


def test_normal_state_rejects_a_token_relabelled_as_another_course():
    user_id = new_user_id()
    verified_at = datetime.now(UTC)
    context_fingerprint = planning_context_fingerprint({
        "department": "MATH", "verified_at": verified_at,
    })
    meetings = [{"day": "Mon", "start_minute": 520, "duration_minutes": 110, "room": "A1"}]
    token = issue_eligibility_token(
        user_id, "20261", "MATH119", "1", eligibility_status="verified", eligible=True,
        context_verified_at=verified_at, snapshot_fetched_at=None, meetings=meetings,
        context_fingerprint=context_fingerprint,
    )
    state = PlanState(
        pool=[{"code": "OTHER999", "raw_code": "2360119"}],
        sections={"OTHER999": [{
            "section": "1", "eligibility_status": "verified", "eligible": True,
            "eligibility_token": token, "eligibility_course_code": "MATH119",
            "eligibility_raw_code": "2360119", "meetings": meetings,
        }]},
        entries=[{**_entry(kind="course"), "code": "OTHER999", "room": "A1"}],
    )
    with pytest.raises(PlanValidationError, match="bound to a different course"):
        _validate_state(
            state, user_id=user_id, term="20261", context_verified_at=verified_at,
            snapshot_fetched_at=None, context_fingerprint=context_fingerprint,
        )


def test_legacy_ignore_constraints_flag_cannot_bypass_normal_validation():
    state = PlanState(
        pool=[{"code": "MATH101", "name": "Math", "credits": 3}],
        sections={"MATH101": [{"section": "1", "eligible": None, "eligibility_status": "unverified", "meetings": []}]},
        entries=[_entry()],
        ignore_constraints=True,
        what_if=False,
    )
    with pytest.raises(PlanValidationError, match="explicit what-if"):
        _validate_state(state)


def test_failed_regeneration_keeps_last_calendar_and_reports_unscheduled_courses():
    state = PlanState(
        what_if=True,
        ignore_constraints=True,
        pool=[{"code": "MATH101", "name": "Math", "credits": 3}],
        sections={
            "MATH101": [{
                "section": "1",
                "meetings": [{"day": "Mon", "start_minute": 520, "duration_minutes": 110, "room": "A1"}],
            }],
        },
        entries=[_entry("math-course", kind="course")],
    )
    failed = _apply_changes(
        state,
        PlanChanges(
            operation="regenerate",
            what_if=True,
            pool=[
                {"code": "MATH101", "name": "Math", "credits": 3},
                {"code": "PHYS101", "name": "Physics", "credits": 3},
            ],
            sections=state.sections,
        ),
    )

    assert [entry.id for entry in failed.entries] == ["math-course"]
    assert failed.unscheduled_courses == ["PHYS101"]
    assert failed.generation_error
    assert failed.alternatives == []


def test_clearing_pool_removes_courses_but_keeps_personal_blocks():
    state = PlanState(entries=[_entry("course", kind="course"), {**_entry("block"), "kind": "block", "code": "BLOCK:WORK"}], pool=[{"code": "MATH101"}])
    cleared = _apply_changes(state, PlanChanges(operation="clear_pool"))
    assert [entry.id for entry in cleared.entries] == ["block"]
    assert cleared.pool == []
    assert cleared.sections == {}
    assert cleared.locked_sections == {}


async def test_timetable_revision_idempotency_conflict_undo_and_user_scope(client):
    user_id = new_user_id()
    other_user_id = new_user_id()
    await _verified_planner_user(user_id)
    await _verified_planner_user(other_user_id)
    headers = auth_header(user_id)
    other_headers = auth_header(other_user_id)
    term = "20261"

    initial = await client.get(f"/api/v1/schedule/timetable?term={term}", headers=headers)
    assert initial.status_code == 200
    assert initial.json()["revision"] == 0
    assert initial.json()["state"]["entries"] == []

    request = {
        "expected_revision": 0,
        "idempotency_key": "planner-test-1",
        "changes": {"operation": "add_entry", "entry": _entry()},
    }
    saved = await client.patch(f"/api/v1/schedule/timetable?term={term}", headers=headers, json=request)
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert saved.json()["state"]["entries"][0]["start_minute"] == 520

    replay = await client.patch(f"/api/v1/schedule/timetable?term={term}", headers=headers, json=request)
    assert replay.status_code == 200
    assert replay.json()["revision"] == 1
    assert replay.json()["idempotency_key"] == "planner-test-1"

    conflict = await client.patch(
        f"/api/v1/schedule/timetable?term={term}",
        headers=headers,
        json={**request, "idempotency_key": "planner-test-stale"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "revision_conflict"
    assert conflict.json()["current"]["revision"] == 1

    other = await client.get(f"/api/v1/schedule/timetable?term={term}", headers=other_headers)
    assert other.status_code == 200
    assert other.json()["revision"] == 0
    assert other.json()["state"]["entries"] == []

    undone = await client.post(
        f"/api/v1/schedule/timetable/undo?term={term}",
        headers=headers,
        json={"expected_revision": 1, "idempotency_key": "planner-test-undo"},
    )
    assert undone.status_code == 200
    assert undone.json()["revision"] == 2
    assert undone.json()["state"]["entries"] == []


async def test_first_edit_seeds_and_undoes_an_existing_projection(client):
    user_id = new_user_id()
    await _verified_planner_user(user_id)
    headers = auth_header(user_id)
    term = "20261"
    await client.get(f"/api/v1/schedule/timetable?term={term}", headers=headers)
    async with SessionLocal() as db:
        db.add(
            StudentTimetable(
                user_id=user_id,
                term=term,
                revision=0,
                payload={
                    "courses": [
                        {
                            "code": "HIST 2201",
                            "name": "History",
                            "section": "1",
                            "credits": 3,
                            "meetings": [{"day": "Wed", "start": 10, "duration": 2}],
                        }
                    ],
                    "busy_blocks": [],
                },
            )
        )
        await db.commit()

    saved = await client.patch(
        f"/api/v1/schedule/timetable?term={term}",
        headers=headers,
        json={
            "expected_revision": 0,
            "idempotency_key": "planner-existing-edit",
            "changes": {"operation": "add_entry", "entry": _entry("new-entry")},
        },
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert {entry["code"] for entry in saved.json()["state"]["entries"]} == {"HIST 2201", "MATH 101"}

    undone = await client.post(
        f"/api/v1/schedule/timetable/undo?term={term}",
        headers=headers,
        json={"expected_revision": 1, "idempotency_key": "planner-existing-undo"},
    )
    assert undone.status_code == 200
    assert undone.json()["revision"] == 2
    assert [entry["code"] for entry in undone.json()["state"]["entries"]] == ["HIST 2201"]


async def test_concurrent_first_writes_are_ordered_by_revision(client):
    user_id = new_user_id()
    await _verified_planner_user(user_id)
    headers = auth_header(user_id)
    term = "20261"

    async def write(key: str):
        return await client.patch(
            f"/api/v1/schedule/timetable?term={term}",
            headers=headers,
            json={
                "expected_revision": 0,
                "idempotency_key": key,
                "changes": {"operation": "add_entry", "entry": _entry(key)},
            },
        )

    first, second = await asyncio.gather(write("concurrent-a"), write("concurrent-b"))
    assert sorted((first.status_code, second.status_code)) == [200, 409]

    current = await client.get(f"/api/v1/schedule/timetable?term={term}", headers=headers)
    assert current.status_code == 200
    assert current.json()["revision"] == 1


async def test_timetable_ics_export_is_revision_bound_and_stable(client):
    user_id = new_user_id()
    await _verified_planner_user(user_id)
    headers = auth_header(user_id)
    term = "20261"
    saved = await client.patch(
        f"/api/v1/schedule/timetable?term={term}",
        headers=headers,
        json={
            "expected_revision": 0,
            "idempotency_key": "ics-seed",
            "changes": {"operation": "add_entry", "entry": _entry()},
        },
    )
    assert saved.status_code == 200
    revision = saved.json()["revision"]
    first = await client.get(
        f"/api/v1/schedule/timetable/export.ics?term={term}&start_date=2026-09-01&end_date=2027-01-31&revision={revision}",
        headers=headers,
    )
    second = await client.get(
        f"/api/v1/schedule/timetable/export.ics?term={term}&start_date=2026-09-01&end_date=2027-01-31&revision={revision}",
        headers=headers,
    )
    assert first.status_code == second.status_code == 200
    assert first.text == second.text
    assert "RRULE" in first.text
    assert "Europe/Istanbul" in first.text

    stale = await client.get(
        f"/api/v1/schedule/timetable/export.ics?term={term}&start_date=2026-09-01&end_date=2027-01-31&revision=0",
        headers=headers,
    )
    assert stale.status_code == 409

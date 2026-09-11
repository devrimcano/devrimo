import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.db.models import StudentTimetable
from app.db.session import SessionLocal
from app.planning import service as planning_service
from app.planning import workspace as planning_workspace
from app.planning.models import PlanState
from tests.conftest import auth_header, new_user_id


def _entry(entry_id: str = "math-mon") -> dict:
    return {
        "id": entry_id,
        "code": "MATH 101",
        "name": "Calculus",
        "section": "1",
        "credits": 3,
        "color": 0,
        "kind": "course",
        "instructor": "Prof X",
        "day": "Mon",
        "start_minute": 520,
        "duration_minutes": 110,
        "room": "B-101",
    }


async def test_timetable_revision_idempotency_conflict_undo_and_user_scope(client):
    user_id = new_user_id()
    other_user_id = new_user_id()
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

    collision = await client.patch(
        f"/api/v1/schedule/timetable?term={term}",
        headers=headers,
        json={**request, "changes": {"operation": "add_entry", "entry": _entry("different-entry")}},
    )
    assert collision.status_code == 409
    assert "different timetable changes" in collision.json()["detail"]

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


async def test_published_write_uses_server_credits_and_preserves_tentative_entries(monkeypatch):
    from app.config import get_settings
    from app.db.models import StudentAcademicSnapshot

    monkeypatch.setattr(get_settings(), "academic_catalog_reads_enabled", True)
    user_id = new_user_id()
    term = "20261"
    fetched_at = datetime.now(UTC)
    offering = SimpleNamespace(
        course_code="2402201",
        section="1",
        title="History",
        credits=3,
        aliases=["HIST2201"],
        schedule=[{"day": "Mon", "start_minute": 540, "end_minute": 590, "room": "B1"}],
        instructor="Registrar",
        eligible=True,
        data_status="fresh",
        fresh=True,
        meetings_status="verified",
        complete=True,
    )

    async with SessionLocal() as db:
        db.add(
            StudentAcademicSnapshot(
                user_id=user_id,
                term=term,
                completed_courses=[],
                enrolled_courses=[],
                current_credits=30,
                current_grade_points=90,
                fetched_at=fetched_at,
                source="sais",
            )
        )
        await db.commit()

        async def published_inputs(_db, _user_id, _term, _course_codes=None):
            return [offering], {}, {"catalog_release_id": "release-a"}

        monkeypatch.setattr(planning_service, "_published_plan_inputs", published_inputs)
        state = PlanState(
            pool=[{"code": "HIST2201", "name": "client supplied", "credits": 59}],
            entries=[
                {
                    **_entry("tentative-history"),
                    "code": "HIST2201",
                    "tentative": True,
                    "verification_status": "tentative",
                }
            ],
        )
        normalized = await planning_workspace._validate_published_state(db, user_id, term, state)

    assert normalized.catalog_release_id == "release-a"
    assert normalized.pool[0].raw_code == "2402201"
    assert normalized.pool[0].credits == 3
    assert normalized.entries[0].tentative is True
    assert normalized.entries[0].verification_status == "tentative"
    assert normalized.sections["2402201"][0]["eligible"] is True


async def test_published_plan_keeps_explicitly_untimed_course_for_credit_planning(monkeypatch):
    from app.config import get_settings
    from app.db.models import StudentAcademicSnapshot
    from app.planning.service import SemesterPlanRequest

    monkeypatch.setattr(get_settings(), "academic_catalog_reads_enabled", True)
    user_id = new_user_id()
    term = "20261"
    snapshot = StudentAcademicSnapshot(
        user_id=user_id,
        term=term,
        completed_courses=[],
        enrolled_courses=[],
        current_credits=30,
        current_grade_points=90,
        fetched_at=datetime.now(UTC),
        source="sais",
    )
    offering = SimpleNamespace(
        course_code="2402201",
        section="1",
        title="Thesis",
        credits=3,
        schedule=[],
        source_url=None,
        eligible=True,
        data_status="fresh",
        fresh=True,
        meetings_status="untimed",
        complete=True,
        instructor="",
    )
    monkeypatch.setattr(
        planning_service,
        "prepare_planning_snapshot",
        AsyncMock(return_value=(snapshot, {"fresh": True, "status": "fresh"})),
    )
    monkeypatch.setattr(
        planning_service,
        "_published_plan_inputs",
        AsyncMock(
            return_value=(
                [offering],
                {"2402201": {"prerequisites": [], "data_status": "verified", "fresh": True}},
                {"catalog_release_id": "release-a"},
            )
        ),
    )

    async with SessionLocal() as db:
        result = await planning_service.plan_semester(
            db,
            user_id,
            SemesterPlanRequest(term=term, required_courses=["2402201"], min_credits=3, max_credits=6),
        )

    assert result["status"] == "ok"
    assert result["selected_credits"] == 3
    assert result["courses"] == [{
        "course_code": "2402201",
        "section": "1",
        "title": "Thesis",
        "credits": 3.0,
        "schedule": [],
        "source_url": None,
        "timing_status": "untimed",
    }]


async def test_published_timetable_read_marks_release_mismatch_without_rewriting_plan(monkeypatch):
    user_id = new_user_id()
    term = "20261"
    row = SimpleNamespace(
        revision=4,
        updated_at=datetime.now(UTC),
        payload=PlanState(catalog_release_id="release-a").model_dump(mode="json"),
    )
    monkeypatch.setattr(planning_workspace, "_has_undo", AsyncMock(return_value=False))

    envelope = await planning_workspace._envelope(
        SimpleNamespace(),
        user_id,
        term,
        row,
        current_release_id="release-b",
    )

    assert envelope.catalog_release_id == "release-a"
    assert envelope.needs_revalidation is True
    assert envelope.state.catalog_release_id == "release-a"
    assert envelope.state.needs_revalidation is True
    assert row.payload["catalog_release_id"] == "release-a"

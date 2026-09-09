from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.db.models import StudentAcademicSnapshot
from app.db.session import SessionLocal
from app.planning.models import PlanChanges, PlanState
from app.planning.proposals import proposal_changes
from app.planning import service as planning_service
from app.planning import workspace as planning_workspace
from app.planning.workspace import _apply_changes, projection_from_state, read_timetable, update_timetable
from app.workspace.resources import ResourceRef
from app.workspace.service import WorkspaceService
from tests.conftest import auth_header, new_user_id


def proposal():
    return {
        "status": "ok", "term": "20261",
        "courses": [{
            "course_code": "MATH101", "section": "1", "title": "Calculus", "credits": 4,
            "schedule": [
                {"day": "Mon", "start": "09:15", "end": "10:05", "room": "A1"},
                {"day": "Wed", "start": "13:00", "end": "14:30", "room": "A1"},
            ],
        }],
    }


def test_proposal_preserves_exact_meetings_and_counts_credits_once():
    changes = proposal_changes(proposal())
    assert changes.operation == "set_entries"
    assert [(row.start_minute, row.duration_minutes) for row in changes.entries] == [(555, 50), (780, 90)]
    assert sum(row.credits for row in changes.entries) == 4
    assert PlanChanges.model_validate(changes.model_dump()) == changes


def test_incomplete_proposal_never_silently_applies_a_partial_schedule():
    for bad_meeting in [{"day": "Mon"}, {"day": "Sat", "start": "09:00", "end": "10:00"}]:
        data = deepcopy(proposal())
        data["courses"][0]["schedule"].append(bad_meeting)
        assert proposal_changes(data) is None
    assert proposal_changes({"status": "needs_academic_snapshot"}) is None
    assert proposal_changes({**proposal(), "status": "constraints_unsatisfied"}) is None


def test_mixed_proposal_persists_explicitly_untimed_course_without_a_meeting():
    data = proposal()
    data["courses"].append(
        {
            "course_code": "2404901",
            "section": "1",
            "title": "Thesis",
            "credits": 3,
            "schedule": [],
            "timing_status": "untimed",
        }
    )

    changes = proposal_changes(data)

    assert changes is not None
    assert changes.operation == "apply_proposal"
    assert len(changes.entries) == 2
    assert [(course.code, course.selected, course.timing_status) for course in changes.pool] == [
        ("MATH101", True, None),
        ("2404901", True, "untimed"),
    ]

    state = _apply_changes(
        PlanState(
            entries=[
                {
                    "id": "block:work:0",
                    "code": "BLOCK:work",
                    "name": "work",
                    "kind": "block",
                    "day": "Fri",
                    "start_minute": 600,
                    "duration_minutes": 60,
                }
            ]
        ),
        changes,
    )
    assert [entry.kind for entry in state.entries] == ["block", "course", "course"]
    assert state.pool[-1].timing_status == "untimed"
    assert state.pool[-1].selected is True

    projection = projection_from_state(state)
    untimed = next(course for course in projection["courses"] if course["code"] == "2404901")
    assert untimed["meetings"] == []
    assert untimed["timing_status"] == "untimed"


def test_projection_keeps_only_the_selected_untimed_section():
    state = PlanState(
        pool=[
            {
                "code": "THESIS",
                "name": "Thesis",
                "credits": 3,
                "raw_code": "2404901",
                "selected": True,
                "timing_status": "untimed",
                "selected_section": "2",
            }
        ],
        sections={
            "2404901": [
                {"section": "1", "meetings_status": "untimed", "eligible": True, "data_status": "fresh"},
                {"section": "2", "meetings_status": "untimed", "eligible": True, "data_status": "fresh"},
            ]
        },
    )

    courses = projection_from_state(state)["courses"]

    assert len(courses) == 1
    assert courses[0]["section"] == "2"
    assert courses[0]["credits"] == 3
    assert courses[0]["meetings"] == []


async def test_mixed_published_proposal_saves_and_reads_untimed_course(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "academic_catalog_reads_enabled", True)
    user_id = new_user_id()
    term = "20261"
    now = datetime.now(UTC)
    timed = SimpleNamespace(
        course_code="2400101",
        section="1",
        title="Calculus",
        credits=4,
        aliases=["MATH101"],
        schedule=[
            {"day": "Mon", "start_minute": 555, "end_minute": 605, "room": "A1"},
            {"day": "Wed", "start_minute": 780, "end_minute": 870, "room": "A1"},
        ],
        instructor="Registrar",
        eligible=True,
        data_status="fresh",
        fresh=True,
        meetings_status="verified",
        complete=True,
    )
    untimed = SimpleNamespace(
        course_code="2404901",
        section="1",
        title="Thesis",
        credits=3,
        aliases=[],
        schedule=[],
        instructor="",
        eligible=True,
        data_status="fresh",
        fresh=True,
        meetings_status="untimed",
        complete=True,
    )
    rules = {
        "2400101": {"prerequisites": [], "data_status": "verified", "fresh": True},
        "2404901": {"prerequisites": [], "data_status": "verified", "fresh": True},
    }
    monkeypatch.setattr(
        planning_service,
        "_published_plan_inputs",
        AsyncMock(return_value=([timed, untimed], rules, {"catalog_release_id": "release-a"})),
    )
    monkeypatch.setattr(
        planning_workspace,
        "_active_catalog_release",
        AsyncMock(return_value=("release-a", True)),
    )

    data = proposal()
    data["courses"][0]["course_code"] = "MATH101"
    data["courses"].append(
        {
            "course_code": "2404901",
            "section": "1",
            "title": "Thesis",
            "credits": 3,
            "schedule": [],
            "timing_status": "untimed",
        }
    )
    changes = proposal_changes(data)
    assert changes is not None and changes.operation == "apply_proposal"

    async with SessionLocal() as db:
        db.add(
            StudentAcademicSnapshot(
                user_id=user_id,
                term=term,
                completed_courses=[],
                enrolled_courses=[],
                current_credits=30,
                current_grade_points=90,
                fetched_at=now,
                source="sais",
            )
        )
        await db.commit()
        saved = await update_timetable(db, user_id, term, changes, 0, "proposal-untimed")
        read = await read_timetable(db, user_id, term)

    assert saved.revision == 1
    assert read.state.catalog_release_id == "release-a"
    assert [(course.code, course.timing_status, course.selected) for course in read.state.pool] == [
        ("MATH101", None, True),
        ("2404901", "untimed", True),
    ]
    assert len(read.state.entries) == 2
    projection = projection_from_state(read.state)
    thesis = next(course for course in projection["courses"] if course["code"] == "2404901")
    assert thesis["meetings"] == []
    assert thesis["timing_status"] == "untimed"


async def test_proposal_is_unsaved_and_applies_through_revisioned_update(client, monkeypatch):
    user = new_user_id()
    headers = auth_header(user)
    path = "/api/v1/schedule/timetable?term=20261"
    assert (await client.get(path, headers=headers)).status_code == 200
    monkeypatch.setattr("app.planning.mcp_bridge.sync_planning_snapshot_from_sais", AsyncMock())
    monkeypatch.setattr("app.planning.service.plan_semester", AsyncMock(return_value=proposal()))
    service = WorkspaceService(user)
    result = await service.plan({"term": "20261"})
    assert result["resource"]["kind"] == "planning.proposal"
    assert (await client.get(path, headers=headers)).json()["revision"] == 0
    application = result["data"]["application"]
    assert application["expected_revision"] == 0
    saved = await service.update(
        ResourceRef.model_validate(application["resource"]), application["changes"],
        application["expected_revision"], "apply-proposal-1",
    )
    assert saved["data"]["revision"] == 1
    assert saved["data"]["state"]["entries"][0]["start_minute"] == 555
    replay = await service.update(
        ResourceRef.model_validate(application["resource"]), application["changes"], 0, "apply-proposal-1",
    )
    assert replay["data"]["revision"] == 1
    stale = await client.patch(path, headers=headers, json={
        "changes": application["changes"], "expected_revision": 0, "idempotency_key": "apply-proposal-2",
    })
    assert stale.status_code == 409
    undone = await service.undo(ResourceRef(kind="planning.timetable", term="20261"), 1, "undo-proposal")
    assert undone["data"]["state"]["entries"] == []

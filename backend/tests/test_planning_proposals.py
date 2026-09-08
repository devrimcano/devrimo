from copy import deepcopy
from unittest.mock import AsyncMock

from app.planning.models import PlanChanges
from app.planning.proposals import proposal_changes
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

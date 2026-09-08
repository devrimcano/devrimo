import asyncio

from app.db.models import StudentTimetable
from app.db.session import SessionLocal
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

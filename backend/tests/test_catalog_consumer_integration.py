"""Real source-shaped imports must be usable by both shared catalog consumers."""

from datetime import UTC, datetime
import json
from uuid import UUID, uuid4

from sqlalchemy import select

from app.academic_catalog import service as catalog
from app.academic_catalog.models import CatalogDraft, CatalogReleaseItem, CatalogTermActiveRelease
from app.admin.directory import METU_ID
from app.config import get_settings
from app.db.models import StudentAcademicSnapshot
from app.db.session import SessionLocal
from app.planning import service as planning
from tests.conftest import auth_header, new_user_id

TERM = "20261"
COURSE = "2402201"


async def import_and_publish(db, user):
    now = datetime.now(UTC)
    values = {"semester": TERM, "department": "240", "course": COURSE}
    observations = [
        ("list_program_courses", {"semester": TERM, "department": "240"}, [
            {"course_code": COURSE, "name": "Fixture Course", "credit": "3", "ects_credit": "5", "type": "Open"},
        ]),
        ("get_course_info", values, {
            "department": "240", "semester": TERM, "course_code": COURSE,
            "course_name": "Fixture Course", "credit_info": "3.00(3.00,0.00,0.00)",
            "sections": [{"section": "1", "instructors": ["STAFF"], "critical_info": "",
                          "schedule": [{"day": "Monday", "time": "09:00 - 09:50", "room": "B1"}]}],
        }),
        ("get_section_constraints", {**values, "section": "1"}, {**values, "course_code": COURSE, "section": "1", "constraints": []}),
        ("get_course_prerequisites", values, []),
        ("get_course_replacements", values, []),
    ]
    for tool, arguments, payload in observations:
        result = await catalog.ingest_observation(db, METU_ID, tool, arguments, payload, now, source_fetched_at=now)
        assert result["status"] in {"success", "empty"}, result
    draft = await db.scalar(select(CatalogDraft).where(CatalogDraft.state == "draft"))
    result = await catalog.publish_drafts(db, METU_ID, TERM, [draft.id], expected_release_id=None,
                                          idempotency_key=str(uuid4()), reason="Review source fixture", created_by=user)
    await db.commit()
    return result


async def test_import_publish_schedule_read_and_automatic_plan_share_release(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "academic_catalog_reads_enabled", True)
    user = new_user_id()
    headers = auth_header(user)
    await client.get("/api/v1/profile", headers=headers)
    async with SessionLocal() as db:
        release = await import_and_publish(db, user)
        db.add(StudentAcademicSnapshot(user_id=user, term=TERM, completed_courses=[], enrolled_courses=[],
                                       current_credits=30, current_grade_points=90, fetched_at=datetime.now(UTC), source="sais"))
        await db.commit()
    response = await client.get(f"/api/v1/schedule/courses/{COURSE}?department=240&semester={TERM}", headers=headers)
    assert response.status_code == 200, response.text
    course = response.json()
    assert course["data"]["_catalog"]["release_id"] == release["release_id"]
    assert course["sections"][0]["meetings"][0]["start_minute"] == 540
    assert course["data"]["local_credits"] == 3
    assert course["data"]["ects"] == 5

    async def local_snapshot(db, user_id, term):
        snapshot = await db.get(StudentAcademicSnapshot, (user_id, term))
        return snapshot, {"fresh": True, "status": "fresh", "fetched_at": snapshot.fetched_at.isoformat()}

    monkeypatch.setattr(planning, "prepare_planning_snapshot", local_snapshot)
    async with SessionLocal() as db:
        result = await planning.plan_semester(db, user, planning.SemesterPlanRequest(
            term=TERM, required_courses=[COURSE], min_credits=3, max_credits=6,
        ))
    assert result["status"] == "ok", json.dumps(result, indent=2, default=str)
    assert result["courses"][0]["course_code"] == COURSE
    assert result["selected_credits"] == 3
    assert result["provenance"]["catalog_release_id"] == release["release_id"]


async def test_partial_publication_carries_courses_and_rollback_rebases_next_import(client):
    user = new_user_id()
    await client.get("/api/v1/profile", headers=auth_header(user))
    async with SessionLocal() as db:
        first = await import_and_publish(db, user)
        second_draft = await catalog.create_draft(db, METU_ID, TERM, "2402202", data={"title": "Second course"},
                                                  reason="Add another reviewed course", created_by=user)
        second = await catalog.publish_drafts(db, METU_ID, TERM, [second_draft.id],
            expected_release_id=UUID(first["release_id"]), idempotency_key=str(uuid4()),
            reason="Selected course publication", created_by=user)
        await db.commit()
        items = (await db.scalars(select(CatalogReleaseItem).where(
            CatalogReleaseItem.release_id == UUID(second["release_id"]),
        ))).all()
        assert {item.course_code for item in items} == {COURSE, "2402202"}
        original = next(item for item in items if item.course_code == COURSE)
        original_revision = original.course_revision_id
        changed = await catalog.create_draft(db, METU_ID, TERM, COURSE, base_revision_id=original_revision,
                                               reason="Revise an existing course", created_by=user)
        await catalog.patch_draft(db, METU_ID, changed.id, expected_revision=changed.revision,
                                  patch={"title": "Title from a later revision"}, reason="Review corrected title")
        third = await catalog.publish_drafts(db, METU_ID, TERM, [changed.id],
            expected_release_id=UUID(second["release_id"]), idempotency_key=str(uuid4()),
            reason="Publish later course revision", created_by=user)
        await db.commit()
        rollback = await catalog.rollback_release(db, METU_ID, TERM, UUID(first["release_id"]),
            expected_release_id=UUID(third["release_id"]), idempotency_key=str(uuid4()),
            reason="Restore the prior course set", created_by=user)
        await db.commit()
        assert rollback["release_id"] not in {first["release_id"], second["release_id"], third["release_id"]}
        pointer = await db.scalar(select(CatalogTermActiveRelease))
        assert str(pointer.release_id) == rollback["release_id"]
        now = datetime.now(UTC)
        observation = await catalog.ingest_observation(db, METU_ID, "get_course_info",
            {"semester": TERM, "department": "240", "course": COURSE},
            {"course_code": COURSE, "course_name": "Updated source title"}, now, source_fetched_at=now)
        await db.commit()
        draft = await db.get(CatalogDraft, UUID(observation["draft_ids"][0]))
        assert draft.base_revision_id == original_revision
        assert draft.data["local_credits"] == 3
        assert draft.data["title"] == "Updated source title"


async def test_verified_empty_source_tables_clear_old_data_without_erasing_other_components(client):
    user = new_user_id()
    await client.get("/api/v1/profile", headers=auth_header(user))
    async with SessionLocal() as db:
        await import_and_publish(db, user)
        now = datetime.now(UTC)
        arguments = {"semester": TERM, "department": "240", "course": COURSE, "section": "1"}
        for restrictions in ([{"given_dept": "HIST", "min_cgpa": "2.50"}], []):
            result = await catalog.ingest_observation(db, METU_ID, "get_section_constraints", arguments,
                {"constraints": restrictions}, now, source_fetched_at=now)
            await db.commit()
        draft = await db.get(CatalogDraft, UUID(result["draft_ids"][0]))
        assert draft.data["sections"][0]["restrictions"] == []
        assert len(draft.data["sections"][0]["meetings"]) == 1
        await catalog.ingest_observation(db, METU_ID, "get_course_info",
            {"semester": TERM, "department": "240", "course": COURSE},
            {"course_code": COURSE, "course_name": "Fixture Course", "sections": []}, now, source_fetched_at=now)
        await db.commit()
        await db.refresh(draft)
        assert draft.data["sections"] == []

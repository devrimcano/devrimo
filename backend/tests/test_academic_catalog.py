"""Focused invariants for the reviewed academic catalog service."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.academic_catalog import service
from app.academic_catalog.models import (
    CatalogCourseRevision,
    CatalogDraft,
    CatalogReleaseItem,
    CatalogSourceObservation,
)
from app.academic_catalog.schemas import DraftCreateIn, DraftPatchIn, MeetingPatch
from app.admin.directory import METU_ID, ensure_metu
from app.db.models import AccountDirectory, AccountStatus
from app.db.session import SessionLocal


TERM = "20261"
COURSE = "2402201"


async def _user() -> tuple:
    user_id = uuid4()
    async with SessionLocal() as db:
        await ensure_metu(db)
        db.add(AccountDirectory(user_id=user_id, organization_id=METU_ID, status=AccountStatus.active))
        await db.commit()
    return user_id


def _source_status(*, fetched: datetime | None = None, verified: bool = True) -> dict:
    now = fetched or datetime.now(UTC)
    return {
        component: {
            "component": component,
            "source_status": "success",
            "verified": verified,
            "fresh": fetched is not None,
            "observed_at": now.isoformat(),
            "source_fetched_at": fetched.isoformat() if fetched else None,
        }
        for component in (
            "listing",
            "details",
            "sections",
            "constraints",
            "prerequisites",
            "replacements",
        )
    }


async def test_source_override_survives_refresh_and_records_conflict():
    async with SessionLocal() as db:
        await ensure_metu(db)
        first = await service.ingest_observation(
            db,
            METU_ID,
            "get_course_info",
            {"semester": TERM, "department": "240", "course": COURSE},
            {"course_code": COURSE, "title": "Source title"},
            datetime.now(UTC),
            source_fetched_at=datetime.now(UTC),
        )
        await db.commit()
        draft = await db.get(CatalogDraft, first["draft_id"])
        assert draft is not None
        await service.patch_draft(
            db,
            METU_ID,
            draft.id,
            expected_revision=draft.revision,
            patch={"title": "Reviewed title"},
            reason="Registrar record checked",
        )
        await db.commit()
        draft = await db.get(CatalogDraft, draft.id)
        second = await service.ingest_observation(
            db,
            METU_ID,
            "get_course_info",
            {"semester": TERM, "department": "240", "course": COURSE},
            {"course_code": COURSE, "title": "New source title"},
            datetime.now(UTC),
            source_fetched_at=datetime.now(UTC),
        )
        await db.commit()
        assert second["draft_id"] == str(draft.id)
        draft = await db.get(CatalogDraft, draft.id)
        assert draft.data["title"] == "Reviewed title"
        assert any(issue["code"] == "source_conflict" for issue in draft.issues)
        assert await db.scalar(
            select(CatalogSourceObservation).where(
                CatalogSourceObservation.organization_id == METU_ID,
                CatalogSourceObservation.course_code == COURSE,
            )
        )


async def test_draft_expected_revision_is_optimistic():
    async with SessionLocal() as db:
        await ensure_metu(db)
        draft = await service.create_draft(
            db,
            METU_ID,
            TERM,
            COURSE,
            data={"title": "Initial"},
            reason="Create test draft",
        )
        await db.commit()
        draft_id = draft.id
        revision = draft.revision
    async with SessionLocal() as first, SessionLocal() as second:
        await service.patch_draft(
            first,
            METU_ID,
            draft_id,
            expected_revision=revision,
            patch={"title": "First writer"},
            reason="First writer update",
        )
        await first.commit()
        with pytest.raises(service.CatalogConflict):
            await service.patch_draft(
                second,
                METU_ID,
                draft_id,
                expected_revision=revision,
                patch={"title": "Stale writer"},
                reason="Stale writer update",
            )


async def test_published_read_requires_active_account():
    async with SessionLocal() as db:
        with pytest.raises(Exception) as caught:
            await service.read_tool(db, uuid4(), "list_program_courses", {"semester": TERM})
        assert getattr(caught.value, "status_code", None) == 403


def test_component_freshness_expires_by_component_window():
    old = datetime.now(UTC) - timedelta(days=8)
    status = {
        "component": "sections",
        "source_status": "success",
        "verified": True,
        "fresh": True,
        "source_fetched_at": old.isoformat(),
    }
    effective = service._effective_component_status(status, component="sections")
    assert effective["fresh"] is False
    assert effective["stale_reason"] == "source_fetch_expired"


def test_meeting_patch_rejects_scheduled_without_time():
    with pytest.raises(ValueError):
        MeetingPatch(status="scheduled", weekday=0)


def test_admin_draft_input_rejects_fabricated_provenance_and_invalid_meetings():
    with pytest.raises(ValueError, match="derived"):
        DraftCreateIn(
            term=TERM,
            course_code=COURSE,
            data={"title": "Client claim", "component_status": {"details": {"fresh": True}}},
        ).typed_data()
    with pytest.raises(ValueError, match="derived"):
        DraftPatchIn(
            expected_revision=1,
            patch={"sections": [{"section_code": "1", "restrictions_source_fetched_at": "2026-09-09T00:00:00Z"}]},
            reason="Reject fake evidence",
        ).typed_patch()
    with pytest.raises(ValueError, match="weekday"):
        DraftPatchIn(
            expected_revision=1,
            patch={"sections": [{"section_code": "1", "meetings": [{"status": "scheduled", "start_minute": 540, "end_minute": 600}]}]},
            reason="Reject invalid meeting",
        ).typed_patch()


def test_admin_draft_input_accepts_typed_courses_tab_rows_and_nulls():
    typed = DraftPatchIn(
        expected_revision=1,
        patch={
            "title": "Reviewed course",
            "local_credits": 3,
            "ects": None,
            "sections": [{
                "section_code": "1",
                "meetings_status": "untimed",
                "meetings": [],
                "restrictions": [{
                    "kind": "department",
                    "given_department": "240",
                    "verified": True,
                    "status": "verified",
                }],
            }],
            "prerequisite_groups": [{
                "group_no": 1,
                "logic": "and",
                "verified": True,
                "requirements": [{"course_code": COURSE}],
            }],
            "replacements": [{
                "relationship_type": "replacement",
                "related_course_code": "2402202",
                "verified": True,
            }],
        },
        reason="Accept typed Courses tab fixture",
    ).typed_patch()
    assert typed["sections"][0]["meetings_status"] == "untimed"
    assert typed["sections"][0]["restrictions"][0]["verified"] is True
    assert typed["prerequisite_groups"][0]["logic"] == "AND"
    assert typed["replacements"][0]["related_course_code"] == "2402202"
    assert typed["ects"] is None


def test_section_parser_preserves_explicit_untimed_and_blocks_contradictory_times():
    rows, present = service._section_rows(
        {
            "sections": [
                {"section": "1", "meetings_status": "explicitly_untimed", "meetings": []},
                {"section": "2", "meetings": []},
                {
                    "section": "3",
                    "meetings_status": "untimed",
                    "meetings": [{"day": "Monday", "start": "bad", "end": "also bad"}],
                },
            ]
        }
    )
    assert present is True
    by_code = {row["section_code"]: row for row in rows}
    assert by_code["1"]["meetings_status"] == "untimed"
    assert by_code["1"]["meetings"] == []
    # Empty schedule data without a positive source assertion remains unknown.
    assert by_code["2"]["meetings_status"] == "unknown"
    # A contradictory explicit marker cannot hide malformed meeting times.
    assert by_code["3"]["meetings_status"] == "invalid"


def test_prerequisite_parser_rejects_mixed_valid_and_invalid_rows_in_one_group():
    assert service._prerequisite_groups({"prerequisites": []}) == ([], True)
    groups, valid = service._prerequisite_groups(
        {
            "prerequisites": [
                {"group_no": 1, "course_code": "2402201"},
                {"group_no": 1, "course_code": "not-a-course"},
            ]
        }
    )
    assert groups[0]["requirements"][0]["course_code"] == "2402201"
    assert valid is False


def test_prerequisite_parser_rejects_invalid_nested_requirement_beside_valid_one():
    groups, valid = service._prerequisite_groups(
        {
            "groups": [
                {
                    "group_no": 1,
                    "requirements": [
                        {"course_code": "2402201"},
                        {"course_code": "240"},
                    ],
                }
            ]
        }
    )
    assert groups[0]["requirements"][0]["course_code"] == "2402201"
    assert valid is False


def test_malformed_prerequisite_observation_does_not_create_verified_candidate():
    candidate, issues, status_value = service._parse_observation(
        "get_course_prerequisites",
        {"semester": TERM, "department": "240", "course": COURSE},
        {"prerequisite_groups": [{"course_code": COURSE}, {"course_code": "bad"}]},
    )
    assert candidate == {}
    assert status_value == "malformed"
    assert any(issue["code"] == "missing_prerequisites_table" for issue in issues)


def test_explicit_verification_expires_by_component_window():
    verified_at = datetime.now(UTC) - timedelta(days=8)
    status = {
        "component": "details",
        "source_status": "admin_verified",
        "verified": True,
        "fresh": True,
        "verification_evidence": "Registrar bulletin 2026-01",
        "verified_at": verified_at.isoformat(),
    }
    effective = service._effective_component_status(status, component="details")
    assert effective["fresh"] is False
    assert effective["stale_reason"] == "verification_expired"


async def test_detail_sections_do_not_certify_unfetched_section_constraints():
    async with SessionLocal() as db:
        await ensure_metu(db)
        now = datetime.now(UTC)
        values = {"semester": TERM, "department": "240", "course": COURSE}
        result = await service.ingest_observation(
            db,
            METU_ID,
            "get_course_info",
            values,
            {
                "course_code": COURSE,
                "title": "Two Sections",
                "sections": [
                    {"section": "1", "schedule": []},
                    {"section": "2", "schedule": []},
                ],
            },
            now,
            source_fetched_at=now,
        )
        await service.ingest_observation(
            db,
            METU_ID,
            "get_section_constraints",
            {**values, "section": "1"},
            {"course_code": COURSE, "section": "1", "constraints": []},
            now,
            source_fetched_at=now,
        )
        draft = await db.get(CatalogDraft, result["draft_id"])
        assert draft is not None
        sections = {row["section_code"]: row for row in draft.data["sections"]}
        assert draft.data["component_status"]["sections"]["verified"] is True
        assert draft.data["component_status"]["constraints"]["verified"] is True
        assert sections["1"]["restrictions_status"] == "verified"
        assert sections["2"].get("restrictions_status", "unknown") == "unknown"


async def test_real_source_components_publish_as_verified_plan_inputs():
    user = await _user()
    async with SessionLocal() as db:
        now = datetime.now(UTC)
        values = {"semester": TERM, "department": "240", "course": COURSE}
        observations = [
            (
                "list_program_courses",
                {"semester": TERM, "department": "240"},
                [{"course_code": COURSE, "name": "Source Course", "credit": 3, "ects": 5}],
            ),
            (
                "get_course_info",
                values,
                {
                    "course_code": COURSE,
                    "title": "Source Course",
                    "local_credits": 3,
                    "ects": 5,
                    "sections": [{"section": "1", "schedule": []}],
                },
            ),
            (
                "get_section_constraints",
                {**values, "section": "1"},
                {"course_code": COURSE, "section": "1", "constraints": []},
            ),
            ("get_course_prerequisites", values, []),
            ("get_course_replacements", values, []),
        ]
        for tool, arguments, payload in observations:
            result = await service.ingest_observation(
                db,
                METU_ID,
                tool,
                arguments,
                payload,
                now,
                source_fetched_at=now,
            )
            assert result["status"] in {"success", "empty"}, result
        draft = await db.scalar(select(CatalogDraft).where(CatalogDraft.state == "draft"))
        assert draft is not None
        publication = await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [draft.id],
            expected_release_id=None,
            idempotency_key=str(uuid4()),
            reason="Publish verified source fixture",
            created_by=user,
        )
        await db.commit()
        offerings, rules, metadata = await service.published_plan_inputs(db, user, TERM)
        assert metadata["catalog_release_id"] == publication["release_id"]
        assert offerings[0]["course_code"] == COURSE
        assert offerings[0]["eligible"] is True
        assert offerings[0]["data_status"] == "fresh"
        assert rules[COURSE]["data_status"] == "verified"


async def test_rollback_rebases_new_source_draft_on_active_target_revision():
    async with SessionLocal() as db:
        await ensure_metu(db)
        first_draft = await service.create_draft(
            db,
            METU_ID,
            TERM,
            COURSE,
            data={"title": "Version one"},
            reason="Initial catalog revision",
        )
        first_release = await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [first_draft.id],
            expected_release_id=None,
            idempotency_key=str(uuid4()),
            reason="Initial release",
        )
        source = await service.ingest_observation(
            db,
            METU_ID,
            "get_course_info",
            {"semester": TERM, "department": "240", "course": COURSE},
            {"course_code": COURSE, "title": "Version two"},
            datetime.now(UTC),
            source_fetched_at=datetime.now(UTC),
        )
        second_draft = await db.get(CatalogDraft, source["draft_id"])
        assert second_draft is not None
        second_release = await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [second_draft.id],
            expected_release_id=UUID(first_release["release_id"]),
            idempotency_key=str(uuid4()),
            reason="Second release",
        )
        rollback = await service.rollback_release(
            db,
            METU_ID,
            TERM,
            UUID(first_release["release_id"]),
            expected_release_id=UUID(second_release["release_id"]),
            idempotency_key=str(uuid4()),
            reason="Restore first release",
        )
        assert rollback["target_release_id"] == first_release["release_id"]
        fresh_source = await service.ingest_observation(
            db,
            METU_ID,
            "get_course_info",
            {"semester": TERM, "department": "240", "course": COURSE},
            {"course_code": COURSE, "title": "Version three"},
            datetime.now(UTC),
            source_fetched_at=datetime.now(UTC),
        )
        rebased = await db.get(CatalogDraft, fresh_source["draft_id"])
        assert rebased is not None
        assert rebased.base_revision_id == UUID(first_release["course_revision_ids"][0])
        assert rebased.data["title"] == "Version three"


async def test_selected_publication_carries_forward_other_active_release_items():
    async with SessionLocal() as db:
        await ensure_metu(db)
        first = await service.create_draft(db, METU_ID, TERM, COURSE, data={"title": "A"}, reason="A")
        second = await service.create_draft(db, METU_ID, TERM, "2402202", data={"title": "B"}, reason="B")
        initial = await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [first.id, second.id],
            expected_release_id=None,
            idempotency_key=str(uuid4()),
            reason="Initial two-course release",
        )
        updated = await service.ingest_observation(
            db,
            METU_ID,
            "get_course_info",
            {"semester": TERM, "department": "240", "course": COURSE},
            {"course_code": COURSE, "title": "A updated"},
            datetime.now(UTC),
            source_fetched_at=datetime.now(UTC),
        )
        updated_draft = await db.get(CatalogDraft, updated["draft_id"])
        assert updated_draft is not None
        release = await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [updated_draft.id],
            expected_release_id=UUID(initial["release_id"]),
            idempotency_key=str(uuid4()),
            reason="Update one course",
        )
        items = (
            await db.scalars(select(CatalogReleaseItem).where(CatalogReleaseItem.release_id == UUID(release["release_id"])))
        ).all()
        by_code = {item.course_code: item.course_revision_id for item in items}
        old_items = (
            await db.scalars(select(CatalogReleaseItem).where(CatalogReleaseItem.release_id == UUID(initial["release_id"])))
        ).all()
        old_by_code = {item.course_code: item.course_revision_id for item in old_items}
        assert by_code["2402201"] != old_by_code["2402201"]
        assert by_code["2402202"] == old_by_code["2402202"]


async def test_publication_idempotency_rejects_changed_request_payload():
    async with SessionLocal() as db:
        await ensure_metu(db)
        draft = await service.create_draft(db, METU_ID, TERM, COURSE, data={"title": "Idempotent"}, reason="Create")
        request_key = str(uuid4())
        first = await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [draft.id],
            expected_release_id=None,
            idempotency_key=request_key,
            reason="Original request",
        )
        replay = await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [draft.id],
            expected_release_id=None,
            idempotency_key=request_key,
            reason="Original request",
        )
        assert replay == first
        with pytest.raises(service.CatalogConflict):
            await service.publish_drafts(
                db,
                METU_ID,
                TERM,
                [draft.id],
                expected_release_id=None,
                idempotency_key=request_key,
                reason="Changed request",
            )


async def test_missing_local_credits_keeps_published_offering_incomplete():
    user = await _user()
    now = datetime.now(UTC)
    component_status = _source_status(fetched=now)
    data = {
        "title": "Creditless Course",
        "sections": [{
            "section_code": "1",
            "meetings": [],
            "restrictions": [],
            "restrictions_status": "verified",
            "restrictions_observed_at": now.isoformat(),
            "restrictions_source_fetched_at": now.isoformat(),
        }],
        "prerequisite_groups": [],
        "replacements": [],
        "component_status": component_status,
    }
    async with SessionLocal() as db:
        draft = await service.create_draft(db, METU_ID, TERM, COURSE, data=data, reason="Missing credit fixture")
        release = await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [draft.id],
            expected_release_id=None,
            idempotency_key=str(uuid4()),
            reason="Publish incomplete credit fixture",
            created_by=user,
        )
        await db.commit()
        offerings, _, _ = await service.published_plan_inputs(db, user, TERM)
        assert release["release_id"] == offerings[0]["catalog_release_id"]
        assert offerings[0]["credits"] is None
        assert offerings[0]["complete"] is False


async def test_explicit_closed_availability_is_browsable_but_not_selectable():
    user = await _user()
    now = datetime.now(UTC)
    data = {
        "title": "Closed Course",
        "local_credits": 3,
        "availability": "Kapalı Ders",
        "sections": [{
            "section_code": "1",
            "meetings": [],
            "restrictions": [],
            "restrictions_status": "verified",
            "restrictions_observed_at": now.isoformat(),
            "restrictions_source_fetched_at": now.isoformat(),
        }],
        "prerequisite_groups": [],
        "replacements": [],
        "component_status": _source_status(fetched=now),
    }
    async with SessionLocal() as db:
        await service.create_draft(db, METU_ID, TERM, COURSE, data=data, reason="Closed availability fixture")
        draft = await db.scalar(select(CatalogDraft).where(CatalogDraft.state == "draft"))
        assert draft is not None
        await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [draft.id],
            expected_release_id=None,
            idempotency_key=str(uuid4()),
            reason="Publish closed availability fixture",
            created_by=user,
        )
        await db.commit()
        offerings, _, _ = await service.published_plan_inputs(db, user, TERM)
        assert offerings[0]["eligible"] is False
        assert offerings[0]["data_status"] == "unavailable"


async def test_explicit_untimed_section_is_publishable_and_credit_eligible():
    user = await _user()
    now = datetime.now(UTC)
    data = {
        "title": "Untimed Thesis",
        "local_credits": 3,
        "sections": [{
            "section_code": "1",
            "meetings_status": "explicitly_untimed",
            "meetings": [],
            "restrictions": [],
            "restrictions_status": "verified",
            "restrictions_observed_at": now.isoformat(),
            "restrictions_source_fetched_at": now.isoformat(),
        }],
        "prerequisite_groups": [],
        "replacements": [],
        "component_status": _source_status(fetched=now),
    }
    async with SessionLocal() as db:
        draft = await service.create_draft(db, METU_ID, TERM, COURSE, data=data, reason="Untimed section fixture")
        await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [draft.id],
            expected_release_id=None,
            idempotency_key=str(uuid4()),
            reason="Publish explicit untimed fixture",
            created_by=user,
        )
        await db.commit()
        offerings, _, _ = await service.published_plan_inputs(db, user, TERM)
        assert offerings[0]["meetings_status"] == "untimed"
        assert offerings[0]["meetings"] == []
        assert offerings[0]["eligible"] is True
        assert offerings[0]["complete"] is True


async def test_long_restriction_labels_and_syllabus_flag_survive_publish():
    user = await _user()
    now = datetime.now(UTC)
    data = {
        "title": "Long Restriction Labels",
        "local_credits": 3,
        "sections": [{
            "section_code": "1",
            "syllabus_available": False,
            "meetings": [],
            "restrictions_status": "verified",
            "restrictions_observed_at": now.isoformat(),
            "restrictions_source_fetched_at": now.isoformat(),
            "restrictions": [{
                "kind": "surname",
                "start_grade": "Herkes alabilir",
                "end_grade": "Herkes alabilir",
                "status": "verified",
            }],
        }],
        "prerequisite_groups": [],
        "replacements": [],
        "component_status": _source_status(fetched=now),
    }
    async with SessionLocal() as db:
        draft = await service.create_draft(db, METU_ID, TERM, COURSE, data=data, reason="Long label fixture")
        release = await service.publish_drafts(
            db,
            METU_ID,
            TERM,
            [draft.id],
            expected_release_id=None,
            idempotency_key=str(uuid4()),
            reason="Publish long label fixture",
            created_by=user,
        )
        await db.commit()
        offerings, _, _ = await service.published_plan_inputs(db, user, TERM)
        section = offerings[0]
        assert section["syllabus_available"] is False
        assert section["restrictions"][0]["start_grade"] == "Herkes alabilir"
        assert section["catalog_release_id"] == release["release_id"]


__all__ = []

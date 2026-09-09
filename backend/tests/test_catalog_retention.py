from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.academic_catalog.models import CatalogSourceObservation
from app.academic_catalog.retention import sweep_failed_observations
from app.academic_catalog.service import create_draft
from app.admin.directory import METU_ID, ensure_metu
from app.db.session import SessionLocal


async def test_retention_removes_only_old_superseded_unreferenced_success():
    now = datetime.now(UTC)
    old = now - timedelta(days=100)

    def observation(course, when):
        return CatalogSourceObservation(
            id=uuid4(), organization_id=METU_ID, tool="get_course_info",
            arguments={"course": course, "semester": "20261"}, term="20261",
            course_code=course, payload={"title": "Fixture"}, payload_hash="fixture",
            observed_at=when, created_at=when, status="success",
        )

    superseded = observation("2402201", old)
    latest = observation("2402201", now)
    referenced = observation("2402202", old)
    referenced_newer = observation("2402202", now)
    only_answer = observation("2402203", old)
    recent = observation("2402201", now - timedelta(days=2))
    async with SessionLocal() as db:
        await ensure_metu(db)
        db.add_all([superseded, latest, referenced, referenced_newer, only_answer, recent])
        await db.flush()
        draft = await create_draft(db, METU_ID, "20261", "2402202", reason="Preserve evidence")
        draft.data = {**draft.data, "_source_observation_ids": [str(referenced.id)]}
        await db.commit()

    assert await sweep_failed_observations() == 1
    async with SessionLocal() as db:
        ids = set((await db.scalars(select(CatalogSourceObservation.id))).all())
        assert superseded.id not in ids
        assert {latest.id, referenced.id, referenced_newer.id, only_answer.id, recent.id} <= ids


async def test_retention_preserves_lost_listing_lineage_for_term_draft():
    now = datetime.now(UTC)
    old = now - timedelta(days=100)
    arguments = {"department": "567", "semester": "20261"}
    listing = CatalogSourceObservation(
        id=uuid4(), organization_id=METU_ID, tool="list_program_courses",
        arguments=arguments, term="20261", department="567", course_code=None,
        payload={"courses": [{"course_code": "2402201"}]}, payload_hash="listing-old",
        observed_at=old, created_at=old, status="success",
    )
    newer_empty = CatalogSourceObservation(
        id=uuid4(), organization_id=METU_ID, tool="list_program_courses",
        arguments=arguments, term="20261", department="567", course_code=None,
        payload={"courses": []}, payload_hash="listing-empty",
        observed_at=now, created_at=now, status="empty",
    )
    async with SessionLocal() as db:
        await ensure_metu(db)
        db.add_all([listing, newer_empty])
        await db.flush()
        # Model the service's bounded provenance after the old listing id has
        # fallen out of the draft's newest-100 observation window.
        await create_draft(
            db,
            METU_ID,
            "20261",
            "2402201",
            data={"_source_observation_ids": [str(uuid4()) for _ in range(100)]},
            reason="Preserve listing lineage",
        )
        await db.commit()

    assert await sweep_failed_observations() == 0
    async with SessionLocal() as db:
        ids = set((await db.scalars(select(CatalogSourceObservation.id))).all())
        assert {listing.id, newer_empty.id} <= ids


async def test_retention_preserves_lost_course_lineage_for_matching_draft():
    now = datetime.now(UTC)
    old = now - timedelta(days=100)
    arguments = {"department": "240", "semester": "20261", "course": "2402201"}
    detail = CatalogSourceObservation(
        id=uuid4(), organization_id=METU_ID, tool="get_course_info",
        arguments=arguments, term="20261", department="240", course_code="2402201",
        payload={"course_code": "2402201", "title": "Fixture"}, payload_hash="detail-old",
        observed_at=old, created_at=old, status="success",
    )
    newer_empty = CatalogSourceObservation(
        id=uuid4(), organization_id=METU_ID, tool="get_course_info",
        arguments=arguments, term="20261", department="240", course_code="2402201",
        payload={}, payload_hash="detail-empty",
        observed_at=now, created_at=now, status="empty",
    )
    async with SessionLocal() as db:
        await ensure_metu(db)
        db.add_all([detail, newer_empty])
        await db.flush()
        await create_draft(
            db,
            METU_ID,
            "20261",
            "2402201",
            data={"_source_observation_ids": [str(uuid4()) for _ in range(100)]},
            reason="Preserve course lineage",
        )
        await db.commit()

    assert await sweep_failed_observations() == 0
    async with SessionLocal() as db:
        ids = set((await db.scalars(select(CatalogSourceObservation.id))).all())
        assert {detail.id, newer_empty.id} <= ids

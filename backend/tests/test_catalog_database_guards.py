from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.academic_catalog.models import (
    CatalogCourse, CatalogCourseRevision, CatalogMeeting, CatalogRelease,
    CatalogReleaseItem, CatalogSection, CatalogTerm,
)
from app.admin.directory import METU_ID, ensure_metu
from app.db.models import Organization
from app.db.session import SessionLocal


async def _published(db):
    await ensure_metu(db)
    term = CatalogTerm(organization_id=METU_ID, term_code="20261")
    course = CatalogCourse(organization_id=METU_ID, course_code="2402201", department="240")
    db.add_all([term, course])
    await db.flush()
    revision = CatalogCourseRevision(organization_id=METU_ID, course_id=course.id, term_id=term.id,
                                     revision=1, state="draft", title="History")
    db.add(revision)
    await db.flush()
    section = CatalogSection(organization_id=METU_ID, course_revision_id=revision.id, section_code="1")
    db.add(section)
    await db.flush()
    meeting = CatalogMeeting(organization_id=METU_ID, section_id=section.id, weekday=0,
                             start_minute=600, end_minute=650, status="scheduled")
    db.add(meeting)
    await db.flush()
    revision.state = "published"
    await db.flush()
    release = CatalogRelease(organization_id=METU_ID, term_id=term.id, release_number=1,
                             reason="fixture publication", idempotency_key="test-release-key")
    db.add(release)
    await db.flush()
    db.add(CatalogReleaseItem(organization_id=METU_ID, release_id=release.id, course_id=course.id,
                              course_revision_id=revision.id, course_code=course.course_code))
    await db.commit()
    return term, course, revision, section, meeting, release


async def test_published_children_and_release_membership_are_immutable():
    async with SessionLocal() as db:
        _, course, revision, section, meeting, release = await _published(db)
        statements = [
            ("UPDATE catalog_meetings SET start_minute=601 WHERE id=:id", meeting.id),
            ("DELETE FROM catalog_sections WHERE id=:id", section.id),
            ("UPDATE catalog_course_revisions SET title='changed' WHERE id=:id", revision.id),
            ("DELETE FROM catalog_release_items WHERE release_id=:id", release.id),
        ]
        for sql, row_id in statements:
            with pytest.raises(DBAPIError):
                async with db.begin_nested():
                    await db.execute(text(sql), {"id": row_id})
        with pytest.raises(DBAPIError, match="sealed|duplicate"):
            async with db.begin_nested():
                await db.execute(text("""INSERT INTO catalog_release_items
                    (organization_id,release_id,course_id,course_revision_id,course_code)
                    VALUES (:org,:release,:course,:revision,'2402201')"""), {
                    "org": METU_ID, "release": release.id, "course": course.id, "revision": revision.id,
                })


async def test_cross_organization_parent_and_worker_publication_are_rejected():
    async with SessionLocal() as db:
        _, _, revision, _, _, _ = await _published(db)
        other = Organization(id=uuid4(), slug="other-campus", name="Other")
        db.add(other)
        await db.commit()
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                db.add(CatalogSection(organization_id=other.id, course_revision_id=revision.id, section_code="9"))
                await db.flush()
        with pytest.raises(DBAPIError, match="permission denied"):
            async with db.begin_nested():
                await db.execute(text("SET LOCAL ROLE devrimo_catalog"))
                await db.execute(text("DELETE FROM catalog_releases WHERE false"))

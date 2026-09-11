"""A component read cleanly stops being a failing component.

`draft.issues` only ever grew. Publication refuses any draft carrying a
blocking issue, so one bad read blocked a course for good - re-reading the page
successfully changed nothing, because the stale error was still attached.

Measured on production: 513 courses stuck behind a `missing_prerequisites_table`
left by a parser that misread METU's `position` field. Re-ingesting their stored
payloads with the fixed parser restored the prerequisite groups - MATH 219 went
from none to four - and all 513 stayed blocked.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.academic_catalog import service as catalog_service
from app.academic_catalog.models import CatalogCourse, CatalogDraft
from app.admin.directory import METU_ID, ensure_metu
from app.db.session import SessionLocal

TERM = "20261"
COURSE = "2360219"
ARGS = {"semester": TERM, "department": "236", "course": COURSE}

GOOD_ROW = {
    "program_code": "1",
    "dept_version": "0",
    "prerequisite_course_code": "2360120",
    "name": "CALCULUS OF FUNCTIONS OF SEVERAL VARIABLES",
    "set_no": "1",
    "min_grade": "DD",
    "position": "Offered Course / Açık Ders",
}


async def _draft() -> CatalogDraft:
    async with SessionLocal() as db:
        course = (await db.scalars(
            select(CatalogCourse).where(
                CatalogCourse.organization_id == METU_ID, CatalogCourse.course_code == COURSE
            )
        )).first()
        assert course is not None
        return (await db.scalars(
            select(CatalogDraft).where(CatalogDraft.course_id == course.id)
        )).first()


def _blocking(draft: CatalogDraft) -> list[str]:
    return [
        issue.get("code")
        for issue in (draft.issues or [])
        if issue.get("severity", "error") == "error"
    ]


@pytest.fixture
async def _metu():
    async with SessionLocal() as db:
        await ensure_metu(db)
        await db.commit()


async def test_a_clean_read_clears_that_component_s_earlier_error(_metu):
    """The whole point: the second reading replaces the first one's verdict."""
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        # An unreadable page: no table and no explicit empty marker.
        await catalog_service.ingest_observation(
            db, METU_ID, "get_course_prerequisites", ARGS, {"unexpected": "shape"}, now,
            source_fetched_at=now, job_id=None,
        )
        await db.commit()
    assert "missing_prerequisites_table" in _blocking(await _draft())

    async with SessionLocal() as db:
        await catalog_service.ingest_observation(
            db, METU_ID, "get_course_prerequisites", ARGS, [GOOD_ROW], now,
            source_fetched_at=now, job_id=None,
        )
        await db.commit()

    draft = await _draft()
    assert "missing_prerequisites_table" not in _blocking(draft), (
        "the page reads cleanly now and the course is still blocked from publication"
    )
    assert draft.data.get("prerequisite_groups"), "the clean read stored no groups"


async def test_another_component_s_failure_is_not_cleared(_metu):
    """This reading is not evidence about a page it did not read."""
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        await catalog_service.ingest_observation(
            db, METU_ID, "get_course_replacements", ARGS, {"unexpected": "shape"}, now,
            source_fetched_at=now, job_id=None,
        )
        await catalog_service.ingest_observation(
            db, METU_ID, "get_course_prerequisites", ARGS, [GOOD_ROW], now,
            source_fetched_at=now, job_id=None,
        )
        await db.commit()

    assert "missing_replacements_table" in _blocking(await _draft())


async def test_a_read_that_fails_again_keeps_saying_so(_metu):
    """Clearing is for readings that succeeded, not for readings that happened."""
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        for _ in range(2):
            await catalog_service.ingest_observation(
                db, METU_ID, "get_course_prerequisites", ARGS, {"unexpected": "shape"}, now,
                source_fetched_at=now, job_id=None,
            )
        await db.commit()

    assert "missing_prerequisites_table" in _blocking(await _draft())

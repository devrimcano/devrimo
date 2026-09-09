"""Listing cost must not add database round trips for every course."""

from uuid import uuid4

from sqlalchemy import event

from app.academic_catalog.models import CatalogCourse, CatalogDraft, CatalogTerm
from app.academic_catalog.service import list_course_rows
from app.admin.directory import METU_ID, ensure_metu
from app.db.session import SessionLocal, engine


async def test_draft_listing_uses_bounded_queries_for_a_large_page():
    async with SessionLocal() as db:
        await ensure_metu(db)
        term = CatalogTerm(id=uuid4(), organization_id=METU_ID, term_code="20261")
        courses = [CatalogCourse(id=uuid4(), organization_id=METU_ID,
                                  course_code=f"240{number:04}", department="240") for number in range(100, 220)]
        db.add_all([term, *courses])
        await db.flush()
        db.add_all([CatalogDraft(organization_id=METU_ID, term_id=term.id, course_id=course.id,
                                 data={"title": f"Course {course.course_code}"}) for course in courses])
        await db.commit()
        queries = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            queries.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            page = await list_course_rows(db, METU_ID, "20261", limit=10, offset=30)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert page["total"] == 120
        assert len(page["courses"]) == 10
        assert page["courses"][0]["course_code"] == "2400130"
        assert len(queries) <= 8, f"Listing made {len(queries)} database calls for 120 courses"

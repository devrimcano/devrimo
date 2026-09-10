"""Finding a course by the name people call it.

The catalog stores 2400101 and department 240. Nobody says that. A student
says HIST 101, the timetable prints HIST 101, and the admin panel's own
department filter suggested typing "CNG" - into a field that compared what you
typed against "240". So the only way to find anything here was to already know
the seven digits.
"""

from uuid import uuid4

from app.academic_catalog.models import CatalogCourse, CatalogDraft, CatalogTerm
from app.academic_catalog.service import list_course_rows
from app.admin.directory import METU_ID, ensure_metu
from app.db.session import SessionLocal


async def _seed():
    """Two departments, so a department filter has something to exclude."""
    async with SessionLocal() as db:
        await ensure_metu(db)
        term = CatalogTerm(id=uuid4(), organization_id=METU_ID, term_code="20261")
        history = CatalogCourse(id=uuid4(), organization_id=METU_ID, course_code="2400101", department="240")
        maths = CatalogCourse(id=uuid4(), organization_id=METU_ID, course_code="2360219", department="236")
        db.add_all([term, history, maths])
        await db.flush()
        db.add_all([
            CatalogDraft(organization_id=METU_ID, term_id=term.id, course_id=history.id,
                         data={"title": "Principles of Kemal Atatürk I"}),
            CatalogDraft(organization_id=METU_ID, term_id=term.id, course_id=maths.id,
                         data={"title": "Introduction to Differential Equations"}),
        ])
        await db.commit()


async def _codes(**kwargs):
    async with SessionLocal() as db:
        page = await list_course_rows(db, METU_ID, "20261", **kwargs)
    return {row["course_code"] for row in page["courses"]}


async def test_a_course_is_found_by_the_code_a_student_says():
    await _seed()
    assert await _codes(query="hist 101") == {"2400101"}
    assert await _codes(query="HIST101") == {"2400101"}


async def test_an_abbreviation_alone_lists_that_department():
    await _seed()
    assert await _codes(query="hist") == {"2400101"}
    assert await _codes(query="math") == {"2360219"}


async def test_the_seven_digits_and_the_title_still_work():
    await _seed()
    assert await _codes(query="2360219") == {"2360219"}
    assert await _codes(query="differential") == {"2360219"}


async def test_the_department_filter_accepts_the_abbreviation_it_asks_for():
    await _seed()
    assert await _codes(department="HIST") == {"2400101"}
    assert await _codes(department="240") == {"2400101"}


async def test_every_row_carries_the_spelling_people_read():
    await _seed()
    async with SessionLocal() as db:
        page = await list_course_rows(db, METU_ID, "20261", query="hist")
    assert page["courses"][0]["display_code"] == "HIST 101"
    # The catalog's key is still there; it is what an import or a bug report
    # quotes, and it is the column every other table joins on.
    assert page["courses"][0]["course_code"] == "2400101"

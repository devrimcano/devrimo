"""Read-only projections with explicit public/private scope."""

from sqlalchemy import or_, select

from app.db.models import StudentAcademicSnapshot
from app.researchers.models import Researcher, ResearcherSection


def researcher_view(row):
    return {
        key: getattr(row, key)
        for key in ("id", "alias", "name", "title", "affiliation", "email", "source_url", "last_seen_at")
    }


async def search_researchers(db, query, limit):
    statement = select(Researcher)
    if query.strip():
        statement = statement.where(
            or_(
                Researcher.name.icontains(query, autoescape=True),
                Researcher.affiliation.icontains(query, autoescape=True),
            )
        )
    rows = (await db.scalars(statement.order_by(Researcher.name, Researcher.id).limit(limit))).all()
    return [researcher_view(row) for row in rows]


async def read_researcher(db, key):
    predicate = Researcher.id == int(key) if str(key).isdigit() else Researcher.alias == key
    row = await db.scalar(select(Researcher).where(predicate))
    if row is None:
        return {"status": "not_found"}
    sections = (await db.scalars(select(ResearcherSection).where(ResearcherSection.researcher_id == row.id))).all()
    return {
        **researcher_view(row),
        "sections": [
            {
                "section": item.section,
                "language": item.language,
                "content": item.content,
                "source_url": item.source_url,
                "fetched_at": item.fetched_at,
            }
            for item in sections
        ],
    }


async def academic_snapshot(db, user_id, term=None):
    query = select(StudentAcademicSnapshot).where(StudentAcademicSnapshot.user_id == user_id)
    if term:
        query = query.where(StudentAcademicSnapshot.term == term)
    row = await db.scalar(query.order_by(StudentAcademicSnapshot.fetched_at.desc()).limit(1))
    if row is None:
        return {"status": "not_synced"}
    return {
        key: getattr(row, key)
        for key in (
            "term",
            "completed_courses",
            "enrolled_courses",
            "current_credits",
            "current_grade_points",
            "fetched_at",
            "source",
        )
    }

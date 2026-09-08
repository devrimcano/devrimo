from uuid import uuid4

from app.api.v1 import schedule as schedule_api
from app.db.models import StudentContext


async def test_student_department_uses_setup_cache_without_catalog(monkeypatch):
    async def unexpected_catalog_call(*_args, **_kwargs):
        raise AssertionError("opening the planner must not contact SAIS for department data")

    monkeypatch.setattr(schedule_api, "call_course_info", unexpected_catalog_call)
    context = StudentContext(
        user_id=uuid4(),
        department="Electrical and Electronics Engineering",
        program_code="567",
        source="sais",
    )

    query, code = await schedule_api._student_department(None, context.user_id, context)

    assert query == "Electrical and Electronics Engineering"
    assert code == "567"


async def test_resolve_department_ignores_browser_override():
    context = StudentContext(
        user_id=uuid4(),
        department="Electrical and Electronics Engineering",
        program_code="567",
        source="sais",
    )

    class CachedContextDatabase:
        async def get(self, model, user_id):
            assert model is StudentContext
            assert user_id == context.user_id
            return context

    resolved = await schedule_api._resolve_department(
        CachedContextDatabase(), context.user_id, "571"
    )

    assert resolved == "567"

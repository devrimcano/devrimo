from uuid import uuid4

from app.api.v1 import schedule as schedule_api
from app.db.models import StudentContext


async def test_student_department_uses_setup_cache_without_catalog(monkeypatch):
    async def unexpected_catalog_call(*_args, **_kwargs):
        raise AssertionError("opening the planner must not contact SAIS for department data")

    monkeypatch.setattr(schedule_api, "resolve_department", unexpected_catalog_call)
    context = StudentContext(
        user_id=uuid4(),
        department="Electrical and Electronics Engineering",
        program_code="567",
        source="sais",
    )

    query, code = await schedule_api._student_department(None, context.user_id, context)

    assert query == "Electrical and Electronics Engineering"
    assert code == "567"

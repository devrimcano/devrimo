"""A failed campus tool is a failure, not an empty catalog.

Agno's MCP wrapper never raises when a server reports an error: it returns the
error text as the tool's own content. Read as a payload, "Error from MCP tool
'x': ..." is a department with no courses and a curriculum board with no
semesters — and for a shared tool it was then written into the catalog cache
for thirty days, so one student's momentary SAIS failure emptied a listing for
everybody. These tests pin the three places that has to be caught: the call,
the write, and the read of a row written before it was.
"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.campus import course_info, curriculum

USER = uuid.UUID("22222222-2222-2222-2222-222222222222")

# What agno hands back for a tool that raised inside the MCP server: its own
# prefix, wrapped around the repr of the content blocks the server returned.
MCP_ERROR = (
    "Error from MCP tool 'course_info_get_student_curriculum': "
    "[TextContent(type='text', text='Error executing tool get_student_curriculum: "
    "SAIS Student Information semester form was not found', annotations=None, meta=None)]"
)


@pytest.fixture(autouse=True)
def _empty_catalog():
    course_info._catalog.purge(lambda key: True)
    yield
    course_info._catalog.purge(lambda key: True)


def _toolkit(content):
    """A toolkit whose one function answers with an Agno-shaped result."""
    function = SimpleNamespace(
        entrypoint=lambda **kwargs: SimpleNamespace(content=content, metadata=None),
        parameters={"properties": {}, "required": []},
    )
    return SimpleNamespace(functions={"course_info_get_student_curriculum": function})


def test_the_server_sentence_is_what_survives():
    """The student's report has to name what SAIS actually said."""
    assert course_info.tool_error_text(MCP_ERROR) == (
        "Error executing tool get_student_curriculum: "
        "SAIS Student Information semester form was not found"
    )


@pytest.mark.parametrize(
    "content",
    [
        "MCP tool 'course_info_get_course_info' failed: connection closed.",
        "Error: timed out",
    ],
)
def test_every_shape_agno_returns_an_error_in_is_recognised(content):
    assert course_info.tool_error_text(content) is not None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"semesters": []},
        [{"course_code": "5670201"}],
        '{"courses": []}',
        # A course name is not an error just because it starts with a capital E.
        "Errors and Uncertainty in Measurement",
    ],
)
def test_an_answer_is_not_mistaken_for_a_failure(payload):
    assert course_info.tool_error_text(payload) is None


async def test_a_failed_tool_raises_instead_of_answering_with_its_error():
    with pytest.raises(HTTPException) as raised:
        await course_info._call_toolkit(None, USER, _toolkit(MCP_ERROR), "get_student_curriculum", {})
    assert raised.value.status_code == 502
    assert "semester form was not found" in raised.value.detail


async def test_a_failure_is_never_written_to_the_shared_cache(monkeypatch):
    """Thirty days of an empty department listing started here."""
    writes: list[str] = []
    monkeypatch.setattr(course_info, "require_catalog_access", AsyncMock())
    monkeypatch.setattr(course_info, "read_cached", AsyncMock(return_value=None))

    async def write_cached(key_hash, value, **kwargs):
        writes.append(key_hash)

    async def invoke(db, user_id, tool_suffix, values, session=None):
        raise HTTPException(502, "SAIS is unreachable")

    monkeypatch.setattr(course_info, "write_cached", write_cached)
    monkeypatch.setattr(course_info, "_invoke", invoke)

    with pytest.raises(HTTPException):
        await course_info.call_course_info(
            None, USER, "list_program_courses", {"department": "236", "semester": "20261"}
        )
    assert writes == []


async def test_a_poisoned_row_heals_on_the_next_read(monkeypatch):
    """Rows written before this check existed must not be served for weeks."""
    fresh = [{"course_code": "5670201", "name": "Circuit Theory"}]
    writes: list = []
    monkeypatch.setattr(course_info, "require_catalog_access", AsyncMock())
    monkeypatch.setattr(course_info, "read_cached", AsyncMock(return_value=MCP_ERROR))

    async def write_cached(key_hash, value, **kwargs):
        writes.append(value)

    async def invoke(db, user_id, tool_suffix, values, session=None):
        return fresh

    monkeypatch.setattr(course_info, "write_cached", write_cached)
    monkeypatch.setattr(course_info, "_invoke", invoke)

    answered = await course_info.call_course_info(
        None, USER, "list_program_courses", {"department": "236", "semester": "20261"}
    )
    assert answered == fresh
    assert writes == [fresh]


def test_an_unreadable_board_reports_what_was_received_instead():
    """The generic sentence is what hid every real cause from the planner."""
    with pytest.raises(ValueError) as raised:
        curriculum.next_semester_courses(MCP_ERROR)
    assert "SAIS Curriculum could not be read" in str(raised.value)
    assert "semester boxes" not in str(raised.value)


async def test_a_failed_read_is_reported_as_unavailable_not_as_an_empty_curriculum(monkeypatch):
    """The planner has to be able to tell the two apart; the student certainly can."""
    import app.api.v1.schedule as schedule

    @asynccontextmanager
    async def catalog_session(db, user_id):
        yield None

    async def call(db, user_id, tool, values, *, session=None):
        raise HTTPException(502, "SAIS Student Information semester form was not found")

    monkeypatch.setattr(schedule, "_resolve_department", AsyncMock(return_value="567"))
    monkeypatch.setattr(schedule, "_cached_plan", AsyncMock(return_value=None))
    monkeypatch.setattr(schedule, "catalog_session", catalog_session)
    monkeypatch.setattr(schedule, "call_course_info", call)

    response = await schedule.curriculum_plan(
        schedule.AiScheduleRequest(semester="20261"),
        user=SimpleNamespace(id=USER),
        db=SimpleNamespace(scalar=AsyncMock(return_value=None)),
    )
    assert response["courses"] == []
    assert response["curriculum_unavailable"] is True
    assert response["warnings"] == [
        "Your curriculum could not be read from METU: The METU response could not be verified."
    ]
    assert response["partial"] is False

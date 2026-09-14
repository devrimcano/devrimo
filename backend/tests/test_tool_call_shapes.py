"""Both tool-call shapes work, and the code a model writes is understood.

The live evaluation found the dominant tool failure was not the campus or the
catalog: it was the model writing a call the schema refused before any of our
code ran. It emits the flat shape and the nested one, sometimes in the same
conversation, so both are legal now and the test pins that down.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.agents.platform_tools import _scoped_ref, build_platform_tools
from app.agents.scholar import hooks
from app.workspace.resources import ResourceRef, SearchResource, normalize_term
from app.workspace.service import WorkspaceService


def test_a_term_label_becomes_the_code_the_catalog_answers_to():
    assert normalize_term("20261") == "20261"
    assert normalize_term("2026-2027 Fall") == "20261"
    assert normalize_term("Fall 2026") == "20261"
    assert normalize_term("2026 Güz") == "20261"
    assert normalize_term("Bahar 2026") == "20252"
    assert normalize_term("Summer 2026") == "20253"
    assert normalize_term("") is None
    assert normalize_term(None) is None
    # Anything unrecognised is handed through rather than guessed at.
    assert normalize_term("next semester") == "next semester"


def test_flat_fields_build_the_same_reference_as_the_nested_object():
    flat = _scoped_ref(ResourceRef, "read", None, "catalog.sections", key="CENG 331", term="2026-2027 Fall")
    nested = _scoped_ref(
        ResourceRef, "read", {"kind": "catalog.sections", "key": "CENG 331", "term": "2026-2027 Fall"}, None
    )
    assert flat == nested
    assert flat.term == "20261"

    search = _scoped_ref(SearchResource, "search", None, "researcher", department="CENG")
    assert search.kind == "researcher"
    assert search.department == "CENG"


def test_a_flat_call_without_a_kind_is_told_what_is_missing():
    with pytest.raises(HTTPException) as excinfo:
        _scoped_ref(ResourceRef, "read", None, None, key="CENG 331")
    assert excinfo.value.status_code == 422
    assert "kind" in str(excinfo.value.detail)


def test_the_generated_schema_accepts_both_shapes():
    tools = {tool.name: tool for tool in build_platform_tools(uuid4())}
    for name in ("read", "search", "update", "undo"):
        properties = tools[name].parameters["properties"]
        assert {"kind", "resource"} <= set(properties), name
        assert "resource" not in tools[name].parameters.get("required", []), name
    read_properties = tools["read"].parameters["properties"]
    assert {"key", "department", "term", "section", "expand"} <= set(read_properties)


async def test_a_course_code_on_catalog_courses_reads_the_course(monkeypatch):
    """5710331 on the department-listing kind used to be a 422, and the model
    then guessed "EE 331" and answered about a course that does not exist."""
    service = WorkspaceService(uuid4())

    async def noop():
        return None

    captured: dict = {}

    async def fake_domain(name, **arguments):
        captured["name"] = name
        captured.update(arguments)
        return {}

    monkeypatch.setattr(service, "authorize", noop)
    monkeypatch.setattr(service, "domain", fake_domain)
    await service.read(ResourceRef(kind="catalog.courses", key="5710331"))
    assert captured["name"] == "get_course_sections"
    assert captured["course_code"] == "5710331"


def test_the_repeat_breaker_stops_the_fourth_identical_failing_call():
    hooks._repeat_failures.clear()
    context = SimpleNamespace(run_id="r1")
    key = hooks._repeat_key("read", {"resource": {"kind": "catalog.sections", "key": "X"}}, context)
    assert key is not None
    for _ in range(hooks.REPEAT_FAILURE_LIMIT):
        assert hooks._repeat_breaker(key) is None
        hooks._note_repeat_failure(key)
    blocked = hooks._repeat_breaker(key)
    assert blocked is not None
    assert blocked["error"] == "repeated_failing_call"
    # Mutations keep their retry semantics: the breaker only covers read-shaped tools.
    assert hooks._repeat_key("update", {}, context) is None
    # A different call in the same turn is not blocked by its neighbour.
    other = hooks._repeat_key("read", {"resource": {"kind": "catalog.sections", "key": "Y"}}, context)
    assert hooks._repeat_breaker(other) is None
    hooks._repeat_failures.clear()

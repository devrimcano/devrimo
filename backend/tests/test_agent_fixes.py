"""Fixes from the 30-case live eval: flat tool arguments and code lookups.

The eval showed 46 of 107 tool calls failing, most because the model emitted
read(kind=..., key=...) instead of read(resource={...}); agno rejects that
before the tool body runs, so each mistake cost a full turn. These cover the
coercion and the code-vs-department key distinction that produced the wrong
"5710331 is not offered" answer.
"""

from app.agents.scholar.hooks import _coerce_flat_arguments
from app.workspace.service import _looks_like_course_code


def test_flat_read_arguments_are_folded_into_resource():
    assert _coerce_flat_arguments("read", {"kind": "my.updates"}) == {"resource": {"kind": "my.updates"}}
    folded = _coerce_flat_arguments("read", {"kind": "catalog.sections", "key": "EE 201", "term": "20252"})
    assert folded == {"resource": {"kind": "catalog.sections", "key": "EE 201", "term": "20252"}}
    folded = _coerce_flat_arguments("search", {"kind": "catalog.departments", "query": "CENG"})
    assert folded == {"resource": {"kind": "catalog.departments"}, "query": "CENG"}


def test_flat_mutation_arguments_are_folded_too():
    """update and undo take the same resource object and were flattened as well."""
    folded = _coerce_flat_arguments(
        "update",
        {"kind": "planning.timetable", "changes": {"operation": "set_options"}, "expected_revision": 1,
         "idempotency_key": "k"},
    )
    assert folded["resource"] == {"kind": "planning.timetable"}
    assert folded["expected_revision"] == 1
    assert _coerce_flat_arguments("undo", {"kind": "planning.timetable", "expected_revision": 0}) == {
        "resource": {"kind": "planning.timetable"}, "expected_revision": 0,
    }


def test_a_null_resource_does_not_block_the_fold():
    assert _coerce_flat_arguments("read", {"resource": None, "kind": "my.updates"}) == {
        "resource": {"kind": "my.updates"}
    }


def test_correct_arguments_are_left_alone():
    expected = {"resource": {"kind": "my.updates"}}
    assert _coerce_flat_arguments("read", expected) == expected
    assert _coerce_flat_arguments("read", {}) == {}
    assert _coerce_flat_arguments("plan", {"request": {}}) == {"request": {}}
    assert _coerce_flat_arguments("read", {"unrelated": "value"}) == {"unrelated": "value"}


def test_a_course_code_is_not_a_department_listing_key():
    assert _looks_like_course_code("5710331")
    assert _looks_like_course_code("EE 201")
    assert _looks_like_course_code("CENG331")
    assert not _looks_like_course_code("571")
    assert not _looks_like_course_code("Computer Engineering")

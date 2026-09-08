"""The isolated prerequisite gate for courses entering the schedule pool."""

from uuid import UUID

import pytest
from fastapi import HTTPException

from app.campus import prerequisites


USER_ID = UUID("11111111-1111-1111-1111-111111111111")


def row(code: str, set_no: str = "1") -> dict:
    return {"prerequisite_course_code": code, "set_no": set_no, "min_grade": "DD"}


def test_no_prerequisites_is_eligible():
    assert prerequisites.unmet_prerequisites([], []) == ()


def test_dd_or_higher_satisfies_a_prerequisite():
    completed = [{"course_code": "PHYS213", "grade": "DD"}]
    assert prerequisites.unmet_prerequisites([row("2300213")], completed) == ()


def test_display_code_keeps_four_digit_course_numbers():
    assert prerequisites.display_code("2300213") == "PHYS 213"
    assert prerequisites.display_code("2402201") == "HIST 2201"
    assert prerequisites.display_code("8770301") == "OHS 301"


def test_failed_or_missing_course_does_not_satisfy_a_prerequisite():
    assert prerequisites.unmet_prerequisites([row("2300213")], []) == ("2300213",)
    completed = [{"course_code": "PHYS213", "grade": "FD"}]
    assert prerequisites.unmet_prerequisites([row("2300213")], completed) == ("2300213",)


def test_courses_in_one_set_are_all_required_and_sets_are_alternatives():
    rows = [row("2300213", "1"), row("2360219", "1"), row("5670101", "2")]
    completed = [{"course_code": "EE101", "grade": "CC"}]
    assert prerequisites.unmet_prerequisites(rows, completed) == ()
    assert prerequisites.unmet_prerequisites(rows, []) == ("5670101",)


def test_malformed_catalog_answer_is_not_treated_as_no_prerequisite():
    try:
        prerequisites._rows({"message": "session expired"})
    except ValueError as exc:
        assert "could not be read" in str(exc)
    else:
        raise AssertionError("malformed prerequisite data was accepted")


def test_non_dict_rows_in_a_wrapped_answer_are_not_treated_as_empty():
    with pytest.raises(ValueError, match="rows could not be read"):
        prerequisites._rows({"prerequisites": ["garbage"]})


async def test_filter_approves_and_rejects_courses_from_transcript(monkeypatch):
    calls = []

    async def call(db, user_id, tool, values, *, session=None):
        calls.append((tool, values, session))
        assert tool == "get_course_prerequisites"
        return [] if values["course"] == "5670201" else [row("2300213")]

    monkeypatch.setattr(prerequisites, "call_course_info", call)
    courses = [{"code": "5670201"}, {"code": "5670213"}]
    approved, rejected, warnings = await prerequisites.filter_courses(
        None, USER_ID, "catalog", "20261", courses, [{"course_code": "PHYS213", "grade": "FF"}]
    )
    assert approved == [{"code": "5670201"}]
    assert [item.as_dict() for item in rejected] == [{
        "course_code": "5670213",
        "course_label": "EE 213",
        "prerequisite_course_codes": ["2300213"],
        "prerequisite_course_labels": ["PHYS 213"],
    }]
    assert warnings == []
    assert [values for _, values, _ in calls] == [
        {"department": "567", "semester": "20261", "course": "5670201"},
        {"department": "567", "semester": "20261", "course": "5670213"},
    ]


async def test_unreadable_prerequisites_fail_closed_with_a_warning(monkeypatch):
    async def call(*args, **kwargs):
        raise HTTPException(502, "catalog unavailable")

    monkeypatch.setattr(prerequisites, "call_course_info", call)
    approved, rejected, warnings = await prerequisites.filter_courses(
        None, USER_ID, None, "20261", [{"code": "5670213"}], []
    )
    assert approved == []
    assert rejected == []
    assert warnings == ["EE 213: prerequisites could not be verified (catalog unavailable)."]

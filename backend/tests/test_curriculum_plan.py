"""Recommendations follow the actual SAIS Curriculum semester board."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
import app.api.v1.schedule as schedule
from app.campus import curriculum

USER = SimpleNamespace(id="11111111-1111-1111-1111-111111111111")

def row(code, grade=""):
    return {"course_code": code, "course_name": code, "grade": grade}

def board(*semesters):
    return {"semesters": list(semesters)}

def semester(number, completed, *courses):
    return {"semester": number, "completed": completed, "courses": list(courses)}

@pytest.mark.parametrize("wrapped", [False, True])
def test_first_unchecked_semester_and_only_blank_grades(wrapped):
    data = board(semester(4, False, row("EE202")), semester(1, True, row("PHYS105")),
                 semester(2, True, row("MATH130")),
                 semester(3, False, row("PHYS213"), row("OHS301", "S"), row("EE201"), row("MATH219", "FF")))
    selected = curriculum.next_semester_courses({"result": data} if wrapped else data)
    assert [r["course_code"] for r in selected] == ["PHYS213", "EE201"]

def test_first_unchecked_with_no_blank_grades_does_not_advance():
    data = board(semester(1, False, row("MATH129", "FF")), semester(2, False, row("MATH130")))
    assert curriculum.next_semester_courses(data) == []

def test_all_checked_is_an_empty_answer():
    assert curriculum.next_semester_courses(board(semester(1, True, row("EE101")))) == []

@pytest.mark.parametrize("data", [None, {}, {"semesters": []}, board({"semester": 1, "courses": []}),
                                  board(semester(1, False, {"course_code": "EE201"}))])
def test_unreadable_completion_or_grade_is_not_treated_as_blank(data):
    with pytest.raises(ValueError):
        curriculum.next_semester_courses(data)

async def test_pipeline_uses_board_not_categories_and_verifies_target_term(monkeypatch):
    calls = []
    data = board(semester(1, True, row("PHYS105")), semester(2, True, row("MATH130")),
                 semester(3, False, row("PHYS213"), row("MATH219"), row("EE201"), row("EE213"),
                          row("ENG211"), row("OHS301", "S"), row("HIST2201"), row("HIST2205")),
                 semester(4, False, row("EE202")))
    offered = ["2300213", "2360219", "5670201", "5670213", "6390211", "8770301", "2402201", "2402205", "5670202"]
    async def call(db, user_id, tool, values, *, session=None):
        calls.append((tool, values))
        if tool == "get_student_curriculum":
            return {"result": data}
        if tool == "get_course_prerequisites":
            return []
        assert tool == "list_program_courses"
        assert values["semester"] == "20261"
        return [{"course_code": c, "name": c, "credit": "3"} for c in offered if c.startswith(values["department"])]
    monkeypatch.setattr(schedule, "call_course_info", call)
    monkeypatch.setattr(schedule.prerequisites, "call_course_info", call)
    courses, _, rejections = await schedule._curriculum_courses(None, USER, None, "567", "20261", [])
    assert [c["code"] for c in courses] == ["2300213", "2360219", "5670201", "5670213", "6390211", "2402201"]
    assert all(c["sections"] == [] for c in courses)
    assert calls[0][0] == "get_student_curriculum"
    assert not any("category" in tool for tool, _ in calls)
    assert rejections == []

async def test_course_not_offered_in_target_term_is_excluded(monkeypatch):
    async def call(db, user_id, tool, values, *, session=None):
        if tool == "get_student_curriculum":
            return board(semester(3, False, row("EE201"), row("EE213")))
        if tool == "get_course_prerequisites":
            return []
        return [{"course_code": "5670201", "name": "Circuit Theory", "credit": "4"}]
    monkeypatch.setattr(schedule, "call_course_info", call)
    monkeypatch.setattr(schedule.prerequisites, "call_course_info", call)
    courses, warnings, rejections = await schedule._curriculum_courses(None, USER, None, "567", "20261", [])
    assert [c["code"] for c in courses] == ["5670201"]
    assert warnings == []
    assert rejections == []


async def test_unmet_prerequisite_is_removed_and_returned_for_popup(monkeypatch):
    async def call(db, user_id, tool, values, *, session=None):
        if tool == "get_student_curriculum":
            return board(semester(3, False, row("EE201"), row("EE213")))
        if tool == "get_course_prerequisites":
            return [] if values["course"] == "5670201" else [{
                "prerequisite_course_code": "2300213",
                "set_no": "1",
                "min_grade": "DD",
            }]
        return [
            {"course_code": "5670201", "name": "Circuit Theory", "credit": "4"},
            {"course_code": "5670213", "name": "Circuits Laboratory", "credit": "2"},
        ]

    monkeypatch.setattr(schedule, "call_course_info", call)
    monkeypatch.setattr(schedule.prerequisites, "call_course_info", call)
    courses, warnings, rejections = await schedule._curriculum_courses(
        None, USER, None, "567", "20261", [{"course_code": "PHYS213", "grade": "FD"}]
    )
    assert [course["code"] for course in courses] == ["5670201"]
    assert warnings == []
    assert rejections == [{
        "course_code": "5670213",
        "course_label": "EE 213",
        "prerequisite_course_codes": ["2300213"],
        "prerequisite_course_labels": ["PHYS 213"],
    }]

async def test_unreadable_board_does_not_fall_back_to_general_catalog(monkeypatch):
    async def call(db, user_id, tool, values, *, session=None):
        assert tool == "get_student_curriculum"
        return {"message": "Session expired"}
    monkeypatch.setattr(schedule, "call_course_info", call)
    with pytest.raises(HTTPException):
        await schedule._curriculum_courses(None, USER, None, "567", "20261", [])

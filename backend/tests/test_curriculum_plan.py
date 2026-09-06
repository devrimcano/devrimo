"""The curriculum read that replaced the planner's agent run.

The agent took a median of 95.8 seconds in production to answer this, and its
answer was wrong in two ways nobody could see: it compared courses by code
alone, so a course sat and failed counted as done, and it was told to prefer the
Turkish-citizen variant of the compulsory history and language courses "unless
the student's record says otherwise" when no field in the record says anything
about citizenship. Both are covered here, because both are the kind of mistake
that looks like a correct answer.
"""

from types import SimpleNamespace

import app.api.v1.schedule as schedule
from app.campus import curriculum
from app.campus.course_info import _tool_arguments
from fastapi import HTTPException

USER = SimpleNamespace(id="11111111-1111-1111-1111-111111111111")

OVERVIEW = {
    "program_types": [{"id": "2", "name": "MINOR"}, {"id": "1", "name": "MAJOR"}],
    "course_categories": [
        {"id": "2-567", "name": "DEPARTMENTAL ELECTIVE"},
        {"id": "1-567", "name": "MUST COURSE"},
        {"id": "1-236", "name": "MUST COURSE"},
    ],
}


# --- choosing the programme --------------------------------------------------


def test_the_major_and_the_students_own_department_are_chosen():
    """A double major is separated by the department suffix alone, and that is enough."""
    program_type, categories = curriculum.select_program(OVERVIEW, "567")
    assert program_type == "1", "MAJOR wins over whichever type happened to be listed first"
    assert [row["id"] for row in categories] == ["1-567", "2-567"]


def test_must_courses_come_before_electives():
    """The pool should arrive in the order a student meets their requirements."""
    _, categories = curriculum.select_program(
        {
            "program_types": [{"id": "1", "name": "MAJOR"}],
            "course_categories": [
                {"id": "4-567", "name": "FREE ELECTIVE"},
                {"id": "1-567", "name": "MUST COURSES"},
                {"id": "3-567", "name": "NONDEPARTMENTAL ELECTIVE"},
            ],
        },
        "567",
    )
    assert [row["name"] for row in categories] == [
        "MUST COURSES",
        "NONDEPARTMENTAL ELECTIVE",
        "FREE ELECTIVE",
    ]


def test_an_unrecognised_suffix_falls_back_to_every_category():
    """Better every category than none: the suffix means something we do not know."""
    _, categories = curriculum.select_program(OVERVIEW, "999")
    assert len(categories) == 3


# --- course codes ------------------------------------------------------------


def test_a_lettered_code_expands_and_a_seven_digit_one_is_left_alone():
    assert curriculum.normalise_code("PHYS 213") == "2300213"
    assert curriculum.normalise_code("2300213") == "2300213"


def test_an_unknown_code_is_dropped_rather_than_prefixed():
    """Rewriting an unrecognised code with the home prefix invents a course."""
    assert curriculum.normalise_code("ZZZ101") is None
    assert curriculum.normalise_code("") is None


# --- what the student still has to take --------------------------------------


def test_a_passed_course_is_done_and_a_failed_one_is_not():
    """The agent compared codes and had no way to tell these apart."""
    completed = [
        {"course_code": "MATH119", "grade": "BA"},
        {"course_code": "PHYS213", "grade": "FF"},
    ]
    assert not curriculum.still_needed("2360119", completed)
    assert curriculum.still_needed("2300213", completed)


def test_a_retaken_course_counts_as_passed_on_the_passing_attempt():
    completed = [
        {"course_code": "PHYS213", "grade": "FF"},
        {"course_code": "PHYS213", "grade": "CC"},
    ]
    assert not curriculum.still_needed("2300213", completed)


# --- the citizenship variants ------------------------------------------------


def _history_pair():
    return [
        {"code": "2402201", "name": "History of the Turkish Revolution I"},
        {"code": "2402205", "name": "History of Turkey I"},
    ]


def test_one_variant_offered_is_left_exactly_as_it_is():
    """The usual case: SAIS built this list for this student, so it already decided."""
    only = [{"code": "2402205", "name": "History of Turkey I"}]
    kept, warnings = curriculum.resolve_citizenship_variants(only, [])
    assert kept == only
    assert warnings == []


def test_the_transcript_decides_between_two_variants():
    """A student who already sat one variant is on that track, whatever the default says."""
    kept, warnings = curriculum.resolve_citizenship_variants(
        _history_pair(), [{"course_code": "2402205", "grade": "FF"}]
    )
    assert [course["code"] for course in kept] == ["2402205"]
    assert warnings == []


def test_with_nothing_to_go_on_the_turkish_pair_is_taken_and_said_out_loud():
    """No field anywhere records citizenship, so the default has to be visible."""
    kept, warnings = curriculum.resolve_citizenship_variants(_history_pair(), [])
    assert [course["code"] for course in kept] == ["2402201"]
    assert len(warnings) == 1
    assert "2402205" in warnings[0], "an international student has to be told what to add"


def test_the_language_variants_are_separated_from_the_history_ones():
    """642 is Turkish Language, 629 is Turkish as a Foreign Language."""
    courses = [
        {"code": "6420103", "name": "Turkish I"},
        {"code": "6290101", "name": "Turkish for Foreigners I"},
        {"code": "5670201", "name": "Circuit Theory"},
    ]
    kept, warnings = curriculum.resolve_citizenship_variants(courses, [])
    assert [course["code"] for course in kept] == ["6420103", "5670201"]
    assert len(warnings) == 1


# --- the pipeline ------------------------------------------------------------


class _Campus:
    """Stands in for the Course Info server, recording what was asked of it."""

    def __init__(self, categories: dict, listings: dict, fail: set[str] | None = None):
        self.categories = categories
        self.listings = listings
        self.fail = fail or set()
        self.calls: list[tuple[str, dict]] = []

    def install(self, monkeypatch):
        async def call_course_info(db, user_id, tool_suffix, values, *, session=None):
            self.calls.append((tool_suffix, dict(values)))
            if tool_suffix == "get_student_course_categories":
                return OVERVIEW
            if tool_suffix == "get_student_courses_by_category":
                return self.categories.get(values["category"], {"courses": []})
            if tool_suffix == "list_program_courses":
                owner = values["department"]
                if owner in self.fail:
                    raise HTTPException(502, f"Course catalog request failed: {owner}")
                return self.listings.get(owner, [])
            raise AssertionError(f"unexpected tool {tool_suffix}")

        monkeypatch.setattr(schedule, "call_course_info", call_course_info)
        return self


def _listing(*rows):
    return [
        {"course_code": code, "name": name, "credit": "3.00 (3.00,0.00,0.00)"} for code, name in rows
    ]


async def test_the_pool_is_this_terms_offerings_minus_what_is_already_passed(monkeypatch):
    campus = _Campus(
        categories={
            "1-567": {
                "courses": [
                    {"course_code": "EE201", "course_name": "Circuit Theory", "credit": "4", "year_or_ects": "2"},
                    {"course_code": "MATH119", "course_name": "Calculus", "credit": "5", "year_or_ects": "1"},
                    {"course_code": "PHYS213", "course_name": "Physics III", "credit": "4", "year_or_ects": "2"},
                ]
            },
            "2-567": {"courses": []},
        },
        listings={
            "567": _listing(("5670201", "CIRCUIT THEORY")),
            "236": _listing(("2360119", "CALCULUS WITH ANALYTIC GEOMETRY")),
            "230": _listing(("2300213", "GENERAL PHYSICS III")),
        },
    ).install(monkeypatch)

    courses, warnings = await schedule._curriculum_courses(
        None, USER, None, "567", "20252", [{"course_code": "MATH119", "grade": "BA"}]
    )

    assert [course["code"] for course in courses] == ["2300213", "5670201"]
    # Calculus is gone because it was passed, not because it was missing.
    assert all(course["code"] != "2360119" for course in courses)
    # Names come from the catalog, not from the curriculum page.
    assert courses[0]["name"] == "GENERAL PHYSICS III"
    # Sections are never fetched here; the planner loads them when opened.
    assert all(course["sections"] == [] for course in courses)
    assert warnings == []


async def test_a_department_whose_listing_fails_is_dropped_and_reported(monkeypatch):
    """Recommending an unverified offering is the one thing this must not do."""
    campus = _Campus(
        categories={
            "1-567": {
                "courses": [
                    {"course_code": "EE201", "course_name": "Circuit Theory", "credit": "4"},
                    {"course_code": "PHYS213", "course_name": "Physics III", "credit": "4"},
                ]
            }
        },
        listings={"567": _listing(("5670201", "CIRCUIT THEORY"))},
        fail={"230"},
    ).install(monkeypatch)

    courses, warnings = await schedule._curriculum_courses(None, USER, None, "567", "20252", [])

    assert [course["code"] for course in courses] == ["5670201"]
    assert any("PHYS" in warning for warning in warnings)


async def test_a_category_that_answers_with_prose_becomes_a_warning(monkeypatch):
    """"No electives left" and "we could not read your electives" are not the same."""
    _Campus(
        categories={"1-567": {"courses": [], "message": "Ders bulunamadi."}},
        listings={},
    ).install(monkeypatch)

    courses, warnings = await schedule._curriculum_courses(None, USER, None, "567", "20252", [])

    assert courses == []
    assert any("Ders bulunamadi." in warning for warning in warnings)


async def test_an_unreadable_course_code_is_reported_not_guessed(monkeypatch):
    _Campus(
        categories={"1-567": {"courses": [{"course_code": "ZZZ101", "course_name": "Mystery"}]}},
        listings={},
    ).install(monkeypatch)

    courses, warnings = await schedule._curriculum_courses(None, USER, None, "567", "20252", [])

    assert courses == []
    assert any("ZZZ101" in warning for warning in warnings)


async def test_one_listing_call_per_owning_department(monkeypatch):
    """Not one per course: this is the whole reason the agent was slow."""
    campus = _Campus(
        categories={
            "1-567": {
                "courses": [
                    {"course_code": "EE201", "course_name": "A"},
                    {"course_code": "EE202", "course_name": "B"},
                    {"course_code": "EE301", "course_name": "C"},
                    {"course_code": "PHYS213", "course_name": "D"},
                ]
            }
        },
        listings={
            "567": _listing(("5670201", "A"), ("5670202", "B"), ("5670301", "C")),
            "230": _listing(("2300213", "D")),
        },
    ).install(monkeypatch)

    await schedule._curriculum_courses(None, USER, None, "567", "20252", [])

    listings = [values["department"] for tool, values in campus.calls if tool == "list_program_courses"]
    assert sorted(listings) == ["230", "567"]


# --- the argument the tool needed and this module could not name --------------


def test_program_type_can_be_passed_to_the_tool():
    """It had no alias, and an unindexed lookup made that a 500 inside the handler."""
    function = SimpleNamespace(
        parameters={"properties": {"program_type": {}, "category_id": {}}, "required": []}
    )
    assert _tool_arguments(function, {"program_type": "1", "category": "1-567"}) == {
        "program_type": "1",
        "category_id": "1-567",
    }


def test_program_type_never_binds_to_a_department_parameter():
    """"program" is already a spelling of department; the two must not cross."""
    function = SimpleNamespace(parameters={"properties": {"program": {}}, "required": []})
    assert _tool_arguments(function, {"program_type": "1"}) == {}
    assert _tool_arguments(function, {"department": "567"}) == {"program": "567"}

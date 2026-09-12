"""The bridge between the three names METU gives one department.

This is generated data, so the tests that matter are not "does the loader
work" but "does the generated file still answer the questions the planner asks
of it". A regenerated directory that quietly lost an abbreviation, or gained a
second row for a common name, would break section eligibility in a way nothing
else notices — the student is simply told the wrong thing.
"""

import pytest

from app.academic_catalog.service import _resolve_catalog_scope
from app.campus.departments import (
    all_departments,
    by_abbreviation,
    by_code,
    by_name,
    expand_course_code,
    resolve,
)

# Exactly the departments a real MATH 260 section-1 eligibility table admitted.
# If a regenerated directory cannot name one of these, that table stops being
# interpretable.
ELIGIBILITY_TABLE_ABBREVIATIONS = [
    "ADM", "BA", "CENG", "ECON", "EE", "EME", "FM",
    "IE", "MMI", "PHED", "PHYS", "PSY", "STAT",
]


@pytest.mark.parametrize("abbreviation", ELIGIBILITY_TABLE_ABBREVIATIONS)
def test_every_abbreviation_a_real_table_used_resolves(abbreviation):
    department = by_abbreviation(abbreviation)
    assert department is not None, f"{abbreviation} is missing from the directory"
    assert department.code.isdigit() and len(department.code) == 3
    # And round-trips, so code and abbreviation cannot drift apart.
    assert by_code(department.code).abbreviation == abbreviation


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("Computer Engineering", "571"),
        ("Bilgisayar Mühendisliği", "571"),
        # The SAIS student card reports both halves joined by a slash.
        ("Computer Engineering/Bilgisayar Mühendisliği", "571"),
        ("Mathematics", "236"),
        ("Matematik", "236"),
        ("Electrical and Electronics Engineering", "567"),
        ("Physics", "230"),
        ("Statistics", "246"),
    ],
)
def test_sais_names_resolve_to_one_department(name, code):
    department = by_name(name)
    assert department is not None, f"{name!r} did not resolve"
    assert department.code == code


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("571", "571"),
        ("CENG", "571"),
        # A seven-digit course code carries its owner in the first three digits.
        ("5710100", "571"),
        ("2360260", "236"),
        ("MATH", "236"),
    ],
)
def test_resolve_accepts_code_abbreviation_or_course_code(value, code):
    assert resolve(value).code == code


@pytest.mark.parametrize(
    ("value", "code"),
    [
        # The regression: stripping the letters leaves "213", which is three
        # digits and reads as a department code. There is no department 213, so
        # the lookup returned nothing and Physics was never consulted — the
        # chat agent could not answer "can I take PHYS 213?" at all.
        ("PHYS213", "230"),
        ("phys 213", "230"),
        ("MATH260", "236"),
        ("CENG 140", "571"),
    ],
)
def test_a_lettered_course_code_resolves_by_its_prefix(value, code):
    assert resolve(value).code == code


@pytest.mark.parametrize(
    ("value", "expanded", "name"),
    [
        ("PHYS213", "2300213", "Physics"),
        ("PHYS 213", "2300213", "Physics"),
        ("math260", "2360260", "Mathematics"),
        ("CENG 140", "5710140", "Computer Engineering"),
        # Already numeric, passed through with its owner identified.
        ("2300213", "2300213", "Physics"),
    ],
)
def test_course_codes_expand_to_the_numeric_form_the_catalog_wants(value, expanded, name):
    """The catalog only accepts the seven-digit form.

    A student says "PHYS 213"; SAIS knows 2300213. Without this the lookup ran
    with a code the catalog had never heard of and returned nothing.
    """
    full_code, department = expand_course_code(value)
    assert full_code == expanded
    assert department.name_en == name


@pytest.mark.parametrize("value", ["", "ZZZZ999", "213", "nonsense"])
def test_an_unrecognisable_course_code_is_refused(value):
    assert expand_course_code(value) is None


@pytest.mark.parametrize("value", ["", "   ", "Engineering", "not a department at all"])
def test_an_unclear_name_is_refused_rather_than_guessed(value):
    """The failure mode of guessing is a confidently wrong department.

    Downstream that becomes a student being told they are eligible for a
    section they cannot register for, which is worse than no answer.
    """
    assert by_name(value) is None


def test_no_two_departments_claim_one_abbreviation():
    seen: dict[str, str] = {}
    for department in all_departments():
        if not department.abbreviation:
            continue
        assert department.abbreviation not in seen, (
            f"{department.abbreviation} claimed by {seen.get(department.abbreviation)} and {department.code}"
        )
        seen[department.abbreviation] = department.code


@pytest.mark.parametrize(
    ("department", "course", "expected_department", "expected_course"),
    [
        # The bug: the student-facing read compared the lettered code against
        # the numeric catalog key, so it said "not available in the published
        # release" for a course the admin panel listed.
        (None, "EE201", "567", "5670201"),
        (None, "EE 201", "567", "5670201"),
        (None, "CENG 331", "571", "5710331"),
        # Already numeric passes through, with its owner identified.
        (None, "5670201", "567", "5670201"),
        # A department named on its own resolves to its code.
        ("EE", None, "567", None),
        ("571", None, "571", None),
        # A department abbreviation alongside a numeric course resolves too.
        ("CENG", "5710331", "571", "5710331"),
        (None, None, None, None),
    ],
)
def test_the_catalog_read_resolves_a_students_lettered_code(
    department, course, expected_department, expected_course
):
    assert _resolve_catalog_scope(department, course) == (expected_department, expected_course)


def test_offsite_and_joint_programmes_are_excluded():
    """They duplicate an Ankara department's English name exactly.

    Keeping them is what made "Computer Engineering" ambiguous, so their
    absence is the property the name lookup depends on.
    """
    for department in all_departments():
        joined = f"{department.name_en} {department.name_tr}".lower()
        assert "kuzey kıbrıs" not in joined
        assert "uluslararası ortak program" not in joined
        assert "international joint program" not in joined
        assert "tau" not in joined.split()


@pytest.mark.parametrize(
    ("course_code", "name"),
    [
        ("8670101", "Software Engineering"),
        ("8850200", "Robotics"),
        ("9090300", "Multimedia Informatics"),
        ("9730100", "Financial Mathematics"),
    ],
)
def test_graduate_course_codes_resolve_to_their_owner(course_code, name):
    """Why the 8xx/9xx rows are kept despite carrying no abbreviation.

    They can never appear in a section's eligibility table, which keys on the
    abbreviation — but a seven-digit course code names its owner in the first
    three digits, and without these rows a graduate code resolves to nothing.
    Trimming the directory to "real departments" would silently break this.
    """
    department = resolve(course_code)
    assert department is not None, f"{course_code} did not resolve"
    assert department.name_en == name

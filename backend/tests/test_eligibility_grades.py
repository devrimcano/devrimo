"""Grade bands and multi-row departments in a section's eligibility table.

Both fixtures below are the real tables live SAIS returned, not invented ones.
PHYS213 section 1 exposed two defects at once: the table holds two rows for the
same department, and both of them restrict the section to students who have not
passed the course — a dimension the comparator ignored entirely, so it told a
student who had already passed that they could register again.
"""

import pytest

from app.campus.eligibility import (
    course_candidates,
    evaluate,
    grade_token,
    has_passed,
    prior_grade,
)

# PHYS213 section 1, exactly as SAIS returned it.
PHYS213_SECTION_1 = [
    {
        "given_dept": "EE", "start_char": "AA", "end_char": "ZZ",
        "min_cgpa": "0.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95",
        "start_grade": "Kaldi", "end_grade": "Kaldi",
    },
    {
        "given_dept": "EE", "start_char": "AA", "end_char": "ZZ",
        "min_cgpa": "0.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95",
        "start_grade": "Hic almayanlar veya Basarisizlar (FD ve alti)",
        "end_grade": "Hic almayanlar veya Basarisizlar (FD ve alti)",
    },
]


def test_a_student_who_has_never_taken_the_course_is_admitted():
    assert evaluate(PHYS213_SECTION_1, department="EE", surname="TA").eligible


def test_a_student_who_failed_it_is_admitted():
    verdict = evaluate(PHYS213_SECTION_1, department="EE", surname="TA", prior_grade="FF")
    assert verdict.eligible


def test_a_student_who_already_passed_it_is_not():
    """The whole point of the band: this section is for people who still need it."""
    verdict = evaluate(PHYS213_SECTION_1, department="EE", surname="TA", prior_grade="BB")
    assert not verdict.eligible
    assert "have not passed" in verdict.reason


def test_a_second_row_can_admit_where_the_first_refuses():
    """Reading only the first matching row rejected people the second was for."""
    rows = [
        {"given_dept": "EE", "start_char": "AA", "end_char": "DZ"},
        {"given_dept": "EE", "start_char": "EA", "end_char": "ZZ"},
    ]
    assert evaluate(rows, department="EE", surname="TA").eligible
    assert evaluate(rows, department="EE", surname="BA").eligible
    # Still excluded when no row covers them.
    assert not evaluate([rows[0]], department="EE", surname="TA").eligible


def test_an_unrecognised_grade_band_never_excludes():
    """Unknown never excludes — a wrong "you cannot" is never discovered."""
    rows = [{"given_dept": "EE", "start_grade": "AA", "end_grade": "CC"}]
    assert evaluate(rows, department="EE", prior_grade="BB").eligible


def test_an_unknown_prior_grade_never_excludes():
    assert evaluate(PHYS213_SECTION_1, department="EE", prior_grade=None).eligible


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("BA", "BA"),
        # A real transcript value: the scrape keeps the whole cell.
        ("BA   \n                    (NTE)", "BA"),
        ("s", "S"),
        ("", ""),
        (None, ""),
    ],
)
def test_grade_token_reads_the_letter_grade_alone(value, expected):
    assert grade_token(value) == expected


@pytest.mark.parametrize(
    ("grade", "passed"),
    [("AA", True), ("DD", True), ("S", True), ("EX", True),
     ("FD", False), ("FF", False), ("NA", False), ("W", False), ("", False)],
)
def test_has_passed(grade, passed):
    assert has_passed(grade) is passed


class _Owner:
    abbreviation = "PHYS"


def test_course_candidates_covers_both_spellings():
    """The table speaks seven-digit codes; the transcript speaks PHYS213."""
    assert set(course_candidates("PHYS213", _Owner(), "2300213")) == {"PHYS213", "2300213"}
    assert "PHYS213" in course_candidates("2300213", _Owner(), "2300213")


def test_prior_grade_finds_the_course_under_either_spelling():
    transcript = [{"course_code": "PHYS213", "grade": "BB"}, {"course_code": "MATH130", "grade": "DC"}]
    assert prior_grade(transcript, ["2300213", "PHYS213"]) == "BB"
    assert prior_grade(transcript, ["CENG240"]) is None


def test_a_repeated_course_reports_the_passing_attempt():
    """Repeats are why the band exists; a later pass is what decides."""
    transcript = [{"course_code": "PHYS213", "grade": "FF"}, {"course_code": "PHYS213", "grade": "CC"}]
    assert prior_grade(transcript, ["PHYS213"]) == "CC"


def test_the_year_band_is_applied():
    """HIST2201 §1 this term admits one department's second-years only."""
    rows = [{"given_dept": "ADM", "start_char": "AA", "end_char": "ZZ",
             "min_year": "2", "max_year": "2", "start_grade": "Herkes alabilir",
             "end_grade": "Herkes alabilir"}]
    assert evaluate(rows, department="ADM", surname="TA", year=2).eligible
    assert not evaluate(rows, department="ADM", surname="TA", year=1).eligible
    assert not evaluate(rows, department="ADM", surname="TA", year=3).eligible
    # Unknown year never excludes.
    assert evaluate(rows, department="ADM", surname="TA").eligible


def test_a_95_ceiling_means_no_upper_bound():
    """The table writes "no limit" as 95, not as an absent value."""
    rows = [{"given_dept": "EE", "min_year": "0", "max_year": "95"}]
    assert evaluate(rows, department="EE", year=1).eligible
    assert evaluate(rows, department="EE", year=6).eligible


def test_all_in_the_department_column_means_everyone():
    """MATH219 §7 is open to the whole university and said "open only to ALL"."""
    rows = [{"given_dept": "ALL", "start_char": "AA", "end_char": "ZZ",
             "min_cgpa": "0.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95"}]
    assert evaluate(rows, department="EE", surname="TA").eligible
    assert evaluate(rows, department="CENG", surname="ZZ").eligible


def test_a_wildcard_row_still_applies_its_other_bands():
    """"Open to everyone" is about the department column, not the whole row."""
    rows = [{"given_dept": "ALL", "start_char": "AA", "end_char": "DZ"}]
    assert evaluate(rows, department="EE", surname="BA").eligible
    assert not evaluate(rows, department="EE", surname="TA").eligible

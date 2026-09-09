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


def test_an_unrecognised_grade_band_is_unknown():
    """An unfamiliar registrar label still cannot certify a section.

    This used to assert the same of "AA"-"CC", on the grounds that a pair of
    letters was not a label this reader knew. It is: METU publishes the scale
    AA, BA, BB, CB, CC, DC, DD, FD, FF, NA, and a pair names every grade
    between its ends. That case is now covered by
    test_a_letter_range_grade_band_is_read_as_a_range; what remains unknown is
    a token that is not on the scale at all.
    """
    rows = [{"given_dept": "EE", "start_grade": "U", "end_grade": "NA"}]
    assert evaluate(rows, department="EE", prior_grade="BB").eligible is None


def test_an_unknown_prior_grade_never_excludes():
    assert evaluate(PHYS213_SECTION_1, department="EE", surname="TA", prior_grade=None).eligible


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


def test_course_candidates_preserves_four_digit_course_numbers_and_resolves_owner():
    assert "HIST2201" in course_candidates("2402201", None, "2402201")


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
    # A missing year cannot certify this bounded row.
    assert evaluate(rows, department="ADM", surname="TA").eligible is None


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


def test_nonempty_row_without_department_scope_is_unknown():
    rows = [{"min_cgpa": "2.00"}]
    assert evaluate(rows, department="EE", cgpa=4.0).eligible is None


def test_explicit_any_scope_can_apply_a_universal_restriction():
    rows = [{"given_dept": "ANY", "min_cgpa": "2.00"}]
    assert evaluate(rows, department="EE", cgpa=4.0).eligible is True


def test_a_wildcard_row_still_applies_its_other_bands():
    """"Open to everyone" is about the department column, not the whole row."""
    rows = [{"given_dept": "ALL", "start_char": "AA", "end_char": "DZ"}]
    assert evaluate(rows, department="EE", surname="BA").eligible
    assert not evaluate(rows, department="EE", surname="TA").eligible


# MATH 219 section 9, exactly as SAIS returned it on 2026-09-09. Three
# departments, three different ways of writing the grade column, in one table.
MATH219_SECTION_9 = [
    {
        "given_dept": "AEE", "start_char": "AA", "end_char": "ZZ",
        "min_cgpa": "0.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95",
        "start_grade": "BA", "end_grade": "NA",
    },
    {
        "given_dept": "BME", "start_char": "AA", "end_char": "ZZ",
        "min_cgpa": "0.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95",
        "start_grade": "Hic almayanlar veya Basarisizlar (FD ve alti)",
        "end_grade": "Hic almayanlar veya Basarisizlar (FD ve alti)",
    },
    {
        "given_dept": "EE", "start_char": "DA", "end_char": "KV",
        "min_cgpa": "0.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95",
        "start_grade": "Herkes alabilir", "end_grade": "Herkes alabilir",
    },
]


def verdict_for(rows, dept, **kwargs):
    return evaluate(rows, department=dept, surname=kwargs.pop("surname", "AA"),
                    cgpa=kwargs.pop("cgpa", 3.0), year=kwargs.pop("year", 3), **kwargs)


@pytest.mark.parametrize(
    "held,expected",
    [
        # BA-NA is every grade from BA down to NA: the registrar's way of
        # saying "unless you already have AA".
        ("BB", True),
        ("NA", True),
        ("FF", True),
        ("AA", False),
        # Nobody's grade at all is not a grade inside the range. This row is
        # written for students repeating the course; the ones who have never
        # taken it are admitted by a different row, where the table has one.
        (None, False),
    ],
)
def test_a_letter_range_grade_band_is_read_as_a_range(held, expected):
    assert verdict_for(MATH219_SECTION_9, "AEE", prior_grade=held).eligible is expected


def test_a_letter_range_no_longer_reports_the_table_as_unreadable():
    """This is what put MATH 219 in front of students as "could not be read"."""
    verdict = verdict_for(MATH219_SECTION_9, "AEE", prior_grade="CC")
    assert verdict.eligible is not None
    assert "could not" not in verdict.reason


def test_the_other_two_spellings_still_mean_what_they_meant():
    # "Never taken or failed" admits someone who has never taken it.
    assert verdict_for(MATH219_SECTION_9, "BME", prior_grade=None).eligible is True
    assert verdict_for(MATH219_SECTION_9, "BME", prior_grade="BB").eligible is False
    # "Everyone may take it" still admits everyone in the surname range.
    assert verdict_for(MATH219_SECTION_9, "EE", surname="EM", prior_grade="AA").eligible is True


def test_a_grade_label_outside_the_scale_stays_undecided_and_names_itself():
    """'U' is not on the AA-NA scale, and guessing at it is not allowed."""
    rows = [dict(MATH219_SECTION_9[0], start_grade="U", end_grade="U")]
    verdict = verdict_for(rows, "AEE", prior_grade="CC")
    assert verdict.eligible is None
    assert "U-U" in verdict.reason

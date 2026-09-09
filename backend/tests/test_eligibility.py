"""Section eligibility, against the real MATH 260 table.

The rows here are transcribed from a live SAIS eligibility page, so these are
not invented cases: ADM really is limited to surnames YA-YA with a 3.00 CGPA
floor, ECON really is DJ-KA, and CENG really does require second year or above.
"""

import pytest

from app.campus.eligibility import evaluate, tr_upper

# Transcribed verbatim from MATH 2360260 section 1.
MATH260 = [
    {"given_dept": "ADM", "start_char": "YA", "end_char": "YA", "min_cgpa": "3.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95"},
    {"given_dept": "BA", "start_char": "AA", "end_char": "ZZ", "min_cgpa": "3.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95"},
    {"given_dept": "CENG", "start_char": "AA", "end_char": "ZZ", "min_cgpa": "0.00", "max_cgpa": "4.00", "min_year": "2", "max_year": "95"},
    {"given_dept": "ECON", "start_char": "DJ", "end_char": "KA", "min_cgpa": "0.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95"},
    {"given_dept": "EME", "start_char": "DÖ", "end_char": "İŞ", "min_cgpa": "0.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95"},
    {"given_dept": "STAT", "start_char": "CB", "end_char": "GÜ", "min_cgpa": "0.00", "max_cgpa": "4.00", "min_year": "0", "max_year": "95"},
]


def test_a_department_absent_from_the_table_is_excluded():
    """The strongest rule: no row means no registration, whatever else matches."""
    verdict = evaluate(MATH260, department="ME", surname="Yılmaz", cgpa=3.5, year=3)
    assert not verdict.eligible
    assert "open only to" in verdict.reason


def test_an_empty_table_admits_everyone():
    """No restriction stated is different from a restriction that excludes."""
    assert evaluate([], department="CENG", surname="Ateş", cgpa=1.0, year=1).eligible


@pytest.mark.parametrize(
    ("surname", "eligible"),
    [
        ("Kabak", True),   # KA, inside
        ("Kaya", True),    # KA is the inclusive upper bound
        ("Kemal", False),  # KE is past KA
        ("Erdem", True),   # ER, comfortably inside
        ("Demir", False),  # DE sorts before the DJ floor
        ("Aydın", False),  # AY is well before DJ
    ],
)
def test_two_letter_surname_ranges(surname, eligible):
    """The bound is two letters, and the second one does real work.

    KAYA and KEMAL share an initial and fall on opposite sides of "KA"; so do
    DEMIR and DJ. Comparing initials alone would get all four wrong.
    """
    assert evaluate(MATH260, department="ECON", surname=surname).eligible is eligible


def test_turkish_letters_sort_where_turkish_says():
    """"DÖ"-"İŞ" only means anything under Turkish collation.

    Ö follows O and İ follows I, so "Gül" is inside the range and "Kaya" is
    past it. Under code-point order every one of these letters sorts after Z
    and the range admits nobody.
    """
    assert evaluate(MATH260, department="EME", surname="Gül").eligible
    assert evaluate(MATH260, department="EME", surname="Erdem").eligible
    assert not evaluate(MATH260, department="EME", surname="Kaya").eligible
    assert not evaluate(MATH260, department="EME", surname="Aydın").eligible


def test_cgpa_floor_is_enforced():
    assert not evaluate(MATH260, department="ADM", surname="Yalçın", cgpa=2.4).eligible
    assert evaluate(MATH260, department="ADM", surname="Yalçın", cgpa=3.2).eligible


def test_a_four_point_ceiling_is_not_a_ceiling():
    """Every row writes 4.00 as its maximum; it means "no upper bound"."""
    assert evaluate(MATH260, department="CENG", surname="Ateş", cgpa=4.0, year=3).eligible


def test_year_floor_is_enforced():
    assert not evaluate(MATH260, department="CENG", surname="Ateş", year=1).eligible
    assert evaluate(MATH260, department="CENG", surname="Ateş", year=2).eligible


def test_a_95_year_ceiling_is_not_a_ceiling():
    assert evaluate(MATH260, department="CENG", surname="Ateş", year=6).eligible


@pytest.mark.parametrize("missing", [{"surname": None}, {"cgpa": None}, {"year": None}, {}])
def test_missing_profile_fields_are_unknown_only_when_needed(missing):
    """A missing value is explicit unknown when the row needs that dimension.

    The CENG row's 0.00-4.00 CGPA range imposes no effective floor or ceiling,
    so CGPA may remain absent while the surname range and year floor still
    require their corresponding values.
    """
    known = {"surname": "Kemal", "cgpa": 3.0, "year": 3}
    expected = None if set(missing) & {"surname", "year"} else True
    assert evaluate(MATH260, department="CENG", **{**known, **missing}).eligible is expected


def test_an_unknown_department_skips_the_table_entirely():
    assert evaluate(MATH260, department=None, surname="Kemal").eligible is None
    assert evaluate(MATH260, department="", surname="Kemal").eligible is None


def test_matched_department_is_reported():
    assert evaluate(MATH260, department="ceng", surname="Ateş", year=3).matched_department == "CENG"


def test_turkish_uppercase_keeps_the_dot():
    assert tr_upper("iş") == "İŞ"
    assert tr_upper("ışık") == "IŞIK"

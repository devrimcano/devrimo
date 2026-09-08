"""Section eligibility, against the real MATH 260 table.

The rows here are transcribed from a live SAIS eligibility page, so these are
not invented cases: ADM really is limited to surnames YA-YA with a 3.00 CGPA
floor, ECON really is DJ-KA, and CENG really does require second year or above.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.campus.eligibility import evaluate, tr_upper, validated_constraint_rows
from app.planning.eligibility import academic_evidence_fresh, issue_eligibility_token

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
def test_what_we_do_not_know_never_excludes(missing):
    """A student who has told us nothing is not thereby ineligible.

    A false "you cannot take this" steers someone away from a section they were
    entitled to and they never find out; a false "you can" surfaces at
    registration, where they can act on it.
    """
    # Start from a student the table admits on every dimension, then blank the
    # one under test. Spreading over an explicit ``year=3`` instead raised
    # TypeError for the year case, so that parameter never actually ran.
    known = {"surname": "Kemal", "cgpa": 3.0, "year": 3}
    assert evaluate(MATH260, department="CENG", **{**known, **missing}).eligible


def test_an_unknown_department_skips_the_table_entirely():
    assert evaluate(MATH260, department=None, surname="Kemal").eligible
    assert evaluate(MATH260, department="", surname="Kemal").eligible


def test_matched_department_is_reported():
    assert evaluate(MATH260, department="ceng", surname="Ateş", year=3).matched_department == "CENG"


def test_turkish_uppercase_keeps_the_dot():
    assert tr_upper("iş") == "İŞ"
    assert tr_upper("ışık") == "IŞIK"


@pytest.mark.parametrize(
    "payload",
    [
        [{"unexpected": "value"}],
        {"constraints": [{"given_dept": "EE", "min_cgpa": {"nested": True}}]},
        {"constraints": [{"given_dept": "EE", "min_cgpa": "NaN"}]},
        {"constraints": ["not a row"]},
    ],
)
def test_malformed_constraint_rows_are_unknown_not_unrestricted(payload):
    assert validated_constraint_rows(payload) is None


def test_an_explicitly_empty_constraint_table_is_valid_open_evidence():
    assert validated_constraint_rows({"constraints": []}) == []


def test_academic_evidence_has_a_defined_freshness_window():
    now = datetime.now(UTC)
    assert academic_evidence_fresh(now, now, now=now)
    assert not academic_evidence_fresh(now - timedelta(days=8), now, now=now)
    assert not academic_evidence_fresh(now, now - timedelta(days=8), now=now)


def test_stale_academic_evidence_cannot_mint_a_positive_token():
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="stale"):
        issue_eligibility_token(
            uuid4(),
            "20261",
            "MATH119",
            "1",
            eligibility_status="verified",
            eligible=True,
            context_verified_at=now - timedelta(days=8),
            snapshot_fetched_at=now,
            meetings=[{"day": "Mon", "start_minute": 520, "duration_minutes": 110, "room": "A1"}],
        )

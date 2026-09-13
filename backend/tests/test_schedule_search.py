"""Search has to answer the forms a student actually types.

Reported from the schedule page: typing a course code returned nothing while the
same course was found by its title. The published search compared only the last
four digits of the stored code, so a pasted seven-digit code matched nothing,
and it required the typed text to appear inside the folded haystack - which is
"ceng331 computer organization ..." with no space, so "CENG 331" matched nothing
either. The title path worked, which is what made it look like the catalog was
missing the course.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.api.v1.schedule import (
    AiScheduleRequest,
    _course_code_matches,
    _index_rows,
    _search_fold,
    _short_code,
)
from app.campus import departments


def _course(code: str, full_code: str) -> dict:
    return {"code": code, "full_code": full_code, "department": "CENG"}


def _indexed(*rows: tuple[str, str], abbreviation: str = "CENG"):
    """The same (haystack, course) pairs the published search is handed."""
    owner = departments.resolve(abbreviation)
    payload = {"courses": [{"course_code": full, "name": name} for full, name in rows]}
    return owner, _index_rows(payload, owner)


def test_every_spelling_of_a_course_code_matches():
    ceng = _course("CENG331", "5710331")
    assert _course_code_matches(ceng, "331")
    assert _course_code_matches(ceng, "CENG331".replace("CENG", ""))  # "331"
    assert _course_code_matches(ceng, "5710331")
    assert _course_code_matches(ceng, "571")
    assert not _course_code_matches(ceng, "332")
    assert not _course_code_matches(ceng, "5710332")


def test_a_leading_zero_course_number_still_matches_what_a_student_types():
    math = _course("MATH119", "2360119")
    assert _course_code_matches(math, "119")
    assert _course_code_matches(math, "2360119")
    assert not _course_code_matches(math, "120")


def test_matching_is_prefix_only():
    """The planner's contract: "PHYS21" narrows, it does not widen.

    A suffix comparison once let "31" reach CENG 331, which both broke the
    narrowing and filled the forty-result limit with unrelated courses.
    """
    ceng = _course("CENG331", "5710331")
    assert not _course_code_matches(ceng, "31")
    assert not _course_code_matches(ceng, "1")
    assert _course_code_matches(ceng, "3")
    assert _course_code_matches(ceng, "33")
    assert _course_code_matches(ceng, "331")


def test_a_query_without_digits_does_not_filter_by_code():
    assert _course_code_matches(_course("CENG331", "5710331"), "")


def test_short_codes_drop_the_catalogs_zero_padding():
    assert _short_code("2400101", "HIST") == "HIST101"
    assert _short_code("5710331", "CENG") == "CENG331"


def test_folded_search_reaches_turkish_letters_from_an_english_keyboard():
    # Both sides are folded, and the k/ğ alternation is folded away with them:
    # a student typing "muhendislik" is looking for "Mühendisliği".
    assert _search_fold("Mühendislik") in _search_fold("Mühendisliği")
    assert "tarih" in _search_fold("TARİHİ")


def test_a_turkish_suffix_search_finds_the_title():
    owner, indexed = _indexed(("5710331", "BİLGİSAYAR MÜHENDİSLİĞİ"))
    haystack = indexed[0][0]
    assert _search_fold("muhendislik") in haystack


async def test_department_search_merges_partial_source_and_directory_matches(monkeypatch):
    import app.api.v1.schedule as schedule

    monkeypatch.setattr(
        schedule,
        "call_course_info",
        AsyncMock(return_value={"departments": [{"code": "430", "name": "Computer Education"}]}),
    )
    user = SimpleNamespace(id="user-1")

    result = await schedule.search_departments(query="Bilgisayar", user=user, db=object())

    assert {option["code"] for option in result["departments"]} >= {"430", "571"}


def test_ai_schedule_request_accepts_term_and_course_codes_compatibility_aliases():
    request = AiScheduleRequest.model_validate({"term": "20261", "course_codes": ["MATH260"]})
    assert request.semester == "20261"
    assert [course.code for course in request.courses] == ["MATH260"]


def test_department_search_lists_directory_matches_the_source_misses():
    """The source matches codes and English names; the directory does the rest.

    An ambiguous name lists every candidate rather than resolving to one:
    "Bilgisayar" is both Computer Engineering and Computer Education.
    """
    from app.api.v1.schedule import _directory_department_options

    assert any(option["code"] == "571" for option in _directory_department_options("CENG"))
    bilgisayar = {option["code"] for option in _directory_department_options("Bilgisayar")}
    assert {"571", "430"} <= bilgisayar

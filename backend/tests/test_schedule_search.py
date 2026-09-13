"""Search has to answer the forms a student actually types.

Reported from the schedule page: typing a course code returned nothing while the
same course was found by its title. The published search compared only the last
four digits of the stored code, so a pasted seven-digit code matched nothing,
and it required the typed text to appear inside the folded haystack - which is
"ceng331 computer organization ..." with no space, so "CENG 331" matched nothing
either. The title path worked, which is what made it look like the catalog was
missing the course.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.api.v1.schedule import (
    AiScheduleRequest,
    _course_code_matches,
    _course_owner_for_search,
    _index_rows,
    _match_courses,
    _published_code_matches,
    _search_fold,
    _short_code,
)
from app.campus import departments


def _course(code: str, full_code: str) -> dict:
    return {"code": code, "full_code": full_code, "department": "CENG"}


def _indexed(*rows: tuple[str, str], abbreviation: str = "CENG"):
    """The same (haystack, course) pairs both search paths are handed."""
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


def test_both_search_paths_answer_every_code_spelling():
    """Flag state must not change which spellings work.

    The published path and the legacy path are the same lookup, so each is
    driven through the same matcher and the same index rows here.
    """
    owner, indexed = _indexed(("5710331", "COMPUTER ORGANIZATION"))
    for digits in ("331", "5710331", "571"):
        published = _published_code_matches(indexed, named=owner, digits=digits, home=owner)
        legacy = _match_courses(
            {"courses": [{"course_code": "5710331", "name": "COMPUTER ORGANIZATION"}]},
            owner,
            digits=digits,
            title="",
        )
        assert [course["code"] for course in published] == ["CENG331"], digits
        assert [course["code"] for course in legacy] == ["CENG331"], digits
    assert _published_code_matches(indexed, named=None, digits="332", home=None) == []
    assert _match_courses({"courses": [{"course_code": "5710331", "name": "X"}]}, owner, digits="332", title="") == []


def test_legacy_full_code_search_uses_the_code_owner_not_the_home_department():
    ceng = departments.resolve("CENG")
    math = departments.resolve("MATH")
    assert _course_owner_for_search(None, ceng, "2360119").code == math.code
    assert _course_owner_for_search(None, ceng, "119").code == ceng.code


async def test_legacy_full_code_search_fetches_the_owning_department(monkeypatch):
    import app.api.v1.schedule as schedule

    calls = []

    @asynccontextmanager
    async def catalog_session(_db, _user_id):
        yield object()

    async def call_course_info(_db, _user_id, tool, values, *, session=None):
        calls.append((tool, values, session))
        return {"courses": [{"course_code": "2360119", "name": "Calculus I"}]}

    monkeypatch.setattr(schedule, "published_catalog_reads_enabled", lambda: False)
    monkeypatch.setattr(schedule, "catalog_session", catalog_session)
    monkeypatch.setattr(schedule, "call_course_info", call_course_info)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(department="CENG", program_code=None)))
    user = SimpleNamespace(id="user-1")

    result = await schedule.search_courses(query="2360119", semester="20261", user=user, db=db)

    assert calls[0][1] == {"department": "236", "semester": "20261"}
    assert result["courses"][0]["code"] == "MATH119"


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
    wanted = _search_fold("muhendislik")
    assert wanted in haystack
    matches = _match_courses(
        {"courses": [{"course_code": "5710331", "name": "BİLGİSAYAR MÜHENDİSLİĞİ"}]},
        owner,
        digits="",
        title="muhendislik",
    )
    assert [course["code"] for course in matches] == ["CENG331"]


def test_department_search_lists_directory_matches_the_source_misses():
    """The source matches codes and English names; the directory does the rest.

    An ambiguous name lists every candidate rather than resolving to one:
    "Bilgisayar" is both Computer Engineering and Computer Education.
    """
    from app.api.v1.schedule import _directory_department_options

    assert any(option["code"] == "571" for option in _directory_department_options("CENG"))
    bilgisayar = {option["code"] for option in _directory_department_options("Bilgisayar")}
    assert {"571", "430"} <= bilgisayar

"""Search has to answer the forms a student actually types.

Reported from the schedule page: typing a course code returned nothing while the
same course was found by its title. The published search compared only the last
four digits of the stored code, so a pasted seven-digit code matched nothing,
and it required the typed text to appear inside the folded haystack - which is
"ceng331 computer organization ..." with no space, so "CENG 331" matched nothing
either. The title path worked, which is what made it look like the catalog was
missing the course.
"""

from app.api.v1.schedule import (
    AiScheduleRequest,
    _course_code_matches,
    _index_rows,
    _legacy_code_owner,
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
    legacy_payload = {"courses": [{"course_code": "5710331", "name": "COMPUTER ORGANIZATION"}]}
    for digits in ("331", "5710331", "571"):
        published = _published_code_matches(indexed, named=owner, digits=digits, home=owner)
        legacy = _match_courses(legacy_payload, owner, digits=digits, title="")
        assert [course["code"] for course in published] == ["CENG331"], digits
        assert [course["code"] for course in legacy] == ["CENG331"], digits
    assert _published_code_matches(indexed, named=None, digits="332", home=None) == []
    assert _match_courses({"courses": [{"course_code": "5710331", "name": "X"}]}, owner, digits="332", title="") == []


def test_a_seven_digit_code_names_its_own_department():
    """The legacy listing is fetched for the code's department, not the student's.

    An EE student searching 5710331 used to read EE's listing for a CENG course
    and find nothing; a student with no saved context got a 422.
    """
    ceng = departments.resolve("CENG")
    ee = departments.resolve("EE")
    assert _legacy_code_owner(None, ee, "5710331") is ceng
    assert _legacy_code_owner(None, None, "5710331") is ceng
    assert _legacy_code_owner(None, ee, "331") is ee
    assert _legacy_code_owner(None, None, "331") is None
    assert _legacy_code_owner(ceng, ee, "331") is ceng


def test_the_curriculum_request_accepts_the_alternate_spellings():
    parsed = AiScheduleRequest.model_validate({"term": "20261", "course_codes": [{"code": "EE201"}]})
    assert parsed.semester == "20261"
    assert [course.code for course in parsed.courses] == ["EE201"]


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

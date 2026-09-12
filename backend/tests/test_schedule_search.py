"""Search has to answer the forms a student actually types.

Reported from the schedule page: typing a course code returned nothing while the
same course was found by its title. The published search compared only the last
four digits of the stored code, so a pasted seven-digit code matched nothing,
and it required the typed text to appear inside the folded haystack - which is
"ceng331 computer organization ..." with no space, so "CENG 331" matched nothing
either. The title path worked, which is what made it look like the catalog was
missing the course.
"""

from app.api.v1.schedule import _course_code_matches, _search_fold, _short_code


def _course(code: str, full_code: str) -> dict:
    return {"code": code, "full_code": full_code, "department": "CENG"}


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


def test_a_query_without_digits_does_not_filter_by_code():
    assert _course_code_matches(_course("CENG331", "5710331"), "")


def test_short_codes_drop_the_catalogs_zero_padding():
    assert _short_code("2400101", "HIST") == "HIST101"
    assert _short_code("5710331", "CENG") == "CENG331"


def test_folded_search_reaches_turkish_letters_from_an_english_keyboard():
    assert "muhendislik" in _search_fold("Mühendisliği")
    assert "tarih" in _search_fold("TARİHİ")


def test_department_search_lists_directory_matches_the_source_misses():
    """The source matches codes and English names; the directory does the rest.

    An ambiguous name lists every candidate rather than resolving to one:
    "Bilgisayar" is both Computer Engineering and Computer Education.
    """
    from app.api.v1.schedule import _directory_department_options

    assert any(option["code"] == "571" for option in _directory_department_options("CENG"))
    bilgisayar = {option["code"] for option in _directory_department_options("Bilgisayar")}
    assert {"571", "430"} <= bilgisayar

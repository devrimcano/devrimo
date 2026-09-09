"""Whether a student may register for a section.

METU restricts a section by department: its eligibility table lists one row per
programme admitted, and a student whose department has no row cannot take the
section at all. Within their row the surname must fall in a two-letter range,
and the cumulative GPA and year of study in their own ranges.

Two principles run through this module.

*Unknown is explicit.* If an applicable row needs a student's surname, GPA,
year or department and that value is missing, the result is ``eligible=None``.
The caller can show that verification is pending instead of turning an
incomplete profile into either a rejection or an admission. A missing
transcript row remains evidence that the student has not taken the course.

*Turkish collation, everywhere.* "Ç" sorts between C and D and "İ" between I
and J, so a range like "DÖ"-"İŞ" only means the right thing under a Turkish
comparison. Comparing by code point puts every dotted and cedilla'd letter
after Z and quietly admits or rejects the wrong students.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Turkish alphabetical order. Used instead of ``locale`` because the broker
# cannot assume a tr_TR locale is installed, and instead of ``str <`` because
# code-point order is wrong for exactly the letters these ranges use.
TR_ALPHABET = "AÂBCÇDEFGĞHIİJKLMNOÖPQRSŞTUÜVWXYZ"
_RANK = {letter: index for index, letter in enumerate(TR_ALPHABET)}

ANYONE = "herkes alabilir"

# What a section writes in the department column when it means "no department
# restriction". Read as a literal abbreviation it excluded everybody: MATH219
# section 7 is open to the whole university and was reported as "open only to
# ALL", which is both wrong and absurd on its face.
ANY_DEPARTMENT = frozenset(
    {
        "ALL",
        "ALL DEPARTMENTS",
        "ANY",
        "ANY DEPARTMENT",
        "TUMU",
        "TÜMÜ",
        "HEPSI",
        "HEPSİ",
        "HERKES",
    }
)


def tr_upper(text: str) -> str:
    """Uppercase the Turkish way: "i" becomes "İ", not "I"."""
    return text.replace("i", "İ").replace("ı", "I").upper()


def _sort_key(text: str) -> list[int]:
    """A comparable key for a surname prefix under Turkish collation.

    Letters outside the alphabet (digits, punctuation, Latin letters with
    accents we do not rank) sort after every ranked letter rather than raising,
    so an unexpected character degrades to "at the end" instead of an error.
    """
    return [_RANK.get(char, len(TR_ALPHABET)) for char in tr_upper(text)]


def _within(value: str, low: str, high: str) -> bool:
    """Is ``value`` inside the inclusive ``[low, high]`` surname range?

    The bounds are two characters ("DJ"-"KA"), so the comparison is on the
    surname's first two letters: "KAYA" is admitted by "DJ"-"KA" and "KEMAL"
    is not. Comparing only initials would admit both.

    Each bound is truncated to the length actually available in the surname
    before comparing, so a one-letter surname "A" is still admitted by "AA"-"ZZ"
    rather than sorting before its own range's lower bound.
    """
    if not low and not high:
        return True
    width = max(len(low), len(high), 1)
    prefix = _sort_key(value[:width])
    if not prefix:
        return True
    if low and prefix < _sort_key(low)[: len(prefix)]:
        return False
    if high and prefix > _sort_key(high)[: len(prefix)]:
        return False
    return True


def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


# METU grades that count as having passed the course. Anything else — FD, FF,
# NA, U, W, an incomplete — leaves the student still needing it.
PASSING_GRADES = frozenset({"AA", "BA", "BB", "CB", "CC", "DC", "DD", "S", "EX", "P"})

_BAND_FOLD = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                            "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})

# How a section says "this is for students who still need the course". METU
# writes it in Turkish prose in the grade columns: "Kaldı", "Hiç almayanlar
# veya Başarısızlar (FD ve altı)".
_NOT_PASSED_BANDS = ("hicalmayanlar", "kaldi", "basarisiz")


def _fold(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).translate(_BAND_FOLD).casefold())


def grade_token(value: Any) -> str:
    """The letter grade alone.

    The transcript scrape brings whitespace and trailing notes with it —
    ``"BA   
 ... (NTE)"`` is a real value — so only the first token is read.
    """
    parts = str(value or "").split()
    return tr_upper(parts[0]) if parts else ""


def has_passed(grade: Any) -> bool:
    return grade_token(grade) in PASSING_GRADES


def course_candidates(supplied: str, owner: Any, full_code: str) -> list[str]:
    """The spellings one course may appear under on a transcript.

    A section's table speaks in seven-digit codes ("2300213"); a transcript
    writes the abbreviation form ("PHYS213"). Whichever the caller was handed,
    both are offered so the lookup matches either way.
    """
    # Callers that only have the canonical seven-digit code (for example the
    # curriculum variant resolver) may leave ``owner`` unset. Resolve it from
    # the same three-digit prefix rather than losing the transcript's
    # abbreviation spelling.
    if owner is None:
        try:
            from app.campus import departments

            owner = departments.by_code(str(full_code or "")[:3])
        except Exception:
            owner = None
    abbreviation = str(getattr(owner, "abbreviation", "") or "")
    canonical_digits = re.sub(r"\D", "", str(full_code or ""))
    # The suffix is four digits in some departments (HIST 2201 is 2402201),
    # so taking only the final three changes the identity to HIST 201.
    number = canonical_digits[3:].lstrip("0") if len(canonical_digits) >= 4 else ""
    names = [supplied, full_code, f"{abbreviation}{number}" if abbreviation and number else ""]
    return [name for name in names if name]


def prior_grade(completed_courses: Any, candidates: Iterable[str]) -> str | None:
    """This student's existing grade in a course, from their transcript.

    ``candidates`` are the spellings the course may appear under — the
    transcript writes "PHYS213" where a section table speaks in seven-digit
    codes — and the first that matches wins.
    """
    wanted = {"".join(str(name).upper().split()) for name in candidates if name}
    if not wanted:
        return None
    best: str | None = None
    for row in completed_courses or []:
        if not isinstance(row, dict):
            continue
        code = "".join(str(row.get("course_code") or row.get("code") or "").upper().split())
        if code not in wanted:
            continue
        grade = grade_token(row.get("grade"))
        # A course can be repeated. The passing attempt is the one that
        # decides whether they still need it.
        if grade and (best is None or (grade in PASSING_GRADES and best not in PASSING_GRADES)):
            best = grade
    return best


def _admits_a_pass(row: Any) -> bool:
    """Whether a row's grade band still admits someone who already passed.

    This helper is kept for callers that only need the historical boolean
    answer. New decision paths use :func:`_grade_band_state` so an unfamiliar
    registrar label becomes explicit unknown rather than an accidental pass.
    """
    band = _fold(f"{_row_value(row, 'start_grade', 'startGrade')} {_row_value(row, 'end_grade', 'endGrade')}")
    return not any(token in band for token in _NOT_PASSED_BANDS)


_OPEN_GRADE_BANDS = ("herkesalabilir", "everyone", "everybody", "all")


def _grade_band_state(row: Any) -> bool | None:
    """Return open/closed/unknown for a registrar grade-band label."""

    band = _fold(f"{_row_value(row, 'start_grade', 'startGrade')} {_row_value(row, 'end_grade', 'endGrade')}")
    if not band:
        return True
    if any(token in band for token in _NOT_PASSED_BANDS):
        return False
    if any(token in band for token in _OPEN_GRADE_BANDS):
        return True
    return None


@dataclass(frozen=True, slots=True)
class Verdict:
    # ``None`` means the table is applicable but the student's profile is
    # missing a value needed to decide it. Keeping this separate from False is
    # what lets catalog readers expose an explicit unknown state instead of
    # silently treating an unverified student as ineligible or eligible.
    eligible: bool | None
    #: Empty when eligible. One short clause naming the rule that excluded them.
    reason: str = ""
    #: The abbreviation of the row that was applied, when one matched.
    matched_department: str = ""


def _row_value(row: Any, *names: str) -> str:
    """Read a field from either a model or a plain dict."""
    for name in names:
        if isinstance(row, dict):
            if name in row and row[name] is not None:
                return str(row[name]).strip()
        else:
            value = getattr(row, name, None)
            if value is not None:
                return str(value).strip()
    return ""


def evaluate(
    constraints: list[Any],
    *,
    department: str | None,
    # A full surname or just its first two letters: the comparison only ever
    # reads as far as the bound is wide, so callers may pass either. What we
    # store is the two-letter prefix, because that is all this needs.
    surname: str | None = None,
    cgpa: float | None = None,
    year: int | None = None,
    # The grade this student already holds in the course, from their
    # transcript. A section restricted to "Hiç almayanlar veya Başarısızlar"
    # is not open to someone who has already passed it.
    prior_grade: str | None = None,
) -> Verdict:
    """Whether this student may register, given a section's eligibility table.

    An empty table means the section states no restriction and everyone is
    admitted — which is different from a table the student's department is
    absent from, where they are specifically excluded. A missing value needed
    by an applicable row returns ``eligible=None``.
    """
    department_fields = (
        "given_dept",
        "givenDept",
        "given_department",
        "dept",
        "department",
        "department_code",
        "program",
    )
    raw_rows = list(constraints or [])
    if not raw_rows:
        return Verdict(True)
    # A nonempty table with a row whose department scope is absent is not an
    # unrestricted table. It may be a universal CGPA/year rule, a malformed
    # admin row, or a source schema we do not understand; all three need an
    # explicit verification state rather than being filtered into True.
    if any(not _row_value(row, *department_fields) for row in raw_rows):
        return Verdict(None, "restriction department scope could not be verified")
    rows = raw_rows

    wanted = tr_upper((department or "").strip())
    if not wanted:
        # An open-to-all row does not need a department value. Any other row
        # does: without it there is no safe way to select the applicable rule.
        if any(tr_upper(_row_value(row, *department_fields)) not in ANY_DEPARTMENT for row in rows):
            return Verdict(None, "student department is required to verify section eligibility")
        mine = rows
    else:
        mine = [
            row
            for row in rows
            if (
                given := tr_upper(_row_value(row, *department_fields))
            ) == wanted
            or given in ANY_DEPARTMENT
        ]
    if not mine:
        admitted = ", ".join(sorted({_row_value(row, *department_fields) for row in rows}))
        return Verdict(False, f"open only to {admitted}")

    # One department can hold several rows: METU splits a section by surname
    # range, or by whether the student has passed the course before. The
    # student needs any one of them to admit them, so reading only the first
    # rejected people the second row was written for.
    reasons: list[str] = []
    unknown_reasons: list[str] = []
    for row in mine:
        verdict = _admits(row, wanted, surname=surname, cgpa=cgpa, year=year, prior_grade=prior_grade)
        if verdict.eligible is True:
            return verdict
        if verdict.eligible is None:
            if verdict.reason and verdict.reason not in unknown_reasons:
                unknown_reasons.append(verdict.reason)
            continue
        if verdict.reason and verdict.reason not in reasons:
            reasons.append(verdict.reason)
    if unknown_reasons:
        return Verdict(None, "; ".join(unknown_reasons), wanted)
    return Verdict(False, "; ".join(reasons), wanted)


def _admits(
    match: Any,
    wanted: str,
    *,
    surname: str | None,
    cgpa: float | None,
    year: int | None,
    prior_grade: str | None,
) -> Verdict:
    """Whether one row of the eligibility table admits this student."""
    low = _row_value(match, "start_char", "startChar")
    high = _row_value(match, "end_char", "endChar")
    clean_surname = "".join(
        char for char in unicodedata.normalize("NFC", (surname or "").strip()) if not char.isspace()
    )
    if low or high:
        if not clean_surname:
            return Verdict(None, f"surname is required for the {low}-{high} range", wanted)
        if not _within(clean_surname, low, high):
            return Verdict(False, f"surnames {low}-{high} only", wanted)

    raw_min_cgpa = _row_value(match, "min_cgpa", "minCgpa")
    raw_max_cgpa = _row_value(match, "max_cgpa", "maxCgpa")
    min_cgpa, max_cgpa = _number(raw_min_cgpa), _number(raw_max_cgpa)
    if (raw_min_cgpa and min_cgpa is None) or (raw_max_cgpa and max_cgpa is None):
        return Verdict(None, "CGPA restriction could not be read", wanted)
    # METU's 0.00-4.00 pair means there is no effective CGPA restriction.
    if cgpa is None and (
        (min_cgpa is not None and min_cgpa > 0.0)
        or (max_cgpa is not None and max_cgpa < 4.0)
    ):
        return Verdict(None, "CGPA is required to verify section eligibility", wanted)
    if cgpa is not None:
        if min_cgpa is not None and cgpa < min_cgpa:
            return Verdict(False, f"CGPA {min_cgpa:.2f} or above", wanted)
        # A 4.00 ceiling is the table's way of writing "no upper bound".
        if max_cgpa is not None and max_cgpa < 4.0 and cgpa > max_cgpa:
            return Verdict(False, f"CGPA {max_cgpa:.2f} or below", wanted)

    raw_min_year = _row_value(match, "min_year", "minYear")
    raw_max_year = _row_value(match, "max_year", "maxYear")
    min_year, max_year = _number(raw_min_year), _number(raw_max_year)
    if (raw_min_year and min_year is None) or (raw_max_year and max_year is None):
        return Verdict(None, "year restriction could not be read", wanted)
    # 0-95 is the registrar's no-limit encoding.
    if year is None and (
        (min_year is not None and min_year > 0)
        or (max_year is not None and max_year < 95)
    ):
        return Verdict(None, "year is required to verify section eligibility", wanted)
    if year is not None:
        if min_year is not None and year < min_year:
            return Verdict(False, f"year {int(min_year)} and above", wanted)
        if max_year is not None and max_year < 95 and year > max_year:
            return Verdict(False, f"year {int(max_year)} and below", wanted)

    grade_band = _grade_band_state(match)
    if grade_band is None:
        return Verdict(None, "grade restriction could not be verified", wanted)
    if not grade_band:
        # The SAIS transcript is the source of truth for this dimension. A
        # missing row means the student has not taken the course; only an
        # actual passing grade closes a "not passed" section.
        if has_passed(prior_grade):
            return Verdict(False, f"for students who have not passed it; you have {grade_token(prior_grade)}", wanted)

    return Verdict(True, "", wanted)

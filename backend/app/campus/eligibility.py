"""Whether a student may register for a section.

METU restricts a section by department: its eligibility table lists one row per
programme admitted, and a student whose department has no row cannot take the
section at all. Within their row the surname must fall in a two-letter range,
and the cumulative GPA and year of study in their own ranges.

Two principles run through this module.

*Unknown never excludes.* If we do not know the student's surname, GPA or year,
that dimension is not checked. The cost of a false "you cannot take this" is a
student steered away from a section they were entitled to, which they will
never discover; the cost of a false "you can" is a registration error they see
immediately and can act on.

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
ANY_DEPARTMENT = frozenset({"ALL", "TUMU", "TÜMÜ", "HEPSI", "HEPSİ", "HERKES"})


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
    abbreviation = str(getattr(owner, "abbreviation", "") or "")
    number = re.sub(r"\D", "", str(full_code or ""))[-3:]
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

    Unrecognised wording admits, on the module's standing principle: the cost
    of wrongly excluding a student is one they never find out about.
    """
    band = _fold(f"{_row_value(row, 'start_grade', 'startGrade')} {_row_value(row, 'end_grade', 'endGrade')}")
    return not any(token in band for token in _NOT_PASSED_BANDS)


@dataclass(frozen=True, slots=True)
class Verdict:
    eligible: bool
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
    absent from, where they are specifically excluded.
    """
    rows = [row for row in constraints or [] if _row_value(row, "given_dept", "givenDept", "dept")]
    if not rows:
        return Verdict(True)

    wanted = tr_upper((department or "").strip())
    if not wanted:
        # We do not know their department, so we cannot apply the table at all.
        return Verdict(True)

    mine = [
        row
        for row in rows
        if (given := tr_upper(_row_value(row, "given_dept", "givenDept", "dept"))) == wanted
        or given in ANY_DEPARTMENT
    ]
    if not mine:
        admitted = ", ".join(sorted({_row_value(row, "given_dept", "givenDept", "dept") for row in rows}))
        return Verdict(False, f"open only to {admitted}")

    # One department can hold several rows: METU splits a section by surname
    # range, or by whether the student has passed the course before. The
    # student needs any one of them to admit them, so reading only the first
    # rejected people the second row was written for.
    reasons: list[str] = []
    for row in mine:
        verdict = _admits(row, wanted, surname=surname, cgpa=cgpa, year=year, prior_grade=prior_grade)
        if verdict.eligible:
            return verdict
        if verdict.reason and verdict.reason not in reasons:
            reasons.append(verdict.reason)
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
    if clean_surname and (low or high) and not _within(clean_surname, low, high):
        return Verdict(False, f"surnames {low}-{high} only", wanted)

    min_cgpa, max_cgpa = _number(_row_value(match, "min_cgpa", "minCgpa")), _number(_row_value(match, "max_cgpa", "maxCgpa"))
    if cgpa is not None:
        if min_cgpa is not None and cgpa < min_cgpa:
            return Verdict(False, f"CGPA {min_cgpa:.2f} or above", wanted)
        # A 4.00 ceiling is the table's way of writing "no upper bound".
        if max_cgpa is not None and max_cgpa < 4.0 and cgpa > max_cgpa:
            return Verdict(False, f"CGPA {max_cgpa:.2f} or below", wanted)

    min_year, max_year = _number(_row_value(match, "min_year", "minYear")), _number(_row_value(match, "max_year", "maxYear"))
    if year is not None:
        if min_year is not None and year < min_year:
            return Verdict(False, f"year {int(min_year)} and above", wanted)
        if max_year is not None and max_year < 90 and year > max_year:
            return Verdict(False, f"year {int(max_year)} and below", wanted)

    if prior_grade and has_passed(prior_grade) and not _admits_a_pass(match):
        return Verdict(False, f"for students who have not passed it; you have {grade_token(prior_grade)}", wanted)

    return Verdict(True, "", wanted)

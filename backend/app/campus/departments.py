"""Lookups over the generated department directory.

Bridges the three names METU uses for the same department — the three-digit
code, the abbreviation a section's eligibility table admits, and the name the
SAIS student card reports. See ``scripts/build_department_directory.py`` for
where the data comes from and why it is generated rather than fetched live.

Every resolution here refuses to guess. An ambiguous name returns ``None``,
because the failure mode of guessing is not an error message — it is a
confidently wrong department, and downstream that becomes a student being told
they are eligible for a section they cannot register for.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).with_name("departments.json")


@dataclass(frozen=True, slots=True)
class Department:
    code: str
    abbreviation: str
    name_en: str
    name_tr: str

    @property
    def name(self) -> str:
        return self.name_en or self.name_tr


def _normalize(text: str) -> str:
    """Casefold for comparison, Turkish included.

    ``str.casefold`` maps "I" to "i" but leaves "İ" as "i̇" (i plus a combining
    dot), so the dotted capital has to be folded explicitly or "İŞLETME" never
    matches "İşletme". Punctuation goes too, so "Public Adm." can meet "Public
    Administration" halfway.
    """
    text = text.replace("İ", "i").replace("I", "ı").replace("ı", "i")
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


@lru_cache(maxsize=1)
def _directory() -> tuple[Department, ...]:
    try:
        payload = json.loads(_DATA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    return tuple(
        Department(
            code=str(row.get("code", "")).strip(),
            abbreviation=str(row.get("abbreviation", "")).strip().upper(),
            name_en=str(row.get("name_en", "")).strip(),
            name_tr=str(row.get("name_tr", "")).strip(),
        )
        for row in payload.get("departments", [])
        if str(row.get("code", "")).strip()
    )


def all_departments() -> tuple[Department, ...]:
    return _directory()


@lru_cache(maxsize=1)
def _by_code() -> dict[str, Department]:
    return {item.code: item for item in _directory()}


@lru_cache(maxsize=1)
def _by_abbreviation() -> dict[str, Department]:
    found: dict[str, Department] = {}
    for item in _directory():
        if item.abbreviation:
            found.setdefault(item.abbreviation, item)
    return found


def by_code(code: str | None) -> Department | None:
    return _by_code().get((code or "").strip())


def by_abbreviation(abbreviation: str | None) -> Department | None:
    return _by_abbreviation().get((abbreviation or "").strip().upper())


def by_name(name: str | None) -> Department | None:
    """The department a SAIS name refers to, or ``None`` if it is not clear.

    SAIS reports either half of ``English/Türkçe`` and sometimes an abbreviated
    form ("Political Science and Public Adm."), so exact, prefix and containment
    matches are tried in that order — and each is accepted only when exactly one
    department satisfies it.
    """
    wanted = _normalize(name or "")
    if not wanted:
        return None

    # A bare "English/Türkçe" pair is the common case; try each half too.
    candidates = [wanted]
    if "/" in (name or ""):
        candidates += [_normalize(part) for part in name.split("/") if part.strip()]

    for candidate in candidates:
        if not candidate:
            continue
        for match in (
            lambda a, b: a == b,
            lambda a, b: a.startswith(b) or b.startswith(a),
            lambda a, b: b in a,
        ):
            hits = {
                item.code: item
                for item in _directory()
                for field in (item.name_en, item.name_tr)
                if field and match(_normalize(field), candidate)
            }
            if len(hits) == 1:
                return next(iter(hits.values()))
    return None


# "PHYS213", "MATH 260", "CENG140": a department abbreviation followed by a
# course number. Both halves are digits-and-letters, so which half means what
# has to be decided by shape, not by stripping.
LETTERED_COURSE = re.compile(r"^\s*([A-Za-zÇĞİÖŞÜçğıöşü]{2,6})\s*(\d{3,4})\s*$")


def resolve(value: str | None) -> Department | None:
    """Whatever we happen to hold — code, abbreviation, name or course code."""
    text = (value or "").strip()
    if not text:
        return None
    # Tried before the digits, and this order is load-bearing: stripping the
    # letters out of "PHYS213" leaves "213", which is three digits and reads as
    # a department code. There is no department 213, so the lookup returned
    # nothing and Physics was never consulted.
    lettered = LETTERED_COURSE.match(text)
    if lettered and (found := by_abbreviation(lettered.group(1))):
        return found
    digits = re.sub(r"\D", "", text)
    # A seven-digit course code carries its owning department in the first three.
    if len(digits) == 7:
        return by_code(digits[:3])
    if len(digits) == 3:
        return by_code(digits)
    return by_abbreviation(text) or by_name(text)


def expand_course_code(value: str | None) -> tuple[str, Department] | None:
    """``"PHYS213"`` -> ``("2300213", Physics)``.

    A METU course code is its owning department's three digits followed by a
    four-digit course number, and the catalog only accepts that numeric form —
    so a student saying "PHYS 213" has to be turned into 2300213 before
    anything can be looked up. Returns ``None`` when the department cannot be
    identified, rather than guessing a prefix.
    """
    text = (value or "").strip()
    if not text:
        return None
    lettered = LETTERED_COURSE.match(text)
    if lettered:
        department = by_abbreviation(lettered.group(1))
        return (f"{department.code}{lettered.group(2).zfill(4)}", department) if department else None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 7:
        department = by_code(digits[:3])
        return (digits, department) if department else None
    return None

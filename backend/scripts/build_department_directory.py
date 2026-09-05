"""Regenerate ``app/campus/departments.json`` from METU's public pages.

Why this exists
---------------
Three identifiers name the same department and no single METU system publishes
all three:

* the **three-digit code** (``236``) — what SAIS uses, and what the first three
  digits of every course code mean;
* the **abbreviation** (``MATH``) — what a section's eligibility table calls the
  departments it admits;
* the **name** (``Mathematics/Matematik``) — what the SAIS student card reports.

A section's eligibility table keys on the abbreviation while the student context
we store carries the name, so without a bridge between them we cannot tell
whether a student may take a section at all.

Two sources, because neither is complete
----------------------------------------
* ``oibs3.metu.edu.tr/View_Program_Course_Details_64`` is the public face of the
  same system the campus servers read. Its department dropdown is authoritative
  and complete (~207 entries, graduate programmes included) and its names are in
  the exact ``English/Türkçe`` form the SAIS student card uses — which is what
  makes matching a stored context to a row possible at all. It does not publish
  abbreviations.
* ``catalog.metu.edu.tr`` never states an abbreviation either, but it lists each
  department's courses under their letter prefix, so the abbreviation is read
  off the courses. It only covers programmes that publish a course list.

What is left over is a handful of departments that teach under a prefix neither
page states. Those live in ``ABBREVIATION_OVERRIDES`` — deliberately small,
individually justified, and printed in the summary so the list cannot quietly
grow into a hand-maintained copy of the whole directory.

Why a generated file rather than a live lookup
----------------------------------------------
This mapping changes when a department opens or closes — roughly once a year.
Scraping two third-party sites on a request path to answer a question whose
answer changes annually would be a liability. So it is generated deliberately,
committed, reviewable in a diff, and loads with no network at all.

Usage
-----
    python scripts/build_department_directory.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

CATALOG = "https://catalog.metu.edu.tr"
SAIS_PUBLIC = "http://oibs3.metu.edu.tr/View_Program_Course_Details_64/main.php"

COURSE_CODE = re.compile(r"^([A-ZÇĞİÖŞÜ]{2,6})\s*\d{3,4}$")
FAC_LINK = re.compile(r"fac_inst=(\d+)")
PROG_LINK = re.compile(r"fac_prog=(\d+)")

# Departments that teach under a prefix neither public page states. Each one is
# a programme whose catalog entry publishes no course list, confirmed against a
# real section eligibility table that named the abbreviation.
ABBREVIATION_OVERRIDES: dict[str, str] = {
    "413": "EME",   # Elementary Mathematics Educ.
    "412": "ESME",  # Elementary Science and Mathematics Educ.
    "421": "PHYE",  # Physics Education
    "422": "CHME",  # Chemistry Education
    "423": "MATE",  # Mathematics Education
    "453": "PHED",  # Physical Education and Sports
    "909": "MMI",   # Multimedia Informatics (Informatics Institute)
    "973": "FM",    # Financial Mathematics (Applied Mathematics Institute)
}


# A run of UTF-8 bytes that were decoded as Latin-1: a lead byte in C2-DF
# followed by continuation bytes in 80-BF.
MOJIBAKE_RUN = re.compile("[\\u00c2-\\u00df][\\u0080-\\u00bf]+")


def _repair_mojibake(text: str) -> str:
    """Undo UTF-8 that was encoded a second time, run by run.

    SAIS serves these names double-encoded, so "ü" (``C3 BC``) arrives as
    ``C3 83 C2 BC`` and a correct UTF-8 decode still yields "Ã¼". The extra
    layer only comes off by encoding back to Latin-1 and decoding as UTF-8
    again — but the strings are *mixed*: an NCC department reads
    "Kimya MÃ¼hendisliÄi(Kuzey Kıbrıs Kampüsü)", where the parenthetical is
    already correct and contains "ı", which no single-byte charset can encode.
    Round-tripping the whole string therefore fails and repairs nothing, so
    each damaged run is repaired on its own and the healthy text is left alone.
    """

    def fix(match: re.Match[str]) -> str:
        try:
            return match.group(0).encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return match.group(0)

    for _ in range(3):
        repaired = MOJIBAKE_RUN.sub(fix, text)
        if repaired == text:
            break
        text = repaired
    return text


# Northern Cyprus programmes duplicate an Ankara programme's English name
# exactly, and only the Turkish name says which is which. They are dropped
# rather than disambiguated: this planner serves the Ankara campus, and keeping
# them would mean every common department name resolved to two rows.
NCC_MARKER = re.compile(r"\(\s*Kuzey\s*Kıbrıs\s*Kampüsü\s*\)", re.IGNORECASE)
# International joint programmes ("Uluslararası Ortak Program") likewise share
# their parent's English name and are not what a schedule is planned against.
UOP_MARKER = re.compile(r"uluslararası\s*ortak\s*program|international\s*joint\s*program", re.IGNORECASE)
# The Türkiye-Azerbaijan University programme is a second "Computer
# Engineering" taught elsewhere, for the same reason.
TAU_MARKER = re.compile(r"\bTAU\b|Türkiye-?Azerbaycan|Türkiye-?Azerbaijan", re.IGNORECASE)

# Graduate and interdisciplinary programmes (the 8xx and 9xx codes) are kept
# deliberately, even though almost none of them carry an abbreviation and so
# none can appear in a section's eligibility table. They earn their place in
# course-code resolution: a seven-digit code names its owner in the first three
# digits, so without these rows "8670101" resolves to nothing instead of to
# Software Engineering.


def _is_excluded(name_en: str, name_tr: str) -> bool:
    """Everything that is not a plain Ankara-campus programme.

    All three kinds duplicate an Ankara department's English name exactly, so
    keeping them means a common name resolves to two or three rows and no
    lookup can pick between them without guessing.
    """
    joined = f"{name_en} {name_tr}"
    return bool(NCC_MARKER.search(joined) or UOP_MARKER.search(joined) or TAU_MARKER.search(joined))


def _soup(response: httpx.Response) -> BeautifulSoup:
    """Parse as UTF-8. Mojibake is repaired per string, not here.

    Repairing the whole document is all-or-nothing: the round trip has to
    encode every character back to a single-byte charset, so one character
    anywhere in 40KB that cp1252 cannot represent silently abandons the repair
    for the entire page. Names are repaired individually instead, where a
    failure costs one name rather than all of them.
    """
    for encoding in ("utf-8", "windows-1254", "iso-8859-9"):
        try:
            return BeautifulSoup(response.content.decode(encoding), "html.parser")
        except (UnicodeDecodeError, LookupError):
            continue
    return BeautifulSoup(response.content.decode("utf-8", "replace"), "html.parser")


def _clean(text: str) -> str:
    return _repair_mojibake(" ".join(text.split()))


async def _get(client: httpx.AsyncClient, url: str) -> BeautifulSoup | None:
    try:
        response = await client.get(url, timeout=30)
        response.raise_for_status()
    except Exception as exc:
        print(f"  ! {url}: {exc}", file=sys.stderr)
        return None
    return _soup(response)


async def sais_departments(client: httpx.AsyncClient) -> dict[str, str]:
    """Every department SAIS knows, as ``{code: "English/Türkçe"}``."""
    soup = await _get(client, SAIS_PUBLIC)
    if soup is None:
        return {}
    select = soup.find("select", {"name": "select_dept"})
    if select is None:
        print("  ! department dropdown not found", file=sys.stderr)
        return {}
    found: dict[str, str] = {}
    for option in select.find_all("option"):
        code = (option.get("value") or "").strip()
        name = _clean(option.get_text(" ", strip=True))
        if code.isdigit() and name:
            found[code] = name
    return found


async def catalog_abbreviations(client: httpx.AsyncClient) -> dict[str, tuple[str, int]]:
    """``{code: (abbreviation, how many courses agreed)}`` from the catalog."""
    index = await _get(client, f"{CATALOG}/index.php")
    faculties = sorted({m.group(1) for a in index.find_all("a", href=True) if (m := FAC_LINK.search(a["href"]))}) if index else []
    print(f"{len(faculties)} faculties in the catalog", file=sys.stderr)

    codes: set[str] = set()
    for faculty in faculties:
        page = await _get(client, f"{CATALOG}/fac_inst.php?fac_inst={faculty}")
        if page is None:
            continue
        found = {m.group(1) for a in page.find_all("a", href=True) if (m := PROG_LINK.search(a["href"]))}
        codes |= found
        print(f"  fac {faculty}: {len(found)} programmes", file=sys.stderr)

    result: dict[str, tuple[str, int]] = {}
    for code in sorted(codes):
        page = await _get(client, f"{CATALOG}/prog_courses.php?prog={code}")
        if page is None:
            continue
        prefixes: Counter[str] = Counter()
        for cell in page.find_all(["td", "a", "li"]):
            match = COURSE_CODE.match(" ".join(cell.get_text(" ", strip=True).split()))
            if match:
                prefixes[match.group(1)] += 1
        if prefixes:
            top, count = prefixes.most_common(1)[0]
            result[code] = (top, count)
    return result


def _split_name(name: str) -> tuple[str, str]:
    """``"Mathematics/Matematik"`` -> ``("Mathematics", "Matematik")``."""
    english, _, turkish = name.partition("/")
    return english.strip(), turkish.strip()


async def build() -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "DevrimoCatalogIndexer/1.0"}) as client:
        names = await sais_departments(client)
        print(f"{len(names)} departments from SAIS", file=sys.stderr)
        abbreviations = await catalog_abbreviations(client)
        print(f"{len(abbreviations)} abbreviations from the catalog", file=sys.stderr)

    rows: list[dict] = []
    skipped = 0
    for code in sorted(names, key=int):
        english, turkish = _split_name(names[code])
        if _is_excluded(english, turkish):
            skipped += 1
            continue
        derived, agreed = abbreviations.get(code, ("", 0))
        override = ABBREVIATION_OVERRIDES.get(code, "")
        rows.append(
            {
                "code": code,
                # The override wins: it exists precisely for codes the catalog
                # could not speak for, and is verified against a real table.
                "abbreviation": override or derived,
                "abbreviation_source": "override" if override else ("catalog" if derived else ""),
                "courses_agreeing": agreed,
                "name_en": english,
                "name_tr": turkish,
            }
        )
    print(f"dropped {skipped} Northern Cyprus / joint programmes", file=sys.stderr)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "app/campus/departments.json"))
    args = parser.parse_args()

    rows = asyncio.run(build())
    named = [row for row in rows if row["abbreviation"]]
    payload = {
        "sources": [SAIS_PUBLIC, CATALOG],
        "note": "Generated by scripts/build_department_directory.py. Do not edit by hand.",
        "departments": rows,
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"\n{len(rows)} departments, {len(named)} with an abbreviation "
        f"({sum(1 for r in rows if r['abbreviation_source'] == 'override')} from overrides) -> {args.out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

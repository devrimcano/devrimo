"""This term's unmet curriculum, read from the catalog rather than reasoned about.

The planner used to ask an agent for this. Measured in production, that took a
median of 95.8 seconds — of which about 8 were the campus and about 88 were the
model, because the agent called ``list_program_courses`` once per department
with a full round trip between each. What it did with those calls was match a
list of codes against department listings; there is no judgement in that, and
nothing here needs a model to do it.

Two things the prompt asked for turn out to be *more* correct done this way:

* It compared courses by code alone, so a course the student took and failed was
  treated as done. A transcript row carries its grade, and this reads it.
* It was told to prefer the Turkish-citizen variant of the compulsory history
  and language courses "unless the student's record says otherwise" — but no
  field anywhere in the record says which citizenship applies, so that escape
  hatch could never fire. The student's own curriculum listing does say, and
  their transcript says, and both are consulted before the default.
"""

import re
from typing import Any

from app.campus import departments
from app.campus.eligibility import course_candidates, has_passed, prior_grade

# A curriculum is four or five categories. The cap is here so a malformed
# overview cannot turn one request into an unbounded walk of the campus.
MAX_CATEGORIES = 6

# Matches ``BulkConstraintsRequest``'s own ceiling, because the planner's next
# move after this is to ask that endpoint for verdicts on everything returned.
MAX_COURSES = 40

# The compulsory history and Turkish-language requirements exist in variants by
# citizenship, and the directory names both sides: 240 History and 642 Turkish
# Language carry the courses Turkish citizens take, while 629 is "Modern
# Languages (Turkish as a Foreign Language)". A student is only ever meant to
# take one variant of each.
_HISTORY_DEPARTMENTS = frozenset({"240"})
_LANGUAGE_DEPARTMENTS = frozenset({"642", "629"})

# HIST2201/2202 and TURK103/104 — what a Turkish citizen takes, and the default
# when nothing else decides. Stated as codes because this is the one place a
# specific pair is preferred over another and it should be readable as such.
_TURKISH_CITIZEN_CODES = frozenset({"2402201", "2402202", "6420103", "6420104"})


def _fold(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def next_semester_courses(board: Any) -> list[dict]:
    """Use SAIS's checkmark and grade cells, never infer a student's semester."""
    # The live MCP tool wraps its dictionary return value under result.
    if isinstance(board, dict) and set(board) == {"result"}:
        board = board["result"]
    # A string here is not a board. It used to be the shape a failed tool call
    # arrived in, and answering it with the generic sentence below is what hid
    # SAIS's own explanation from the one person who could act on it.
    if isinstance(board, str):
        raise ValueError(f"SAIS Curriculum could not be read: {' '.join(board.split())[:200]}")
    if not isinstance(board, dict) or not isinstance(board.get("semesters"), list) or not board["semesters"]:
        raise ValueError("SAIS Curriculum semester boxes could not be read")
    semesters = board["semesters"]
    for item in semesters:
        if (not isinstance(item, dict) or type(item.get("semester")) is not int
                or item["semester"] < 1 or type(item.get("completed")) is not bool
                or not isinstance(item.get("courses"), list)):
            raise ValueError("SAIS Curriculum completion markers could not be read")
    pending = next((item for item in sorted(semesters, key=lambda item: item["semester"])
                    if not item["completed"]), None)
    if pending is None:
        return []
    rows = pending["courses"]
    if any(not isinstance(row, dict) or not isinstance(row.get("grade"), str) for row in rows):
        raise ValueError("SAIS Curriculum course grades could not be read")
    return [row for row in rows if not row["grade"].strip()]


def normalise_code(value: Any) -> str | None:
    """A curriculum row's course code as METU's seven digits, or nothing.

    A row that resolves to neither a seven-digit code nor a known abbreviation
    becomes a warning at the call site. It is never guessed at: rewriting an
    unrecognised code with the student's own department prefix is how a course
    that belongs to Physics ends up recommended as an Electrical one.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 7:
        return digits
    expanded = departments.expand_course_code(text)
    return expanded[0] if expanded else None


def still_needed(code: str, completed: Any) -> bool:
    """Whether the student still has to take this course.

    Not "is it absent from the transcript". A course sat and failed is on the
    transcript and still has to be taken; comparing codes alone — which is all
    the agent was given — quietly dropped exactly the courses a student most
    needs to be reminded of.
    """
    owner = departments.by_code(code[:3])
    held = prior_grade(completed, course_candidates(code, owner, code))
    return not (held and has_passed(held))


def _variant_family(code: str) -> str | None:
    """Which citizenship-varying requirement a course belongs to, if any."""
    prefix = code[:3]
    if prefix in _HISTORY_DEPARTMENTS:
        return "history"
    if prefix in _LANGUAGE_DEPARTMENTS:
        return "language"
    return None


def resolve_citizenship_variants(
    courses: list[dict], completed: Any
) -> tuple[list[dict], list[str]]:
    """Keep one variant of each citizenship-varying requirement.

    Order of evidence, strongest first. If the student's own curriculum listing
    offers only one variant, that is the answer and nothing here fires — SAIS
    built that list for this student and this programme, so it already encodes
    what they are expected to take. If it offers several, a variant they have
    already sat decides. Only when neither says anything is the Turkish-citizen
    pair taken, and then the choice is stated in a warning rather than made
    silently, because an international student needs to see it to correct it.
    """
    grouped: dict[str, list[dict]] = {}
    for course in courses:
        family = _variant_family(str(course.get("code") or ""))
        if family:
            grouped.setdefault(family, []).append(course)

    dropped: set[str] = set()
    warnings: list[str] = []
    for family, members in grouped.items():
        if len(members) < 2:
            continue
        sat = [
            course
            for course in members
            if prior_grade(completed, course_candidates(course["code"], None, course["code"]))
        ]
        if sat:
            keep = {course["code"] for course in sat}
        else:
            preferred = [c for c in members if c["code"] in _TURKISH_CITIZEN_CODES]
            keep = {course["code"] for course in (preferred or members[:1])}
            if preferred:
                others = ", ".join(sorted(c["code"] for c in members if c["code"] not in keep))
                warnings.append(
                    f"{family}: kept {', '.join(sorted(keep))} — the variant Turkish citizens take. "
                    f"If you are an international student, add {others} instead."
                )
        dropped |= {course["code"] for course in members if course["code"] not in keep}

    return [course for course in courses if course["code"] not in dropped], warnings


def offered_this_term(codes: list[str], listing_codes: set[str]) -> list[str]:
    """The subset a department's published listing actually contains."""
    return [code for code in codes if code in listing_codes]

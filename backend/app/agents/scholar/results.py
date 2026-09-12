"""Tool results are projected before they are bounded.

Every result is re-sent on each later model step, and the 6,000-character bound
was turning real reads into a truncated preview: on a live prerequisites read,
3,948 of 5,692 characters were the fields the answer used, and other catalog
reads crossed the bound entirely - the model then reported that the list "came
back shortened" and answered from the front half.

The `_catalog` envelope alone was 1,744 characters of that payload (release
ids, components, registration window). It is not dropped whole, though: its
`components` block is the read-time fresh/stale state per catalog component, and
a result whose prerequisites were verified yesterday but are stale today must
not read as current. It is compacted to a `freshness` summary instead.

Projection is otherwise generic: it removes private (underscore) keys and null
values, recursively, and changes nothing else. It never guesses which domain
fields an answer needs - except where a resource has two documented shapes
(the published catalog record and the legacy one), where it supports both.

A result that still exceeds the bound is cut structurally, from the end of its
list, with the totals kept in front of it: a preview string loses the totals
that tell the answer how much it is missing, which is how one run reported an
invented "172 sections" for a 61-section course.
"""

import json
from typing import Any

MAX_TOOL_RESULT_CHARS = 6_000
# An explicit "show me everything" raises the bound; it is the only case where
# paying for the whole list on every later model step is what the student asked
# for. It is still bounded, because a result is re-sent on each step.
EXPANDED_RESULT_CHARS = MAX_TOOL_RESULT_CHARS * 4
# How much of a long list travels before the answer offers the choice: fifty
# sections in one prompt cost every step of the turn, and the student rarely
# wants all of them - they want the one that fits their surname range or week.
SECTION_PREVIEW = 8
UPDATES_PREVIEW = 20
# A row is ~500 characters raw; a section carries up to five of them, which is
# where the listing's bulk lived. The full range is not a restriction.
_FULL_RANGE = {"min_cgpa": 0.0, "max_cgpa": 4.0, "min_year": 0, "max_year": 95}
_RESTRICTION_FIELDS = (
    "restriction_group",
    "given_department",
    "start_char",
    "end_char",
    "min_year",
    "max_year",
    "min_cgpa",
    "max_cgpa",
    "start_grade",
    "end_grade",
)
MAX_SECTION_RESTRICTIONS = 4
# An expanded list is a schedule, not an eligibility report: one restriction
# row per section is the gist, and it is what lets sixty-one sections fit.
MAX_EXPANDED_SECTION_RESTRICTIONS = 1
# Per-field caps, because one measurement is not a bound: a course whose notes
# are allowed to run to their 4,000-character schema limit blows through any
# global ceiling, and a global ceiling that gets raised to fit it is a guess.
MAX_SECTION_NOTES = 300
_COURSE_FIELDS = (
    "course_code",
    "title",
    "name",
    "credits",
    "local_credits",
    "ects",
    "level",
    "availability",
    "term",
)
# The fields that carry section information, across both catalog shapes: the
# published record uses `instructors`/`meetings`/`restrictions`, the legacy
# payload uses `instructor`/`schedule`/`constraint`.
_SECTION_VALUE_KEYS = ("instructors", "instructor", "meetings", "schedule", "restrictions", "constraint")


def _catalog_summary(catalog: dict) -> dict:
    """The fresh/stale state of every component, without the ids and windows.

    Keeps `fresh` and `verified` for each component - the flags an answer needs
    to qualify a fact - and the registration window, which a scheduling question
    can act on. Drops the observation timestamps, source statuses and release
    identifiers that only the ingestion side reads.
    """
    components = catalog.get("components") or {}
    return {
        "release_id": catalog.get("release_id"),
        "components": {
            name: {"fresh": bool(entry.get("fresh")), "verified": bool(entry.get("verified"))}
            for name, entry in components.items()
            if isinstance(entry, dict)
        },
        "registration_window": catalog.get("registration_window"),
    }


def project(value: Any) -> Any:
    if isinstance(value, dict):
        projected: dict = {}
        for key, item in value.items():
            if key == "_catalog" and isinstance(item, dict):
                projected["freshness"] = project(_catalog_summary(item))
                continue
            if item is None or str(key).startswith("_"):
                continue
            projected[key] = project(item)
        return projected
    if isinstance(value, list):
        return [project(item) for item in value]
    return value


def bound(result: Any, *, limit: int = MAX_TOOL_RESULT_CHARS) -> Any:
    """Cap a result at the size every later model step of the turn pays for."""
    if isinstance(result, str) and len(result) > limit:
        return result[:limit] + f"\n\n[Result truncated by Devrimo after {limit} characters. Narrow the query.]"
    if isinstance(result, (dict, list)):
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) > limit:
            return {
                "truncated": True,
                "preview": serialized[:limit],
                "instruction": "Narrow the query before using this result.",
            }
    return result


def _compact_meeting(item: dict) -> dict:
    """One meeting in whichever shape the source used, on one set of keys."""
    compact: dict = {}
    if item.get("weekday") is not None:
        compact["weekday"] = item["weekday"]
    elif item.get("day"):
        compact["day"] = item["day"]
    label = item.get("raw_label") or item.get("hour")
    if label:
        compact["time"] = label
    elif item.get("start_minute") is not None:
        compact["time"] = f"{int(item['start_minute']) // 60:02d}:{int(item['start_minute']) % 60:02d}"
    minutes = item.get("duration_minutes")
    if minutes is None and item.get("start_minute") is not None and item.get("end_minute") is not None:
        minutes = int(item["end_minute"]) - int(item["start_minute"])
    if minutes:
        compact["minutes"] = minutes
    if item.get("room"):
        compact["room"] = item["room"]
    return compact


def _compact_restriction(row: dict) -> dict:
    compact = {}
    for key in _RESTRICTION_FIELDS:
        value = row.get(key)
        if value is None:
            continue
        if key in _FULL_RANGE and value == _FULL_RANGE[key]:
            continue
        compact[key] = value
    return compact


def _compact_section(section: dict, *, expand: bool = False) -> dict:
    """One section as the answer needs it: number, who, when, who may take it.

    Reads both catalog shapes: the published record's `instructors` list and
    `meetings`, and the legacy `instructor` string and `schedule` list. The
    legacy shape is the default whenever `academic_catalog_reads_enabled` is
    off, so dropping it would remove the core facts on that path.
    """
    compact: dict = {}
    if section.get("section") not in (None, ""):
        compact["section"] = section["section"]
    notes = section.get("notes")
    if isinstance(notes, str) and notes.strip():
        compact["notes"] = notes.strip()[:MAX_SECTION_NOTES]
    names: list[str] = []
    instructors = section.get("instructors")
    if isinstance(instructors, list):
        names = [
            item.get("source_name")
            for item in instructors
            if isinstance(item, dict) and item.get("source_name")
        ]
    if not names:
        legacy = section.get("instructor")
        if isinstance(legacy, str) and legacy.strip():
            names = [legacy.strip()]
        elif isinstance(legacy, list):
            names = [str(name) for name in legacy if isinstance(name, str) and name.strip()]
    if names:
        compact["instructors"] = names
    meetings = section.get("meetings")
    if not (isinstance(meetings, list) and meetings):
        meetings = section.get("schedule")
    if isinstance(meetings, list) and meetings:
        compact["meetings"] = [
            meeting
            for meeting in (_compact_meeting(item) for item in meetings if isinstance(item, dict))
            if meeting
        ]
    restrictions = section.get("restrictions")
    cap = MAX_EXPANDED_SECTION_RESTRICTIONS if expand else MAX_SECTION_RESTRICTIONS
    if isinstance(restrictions, list) and restrictions:
        compact["restrictions"] = [_compact_restriction(row) for row in restrictions[:cap] if isinstance(row, dict)]
        if len(restrictions) > cap:
            compact["restrictions_omitted"] = len(restrictions) - cap
    elif section.get("constraint") not in (None, "", []):
        compact["constraint"] = str(section["constraint"])[:MAX_SECTION_NOTES]
    return compact


def _section_score(rows: list) -> int:
    return sum(
        1
        for row in rows
        if isinstance(row, dict)
        for key in _SECTION_VALUE_KEYS
        if row.get(key) not in (None, "", [], {})
    )


def _richest_sections(candidates: list[list]) -> list:
    if not candidates:
        return []
    return max(candidates, key=_section_score)


def _project_sections(envelope: dict, *, expand: bool) -> dict:
    """Unwrap the envelope, keep both shapes, and cap the list with a count.

    The published listing nests the real course under `data.data` and repeats a
    thinner normalized list beside it; the legacy payload is the other way
    around. The list with the most usable section information wins, and the
    compact row supports both field spellings, so neither path loses the facts.
    """
    data = envelope.get("data")
    if not isinstance(data, dict):
        return envelope
    inner = data.get("data") if isinstance(data.get("data"), dict) else None
    candidates: list[list] = []
    for value in (inner.get("sections") if inner else None, data.get("sections")):
        if isinstance(value, list) and value:
            candidates.append(value)
    if not candidates and isinstance(data.get("sections"), list):
        # A genuinely empty list is a real answer and must stay an empty list.
        candidates.append(data["sections"])
    sections = _richest_sections(candidates)
    base = inner if inner and inner.get("sections") is not None else data
    compact = {key: base.get(key) for key in _COURSE_FIELDS if base.get(key) is not None}
    if "course_code" not in compact and data.get("course"):
        compact["course_code"] = data["course"]
    if "term" not in compact and data.get("semester"):
        compact["term"] = data["semester"]
    cap = len(sections) if expand else SECTION_PREVIEW
    # The totals come before the list they describe: a result that still
    # overflows is truncated from the end, and a preview that loses "61
    # sections" invites an invented number instead of the real one.
    compact["sections_total"] = len(sections)
    if len(sections) > cap:
        compact["sections_omitted"] = len(sections) - cap
    compact["sections"] = [_compact_section(item, expand=expand) for item in sections[:cap] if isinstance(item, dict)]
    # Carried so the generic projection below turns it into `freshness`.
    compact["_catalog"] = (inner or data).get("_catalog")
    # A copy: two projections of one envelope (a preview and an expand) must
    # not see each other's work.
    return {**envelope, "data": compact}


def _compact_update(item: dict) -> dict:
    summary = item.get("summary")
    if not summary:
        content = item.get("content")
        summary = f"{str(content)[:240]}…" if content else None
    compact = {
        "id": item.get("id"),
        "type": item.get("type"),
        "title": item.get("title"),
        "when": item.get("starts_at") or item.get("published_at") or item.get("retrieved_at"),
        "summary": summary,
        "source": item.get("source"),
        "url": item.get("url"),
        "origin": item.get("origin"),
        "read": item.get("read"),
    }
    return {key: value for key, value in compact.items() if value is not None}


def _project_updates(envelope: dict, *, expand: bool) -> dict:
    """A feed shows its newest few; the rest stay available on request.

    Each item keeps what an answer uses - what, when, where from, a short
    summary - and drops the retrieval plumbing and the duplicated full text
    that made thirteen items weigh 18,813 characters.
    """
    data = envelope.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return envelope
    items = data["items"]
    cap = len(items) if expand else UPDATES_PREVIEW
    compact = {key: value for key, value in data.items() if key != "items"}
    # Totals before items, for the same reason as sections.
    compact["items_total"] = len(items)
    if len(items) > cap:
        compact["items_omitted"] = len(items) - cap
    compact["items"] = [_compact_update(item) for item in items[:cap] if isinstance(item, dict)]
    return {**envelope, "data": compact}


def _fit_sections(projected: Any, limit: int) -> Any:
    """Cut a too-large section list from the end, keeping the totals.

    The alternative - `bound`'s preview string - throws away the JSON shape and
    the totals with it, which is worse than a shorter structured list that says
    how much it left out.
    """
    if not isinstance(projected, dict):
        return projected
    data = projected.get("data")
    if not isinstance(data, dict):
        return projected
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        return projected
    total = data.get("sections_total", len(sections))
    size = len(json.dumps(projected, ensure_ascii=False, default=str))
    while sections and size > limit:
        sections.pop()
        data["sections_omitted"] = total - len(sections)
        size = len(json.dumps(projected, ensure_ascii=False, default=str))
    return projected


def project_result(kind: str | None, result: Any, *, expand: bool = False, limit: int = MAX_TOOL_RESULT_CHARS) -> Any:
    """Projection plus the per-kind reshaping the answer actually needs."""
    if isinstance(result, dict):
        if kind == "catalog.sections":
            result = _project_sections(result, expand=expand)
        elif kind == "my.updates":
            result = _project_updates(result, expand=expand)
    projected = project(result)
    if kind == "catalog.sections":
        projected = _fit_sections(projected, limit)
    return projected

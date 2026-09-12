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
fields an answer needs.
"""

from typing import Any

MAX_TOOL_RESULT_CHARS = 6_000
# An explicit "show me everything" raises the bound; it is the only case where
# paying for the whole list on every later model step is what the student asked
# for. It is still bounded, because a result is re-sent on each step.
EXPANDED_RESULT_CHARS = MAX_TOOL_RESULT_CHARS * 3
# How much of a long list travels before the answer offers the choice: fifty
# sections in one prompt cost every step of the turn, and the student rarely
# wants all of them - they want the one that fits their surname range or week.
SECTION_PREVIEW = 8
UPDATES_PREVIEW = 20

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
# A row is ~500 characters raw; a section carries up to five of them, which is
# where the listing's bulk lived. The full range is not a restriction.
_FULL_RANGE = {"min_cgpa": 0.0, "max_cgpa": 4.0, "min_year": 0, "max_year": 95}
_MEETING_FIELDS = ("weekday", "raw_label", "room")
MAX_SECTION_RESTRICTIONS = 4


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


def _compact_section(section: dict) -> dict:
    """One section as the answer needs it: number, who, when, who may take it.

    The raw row carries syllabus plumbing, observation timestamps and a
    thirty-field restriction object per rule; none of that changes the answer.
    Meetings keep the weekday, the readable time and the room - `weekday` is a
    number, so `raw_label` is what a table can show.
    """
    compact: dict = {}
    if section.get("section") not in (None, ""):
        compact["section"] = section["section"]
    if section.get("notes"):
        compact["notes"] = section["notes"]
    instructors = section.get("instructors")
    if isinstance(instructors, list):
        names = [
            item.get("source_name")
            for item in instructors
            if isinstance(item, dict) and item.get("source_name")
        ]
        if names:
            compact["instructors"] = names
    meetings = section.get("meetings")
    if isinstance(meetings, list) and meetings:
        compact["meetings"] = [
            {key: item.get(key) for key in _MEETING_FIELDS if item.get(key) is not None}
            for item in meetings
            if isinstance(item, dict)
        ]
    restrictions = section.get("restrictions")
    if isinstance(restrictions, list) and restrictions:
        compact["restrictions"] = [
            _compact_restriction(row) for row in restrictions[:MAX_SECTION_RESTRICTIONS] if isinstance(row, dict)
        ]
        if len(restrictions) > MAX_SECTION_RESTRICTIONS:
            compact["restrictions_omitted"] = len(restrictions) - MAX_SECTION_RESTRICTIONS
    return compact


def _project_sections(envelope: dict, *, expand: bool) -> dict:
    """Unwrap the double envelope and cap the section list with a count.

    The listing nests the real course under ``data.data`` and repeats a stub
    list beside it; that duplication is what pushed a five-section course past
    the bound. Projection keeps the course record, keeps at most
    ``SECTION_PREVIEW`` sections unless the student asked for all of them, and
    records how many were left out so the answer can offer the choice instead
    of silently showing a truncated list.
    """
    data = envelope.get("data")
    if not isinstance(data, dict):
        return envelope
    inner = data.get("data")
    course = inner if isinstance(inner, dict) and inner.get("sections") is not None else data
    sections = course.get("sections")
    if not isinstance(sections, list):
        return envelope
    compact = {key: course.get(key) for key in _COURSE_FIELDS if course.get(key) is not None}
    cap = len(sections) if expand else SECTION_PREVIEW
    # The totals come before the list they describe: a result that still
    # overflows is truncated from the end, and a preview that loses "61
    # sections" invites an invented number instead of the real one.
    compact["sections_total"] = len(sections)
    if len(sections) > cap:
        compact["sections_omitted"] = len(sections) - cap
    compact["sections"] = [_compact_section(item) for item in sections[:cap] if isinstance(item, dict)]
    # Carried so the generic projection below turns it into `freshness`.
    compact["_catalog"] = course.get("_catalog")
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


def project_result(kind: str | None, result: Any, *, expand: bool = False) -> Any:
    """Projection plus the per-kind reshaping the answer actually needs."""
    if isinstance(result, dict):
        if kind == "catalog.sections":
            result = _project_sections(result, expand=expand)
        elif kind == "my.updates":
            result = _project_updates(result, expand=expand)
    return project(result)


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
        return (
            result[:limit]
            + f"\n\n[Result truncated by Devrimo after {limit} characters. Narrow the query.]"
        )
    if isinstance(result, (dict, list)):
        import json

        serialized = json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) > limit:
            return {
                "truncated": True,
                "preview": serialized[:limit],
                "instruction": "Narrow the query before using this result.",
            }
    return result

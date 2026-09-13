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

import json
import re
from typing import Any

MAX_TOOL_RESULT_CHARS = 6_000
# An explicit "show me everything" gets a larger budget, but the section
# projection still fills that budget structurally and reports omitted rows.
# It is not a promise that an arbitrary JSON prefix is safe for every source
# response; per-field caps and the fit loop below keep valid expanded results
# self-describing when they exceed this budget.
EXPANDED_RESULT_CHARS = MAX_TOOL_RESULT_CHARS * 4
# How much of a long list travels before the answer offers the choice: fifty
# sections in one prompt cost every step of the turn, and the student rarely
# wants all of them - they want the one that fits their surname range or week.
SECTION_PREVIEW = 8
UPDATES_PREVIEW = 20
MAX_UPDATE_SUMMARY_CHARS = 512
MAX_UPDATE_TITLE_CHARS = 256
MAX_UPDATE_SOURCE_CHARS = 256
MAX_UPDATE_URL_CHARS = 2_048

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
# An expanded list is a schedule, not an eligibility report: one restriction
# row per section is the gist. Variable-length source fields are capped below
# as well, so the structural budget never falls back to an arbitrary JSON
# prefix for a valid section result.
MAX_EXPANDED_SECTION_RESTRICTIONS = 1
MAX_SECTION_INSTRUCTORS = 8
MAX_SECTION_MEETINGS = 8
MAX_TEXT_VALUE_CHARS = 256
MAX_INSTRUCTOR_NAME_CHARS = 128
MAX_MEETING_TEXT_CHARS = 128


def _bounded_text(value: Any, limit: int = MAX_TEXT_VALUE_CHARS) -> tuple[str | None, bool]:
    """Return readable text with an explicit truncation marker."""
    if value in (None, ""):
        return None, False
    text = str(value).strip()
    if not text:
        return None, False
    if len(text) <= limit:
        return text, False
    return f"{text[: max(1, limit - 1)].rstrip()}…", True


def _bounded_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _bounded_text(value)
    return value, False


def _compact_restriction(row: dict) -> dict:
    compact = {}
    for key in _RESTRICTION_FIELDS:
        value = row.get(key)
        if value is None:
            continue
        if key in _FULL_RANGE and value == _FULL_RANGE[key]:
            continue
        compact[key], _ = _bounded_value(value)
    return compact


def _instructor_names(value: Any) -> list[str]:
    """Read both Course Info's plural rows and its legacy singular value."""
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, str):
        value = re.split(r"[,;/|]", value)
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = next(
                (
                    item.get(key)
                    for key in ("source_name", "name", "instructor", "lecturer", "teacher")
                    if item.get(key)
                ),
                None,
            )
        name, _ = _bounded_text(item, MAX_INSTRUCTOR_NAME_CHARS)
        if name:
            names.append(name)
    return names


def _first_section_value(section: dict, *keys: str) -> Any:
    for key in keys:
        value = section.get(key)
        if value not in (None, "", []):
            return value
    return None


def _compact_meeting(item: Any) -> dict:
    """Keep meeting facts from either normalized or legacy Course Info rows."""
    if not isinstance(item, dict):
        schedule, _ = _bounded_text(item, MAX_MEETING_TEXT_CHARS)
        return {"schedule": schedule} if schedule else {}

    compact: dict[str, Any] = {}
    weekday = item.get("weekday")
    raw_label, raw_label_truncated = _bounded_text(
        _first_section_value(item, "raw_label", "label"), MAX_MEETING_TEXT_CHARS
    )
    room, room_truncated = _bounded_text(
        _first_section_value(item, "room", "classroom", "location", "derslik"), MAX_MEETING_TEXT_CHARS
    )

    # Published rows have a numeric weekday and a readable raw label. Keep the
    # established compact shape for those rows; the source's exact minute
    # fields are represented by the readable label in this model-facing view.
    if weekday is not None or raw_label:
        if weekday is not None:
            compact["weekday"] = weekday
        if raw_label:
            compact["raw_label"] = raw_label
        else:
            start = _first_section_value(item, "start_minute", "startMinute", "start")
            end = _first_section_value(item, "end_minute", "endMinute", "end")
            if start is not None:
                compact["start_minute"] = start
            if end is not None:
                compact["end_minute"] = end
        if room:
            compact["room"] = room
    else:
        # The legacy normalizer emits day/start/duration, while an old live
        # Course Info response emits day/time. Preserve whichever facts exist
        # instead of dropping the whole schedule because the sibling shape is
        # different.
        day = _first_section_value(item, "day", "week_day", "gun")
        if day is not None:
            compact["day"] = day
        start = _first_section_value(item, "start_minute", "startMinute", "start")
        duration = _first_section_value(item, "duration_minutes", "durationMinutes", "duration")
        if start is not None:
            compact["start_minute"] = start
        if duration is not None:
            compact["duration_minutes"] = duration
        time_value, time_truncated = _bounded_text(
            _first_section_value(item, "time", "hours", "schedule", "meeting"), MAX_MEETING_TEXT_CHARS
        )
        if time_value:
            compact["time"] = time_value
        if room:
            compact["room"] = room
        raw_label_truncated = raw_label_truncated or time_truncated

    status = item.get("status") or item.get("meeting_status")
    if status and str(status).casefold() != "scheduled":
        compact["status"], _ = _bounded_text(status, MAX_MEETING_TEXT_CHARS)
    if raw_label_truncated or room_truncated:
        compact["text_truncated"] = True
    return compact


def _meeting_values(section: dict) -> Any:
    # Prefer the canonical sibling when it has facts, but fall back to the raw
    # schedule used by older Course Info servers when the canonical list is
    # absent or empty.
    return _first_section_value(section, "meetings", "schedule", "meeting", "hours", "time")


def _compact_section(section: dict, *, expand: bool = False) -> dict:
    """One section as the answer needs it: number, who, when, who may take it.

    The raw row carries syllabus plumbing, observation timestamps and a
    thirty-field restriction object per rule; none of that changes the answer.
    Meetings keep the weekday, the readable time and the room - `weekday` is a
    number, so `raw_label` is what a table can show. An expanded list cuts the
    restriction rows to their gist, because sixty-one sections have to fit a
    table the student asked for.
    """
    compact: dict = {}
    section_value = _first_section_value(section, "section", "section_code", "section_number", "section_no", "sec")
    if section_value is not None:
        compact["section"], _ = _bounded_value(section_value)
    notes_value = _first_section_value(section, "notes", "critical_info", "critical", "constraint", "comment")
    notes, notes_truncated = _bounded_text(notes_value)
    if notes:
        compact["notes"] = notes
    if notes_truncated:
        compact["notes_truncated"] = True

    # The normalized sibling uses singular ``instructor``. Check it first so
    # it wins over a stale/raw plural value retained for fallback fields.
    instructor_value = _first_section_value(section, "instructor", "instructors", "lecturer", "teacher")
    names = _instructor_names(instructor_value)
    if names:
        compact["instructors"] = names[:MAX_SECTION_INSTRUCTORS]
        if len(names) > MAX_SECTION_INSTRUCTORS:
            compact["instructors_omitted"] = len(names) - MAX_SECTION_INSTRUCTORS

    meetings = _meeting_values(section)
    if isinstance(meetings, dict):
        meetings = [meetings]
    if isinstance(meetings, list):
        compact_meetings = [
            item
            for raw in meetings[:MAX_SECTION_MEETINGS]
            if (item := _compact_meeting(raw))
        ]
        if compact_meetings:
            compact["meetings"] = compact_meetings
        if len(meetings) > MAX_SECTION_MEETINGS:
            compact["meetings_omitted"] = len(meetings) - MAX_SECTION_MEETINGS
    elif meetings not in (None, ""):
        meeting = _compact_meeting(meetings)
        if meeting:
            compact["meetings"] = [meeting]

    restrictions = section.get("restrictions")
    cap = MAX_EXPANDED_SECTION_RESTRICTIONS if expand else MAX_SECTION_RESTRICTIONS
    if isinstance(restrictions, list) and restrictions:
        compact["restrictions"] = [
            _compact_restriction(row) for row in restrictions[:cap] if isinstance(row, dict)
        ]
        if len(restrictions) > cap:
            compact["restrictions_omitted"] = len(restrictions) - cap
    return compact


def _section_key(section: Any, index: int) -> str:
    if isinstance(section, dict):
        value = _first_section_value(section, "section", "section_code", "section_number", "section_no", "sec")
        if value is not None:
            text = str(value).strip()
            return (text.lstrip("0") or "0") if text.isdigit() else text
    return f"__index_{index}"


def _is_normalized_sibling(sections: Any) -> bool:
    """Recognize ``normalize_sections`` output without mistaking a stub list."""
    if not isinstance(sections, list):
        return False
    for section in sections:
        if not isinstance(section, dict):
            continue
        if "instructor" in section or "constraint" in section:
            return True
        meetings = section.get("meetings")
        if isinstance(meetings, list) and any(isinstance(item, dict) and item for item in meetings):
            return True
    return False


def _merge_section_sources(raw_sections: Any, normalized_sections: Any) -> list[dict]:
    """Merge raw detail rows with the normalized sibling, preferring the latter.

    ``get_course_sections`` returns the raw Course Info response under
    ``data`` and a normalized ``sections`` sibling. Older responses put
    instructors under ``instructors`` and times under ``schedule``; published
    responses use typed ``instructors``/``meetings``. The sibling is the
    canonical source for instructor/meeting facts when it is real, while the
    raw row still contributes restrictions and notes that normalization does
    not carry.
    """
    raw = [item for item in raw_sections or [] if isinstance(item, dict)] if isinstance(raw_sections, list) else []
    normalized = (
        [item for item in normalized_sections if isinstance(item, dict)]
        if isinstance(normalized_sections, list) and _is_normalized_sibling(normalized_sections)
        else []
    )
    if not normalized:
        return raw
    raw_by_key = {_section_key(item, index): item for index, item in enumerate(raw)}
    merged: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(normalized):
        key = _section_key(item, index)
        source = dict(raw_by_key.get(key, {}))
        for field, value in item.items():
            # Empty normalized values should not erase a useful legacy
            # schedule/instructor field; non-empty canonical values win.
            if value not in (None, "", []):
                source[field] = value
            else:
                source.setdefault(field, value)
        merged.append(source)
        seen.add(key)
    merged.extend(item for index, item in enumerate(raw) if _section_key(item, index) not in seen)
    return merged


def _projected_size(envelope: dict, compact: dict) -> int:
    """Measure the final projected shape, including freshness metadata."""
    return len(json.dumps(project({**envelope, "data": compact}), ensure_ascii=False, default=str))


def _project_sections(envelope: dict, *, expand: bool) -> dict:
    """Unwrap the double envelope and fit sections structurally.

    The listing nests the real course under ``data.data`` and repeats a stub
    list beside it. The sibling is a normalized view on both the published and
    legacy paths, so it is merged into the raw rows before projection. The
    final section count is then chosen against the normal result budget rather
    than against one measured course: omitted rows are represented by counts,
    never by a JSON prefix produced by ``bound``.
    """
    data = envelope.get("data")
    if not isinstance(data, dict):
        return envelope
    inner = data.get("data")
    course = inner if isinstance(inner, dict) and inner.get("sections") is not None else data
    raw_sections = course.get("sections")
    if not isinstance(raw_sections, list):
        return envelope
    sibling_sections = data.get("sections") if data is not course else None
    sections = _merge_section_sources(raw_sections, sibling_sections)

    compact: dict[str, Any] = {}
    for key in _COURSE_FIELDS:
        value = course.get(key)
        if value is not None:
            compact[key], _ = _bounded_value(value)
    compact["sections_total"] = len(sections)
    # Carried so the generic projection below turns it into `freshness`.
    compact["_catalog"] = course.get("_catalog") or data.get("_catalog")

    # The default read is intentionally a short preview. An explicit expanded
    # read may request every section, but a course can contain an unbounded
    # number of source rows or unusually large notes/schedules. Build the list
    # one complete section at a time and retain structured omission metadata
    # when the final projected object reaches the relevant result budget.
    requested_cap = min(len(sections), len(sections) if expand else SECTION_PREVIEW)
    result_budget = EXPANDED_RESULT_CHARS if expand else MAX_TOOL_RESULT_CHARS
    selected: list[dict] = []
    for section in sections[:requested_cap]:
        candidate_sections = [*selected, _compact_section(section, expand=expand)]
        candidate = dict(compact)
        candidate["sections_total"] = len(sections)
        if len(candidate_sections) < len(sections):
            candidate["sections_omitted"] = len(sections) - len(candidate_sections)
            candidate["sections_returned"] = len(candidate_sections)
            candidate["sections_omission_reason"] = (
                "result_bound" if len(candidate_sections) < requested_cap else "preview"
            )
        candidate["sections"] = candidate_sections
        if _projected_size(envelope, candidate) > result_budget:
            break
        selected = candidate_sections

    compact["sections"] = selected
    if len(selected) < len(sections):
        compact["sections_omitted"] = len(sections) - len(selected)
        compact["sections_returned"] = len(selected)
        compact["sections_omission_reason"] = "result_bound" if len(selected) < requested_cap else "preview"
    # The totals come before the list they describe: a preview cut from the
    # end still carries the actual total and omission count.
    compact["sections_total"] = len(sections)
    # A copy: two projections of one envelope (a preview and an expand) must
    # not see each other's work.
    return {**envelope, "data": compact}


def _compact_update(item: dict) -> dict:
    summary = item.get("summary")
    if not summary:
        content = item.get("content")
        summary = f"{str(content)[:240]}…" if content else None
    values = {
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
    limits = {
        "id": MAX_UPDATE_SOURCE_CHARS,
        "type": MAX_UPDATE_SOURCE_CHARS,
        "title": MAX_UPDATE_TITLE_CHARS,
        "when": MAX_UPDATE_SOURCE_CHARS,
        "summary": MAX_UPDATE_SUMMARY_CHARS,
        "source": MAX_UPDATE_SOURCE_CHARS,
        "url": MAX_UPDATE_URL_CHARS,
        "origin": MAX_UPDATE_SOURCE_CHARS,
    }
    compact: dict[str, Any] = {}
    truncated: list[str] = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, str):
            bounded, was_truncated = _bounded_text(value, limits[key])
            if bounded is None:
                continue
            compact[key] = bounded
            if was_truncated:
                truncated.append(key)
        else:
            compact[key] = value
    if truncated:
        compact["fields_truncated"] = truncated
    return compact


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
    compact = {key: value for key, value in data.items() if key != "items"}
    compact["items_total"] = len(items)
    cap = min(len(items), len(items) if expand else UPDATES_PREVIEW)
    result_budget = EXPANDED_RESULT_CHARS if expand else MAX_TOOL_RESULT_CHARS
    selected: list[dict] = []
    candidates = [_compact_update(item) for item in items[:cap] if isinstance(item, dict)]
    for item in candidates:
        candidate_items = [*selected, item]
        candidate = dict(compact)
        if len(candidate_items) < len(items):
            candidate["items_omitted"] = len(items) - len(candidate_items)
            candidate["items_returned"] = len(candidate_items)
            candidate["items_omission_reason"] = (
                "result_bound" if len(candidate_items) < cap else "preview"
            )
        candidate["items"] = candidate_items
        if _projected_size(envelope, candidate) > result_budget:
            break
        selected = candidate_items

    compact["items"] = selected
    if len(selected) < len(items):
        compact["items_omitted"] = len(items) - len(selected)
        compact["items_returned"] = len(selected)
        compact["items_omission_reason"] = "result_bound" if len(selected) < cap else "preview"
    # Totals before items, for the same reason as sections.
    compact["items_total"] = len(items)
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

"""Typed section and meeting normalization for the schedule API.

Course Info returns a stable model in production, while older campus servers
and cached answers may still wrap the same values in dictionaries or JSON
strings. This module owns that boundary so the browser receives one typed,
minute precise representation and never has to interpret catalog text or
decide which rows are schedulable.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.planning.models import Day

_KEY_FOLD = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
})
_DAYS: tuple[tuple[Day, tuple[str, ...]], ...] = (
    ("Mon", ("monday", "pazartesi", "mon")),
    ("Tue", ("tuesday", "sali", "salı", "tue")),
    ("Wed", ("wednesday", "carsamba", "çarşamba", "wed")),
    ("Thu", ("thursday", "persembe", "perşembe", "thu")),
    ("Fri", ("friday", "cuma", "fri")),
)


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).translate(_KEY_FOLD).casefold())


def _value(record: dict[str, Any], names: tuple[str, ...]) -> Any:
    wanted = {_key(name) for name in names}
    for name, value in record.items():
        if _key(name) in wanted:
            return value
    return None


def _day(value: Any) -> Day | None:
    folded = _key(value)
    for day, names in _DAYS:
        if any(name in folded for name in names):
            return day
    return None


def _clock(value: Any, *, start: bool) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        if 0 <= number <= 24:
            return number * 60 + (40 if start else 30)
        return number if 0 <= number < 24 * 60 else None
    match = re.search(r"(\d{1,2})(?::|\.)(\d{2})", str(value))
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return hour * 60 + minute if 0 <= hour <= 23 and 0 <= minute <= 59 else None
    match = re.fullmatch(r"\s*(\d{1,2})\s*", str(value))
    if match:
        hour = int(match.group(1))
        return hour * 60 + (40 if start else 30) if 0 <= hour <= 24 else None
    return None


def _time_range(value: Any) -> tuple[int, int] | None:
    text = str(value or "")
    matches = re.findall(r"(\d{1,2})(?::|\.)(\d{2})", text)
    if len(matches) < 2:
        return None
    start = int(matches[0][0]) * 60 + int(matches[0][1])
    end = int(matches[1][0]) * 60 + int(matches[1][1])
    if not (0 <= start < 24 * 60 and 0 < end <= 24 * 60 and end > start):
        return None
    return start, end


def _meeting(record: dict[str, Any]) -> dict[str, Any] | None:
    day = _day(_value(record, ("day", "weekday", "gun")))
    if day is None:
        return None
    time_value = _value(record, ("time", "hours", "saat"))
    ranged = _time_range(time_value)
    start_value = _value(record, ("start_minute", "startMinute", "starttime", "start", "begin", "baslangic"))
    end_value = _value(record, ("end_minute", "endMinute", "endtime", "end", "finish", "bitis"))
    if ranged:
        start, end = ranged
    else:
        start = _clock(start_value, start=True)
        end = _clock(end_value, start=False)
        if start is None:
            return None
        if end is None:
            duration_value = _value(record, ("duration_minutes", "durationMinutes", "duration"))
            if isinstance(duration_value, (int, float)) and not isinstance(duration_value, bool):
                duration = int(duration_value)
                duration = duration * 60 - 10 if duration <= 12 else duration
                end = start + max(1, duration)
            else:
                return None
    end = min(end, 24 * 60)
    if end <= start:
        return None
    return {
        "day": day,
        "start_minute": start,
        "duration_minutes": end - start,
        "room": str(_value(record, ("room", "classroom", "location", "derslik")) or "").strip(),
    }


def _json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _records(value: Any) -> list[dict[str, Any]]:
    value = _json(value)
    if isinstance(value, list):
        return [record for item in value for record in _records(item)]
    if not isinstance(value, dict):
        return []
    keys = {_key(name) for name in value}
    if keys.intersection({"section", "sectionnumber", "sectionno", "sec"}):
        return [value]
    return [record for child in value.values() for record in _records(child)]


def _text_sections(value: str) -> list[dict[str, Any]]:
    chunks = re.split(
        r"(?=^\s*(?:section|şube|sube)\s*(?:no\.?\s*)?[:#-]?\s*\d+)",
        value,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    rows: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        match = re.search(r"(?:section|şube|sube)\s*(?:no\.?\s*)?[:#-]?\s*(\d+)", chunk, re.IGNORECASE)
        section = match.group(1) if match else str(index + 1)
        meetings: list[dict[str, Any]] = []
        meetings.extend(_text_meetings(chunk))
        rows.append({"section": section, "instructor": "", "meetings": meetings, "constraint": ""})
    return rows


def _text_meetings(value: str) -> list[dict[str, Any]]:
    meetings: list[dict[str, Any]] = []
    for match in re.finditer(
        r"(monday|tuesday|wednesday|thursday|friday|pazartesi|salı|sali|çarşamba|carsamba|perşembe|persembe|cuma|mon|tue|wed|thu|fri)[^\n\d]*(\d{1,2}:\d{2})\s*(?:[-–—]|to)\s*(\d{1,2}:\d{2})",
        value,
        re.IGNORECASE,
    ):
        day = _day(match.group(1))
        ranged = _time_range(match.group(0))
        if day and ranged:
            start, end = ranged
            meetings.append({"day": day, "start_minute": start, "duration_minutes": end - start, "room": ""})
    return meetings


def normalize_sections(value: Any) -> list[dict[str, Any]]:
    """Return catalog sections with exact minutes and no raw text parsing left to clients."""

    value = _json(value)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return _text_sections(value)
    rows: list[dict[str, Any]] = []
    for index, record in enumerate(_records(value)):
        section_value = _value(record, ("section_number", "sectionnumber", "section", "sectionno", "sec"))
        section = str(section_value).strip() if section_value not in (None, "") else str(index + 1)
        instructor_value = _value(record, ("instructors", "instructor", "lecturer", "teacher"))
        instructor = (
            ", ".join(str(item).strip() for item in instructor_value if item)
            if isinstance(instructor_value, list)
            else str(instructor_value or "").strip()
        )
        schedule = _value(record, ("schedule", "meeting", "hours", "time"))
        candidates: list[dict[str, Any]] = []

        def visit(item: Any, found: list[dict[str, Any]]) -> None:
            item = _json(item)
            if isinstance(item, list):
                for child in item:
                    visit(child, found)
            elif isinstance(item, dict):
                if _meeting(item) is not None:
                    found.append(item)
                else:
                    for child in item.values():
                        visit(child, found)

        visit(schedule, candidates)
        meetings = [meeting for item in candidates if (meeting := _meeting(item)) is not None]
        if not meetings and isinstance(schedule, str):
            meetings.extend(_text_meetings(schedule))
        rows.append(
            {
                "section": section,
                "instructor": instructor,
                "meetings": meetings,
                "constraint": str(
                    _value(record, ("critical_info", "criticalinfo", "critical", "constraint", "kisit", "eklenti"))
                    or ""
                ).strip(),
            }
        )
    return rows

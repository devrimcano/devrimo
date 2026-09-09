"""Recover request identities from an old shared cache without inventing terms."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.core.digest import stable_digest


def cache_key(tool: str, values: dict[str, str]) -> str:
    return stable_digest({"namespace": "course-catalog", "identity": [
        tool, *(f"{key}={value}" for key, value in sorted(values.items())),
    ]})


def recover(rows: list[dict[str, Any]], terms: list[str] | None = None):
    """Only self-identifying or exact hash-matched requests are recoverable."""
    term_codes = set(terms or [])
    course_codes: set[str] = set()
    departments: set[str] = set()
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, dict):
            continue
        if payload.get("semester") and str(payload.get("course_code", "")).isdigit():
            term_codes.add(str(payload["semester"]))
            course_codes.add(str(payload["course_code"]))
        for item in payload.get("semesters", []):
            if isinstance(item, dict) and item.get("code"):
                term_codes.add(str(item["code"]))
        items = payload.get("result", payload.get("value", []))
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("course_code", ""))
                if len(code) == 7 and code.isdigit():
                    course_codes.add(code)
                    departments.add(code[:3])
                dept = str(item.get("code", ""))
                if len(dept) == 3 and dept.isdigit():
                    departments.add(dept)
    lookup: dict[str, tuple[str, dict[str, str]]] = {}
    for term in term_codes:
        for dept in departments:
            for tool in ("list_program_courses", "get_thesis_courses"):
                values = {"department": dept, "semester": term}
                lookup[cache_key(tool, values)] = tool, values
        for code in course_codes:
            for tool in ("get_course_prerequisites", "get_course_replacements"):
                values = {"department": code[:3], "semester": term, "course": code}
                lookup[cache_key(tool, values)] = tool, values
    lookup[cache_key("get_departments_and_semesters", {})] = "get_departments_and_semesters", {}
    accepted, unresolved = [], []
    counts: Counter = Counter()
    for row in rows:
        payload = row["payload"]
        match = lookup.get(row["key_hash"])
        if isinstance(payload, dict) and payload.get("course_code") and payload.get("semester"):
            code = str(payload["course_code"])
            values = {"department": code[:3], "semester": str(payload["semester"]), "course": code}
            if isinstance(payload.get("sections"), list):
                tool = "get_course_info"
            elif isinstance(payload.get("constraints"), list) and payload.get("section") is not None:
                tool = "get_section_constraints"
                values["section"] = str(payload["section"])
            else:
                tool = ""
            if tool and cache_key(tool, values) == row["key_hash"]:
                match = tool, values
        if match is None:
            unresolved.append({"key_hash": row["key_hash"], "reason": "request_identity_not_recoverable"})
            continue
        tool, values = match
        counts[tool] += 1
        accepted.append({**row, "tool": tool, "values": values})
    return accepted, unresolved, dict(counts)

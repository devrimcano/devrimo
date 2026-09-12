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


def bound(result: Any) -> Any:
    """Cap a result at the size every later model step of the turn pays for."""
    if isinstance(result, str) and len(result) > MAX_TOOL_RESULT_CHARS:
        return (
            result[:MAX_TOOL_RESULT_CHARS]
            + f"\n\n[Result truncated by Devrimo after {MAX_TOOL_RESULT_CHARS} characters. Narrow the query.]"
        )
    if isinstance(result, (dict, list)):
        import json

        serialized = json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) > MAX_TOOL_RESULT_CHARS:
            return {
                "truncated": True,
                "preview": serialized[:MAX_TOOL_RESULT_CHARS],
                "instruction": "Narrow the query before using this result.",
            }
    return result

"""One reading of an MCP tool result, shared by everything that calls one.

Agno hands back a result object whose useful payload can be in any of three
places: the protocol's ``structured_content`` metadata — typed, and what a
well-behaved server sends — a content block carrying a JSON document as text,
or a bare string. Reading only some of them is how two callers end up
disagreeing about what the same server said, which is precisely what happened
between the planning bridge and the schedule endpoints.
"""

import json
from typing import Any

_RESULT_WRAPPERS = frozenset({"result", "data", "content", "output", "payload", "response"})


def mcp_payload(result: Any) -> Any:
    """Return the payload an MCP tool result carries, preferring typed output."""
    metadata = getattr(result, "metadata", None) or {}
    structured = metadata.get("structured_content")
    if structured is not None:
        return _unwrap_envelope(structured)
    return _unwrap_content(getattr(result, "content", result))


def _unwrap_envelope(value: Any, depth: int = 0) -> Any:
    """Unwrap nested single-key MCP result envelopes.

    FastMCP wraps a tool result in a one-key envelope, and some Agno/FastMCP
    combinations add that envelope twice. Keep peeling known wrapper keys until
    the actual document is reached. Plain text stays inside its wrapper so
    callers can distinguish an error message from a structured answer.
    """
    if depth > 4 or not isinstance(value, dict) or len(value) != 1:
        return value
    key, only = next(iter(value.items()))
    if str(key).strip().lower() not in _RESULT_WRAPPERS:
        return value
    if isinstance(only, (dict, list)):
        return _unwrap_envelope(only, depth + 1)
    if isinstance(only, str):
        parsed = parse_json_document(only)
        if not isinstance(parsed, str):
            return _unwrap_envelope(parsed, depth + 1)
    return value


def parse_json_document(text: str) -> Any:
    """Parse ``text`` when it holds a JSON object or array, else return it as is.

    Deliberately not ``json.loads`` on every string. A bare ``"5670201"`` is a
    course code and ``"01"`` is a section number; decoding them turns an
    identifier into an integer and drops the leading zero, and ``"null"`` and
    ``"true"`` stop being text at all. Only a document — something that starts
    with ``{`` or ``[`` — is worth decoding.
    """
    stripped = text.strip()
    if stripped[:1] not in {"{", "["}:
        return text
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return text


def _unwrap_content(content: Any) -> Any:
    if isinstance(content, str):
        return parse_json_document(content)
    if isinstance(content, list):
        # A single textual block is the common FastMCP shape: the block's text
        # *is* the tool's JSON payload. Several blocks are a real list of
        # results and are left alone.
        texts = [
            item.get("text")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if len(texts) == 1:
            return parse_json_document(texts[0])
    if isinstance(content, dict):
        nested = content.get("content")
        if nested is not None:
            return _unwrap_content(nested)
        return _unwrap_envelope(content)
    return content

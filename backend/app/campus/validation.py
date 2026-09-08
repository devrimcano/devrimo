"""Validation for campus endpoints that cross the process boundary."""

import re
from urllib.parse import urlsplit

ODTUCLASS_BASE_HOST = "odtuclass.metu.edu.tr"
_ODTUCLASS_TERM_HOST = re.compile(
    r"^odtuclass\d{4}(?:sum|f|s)\.metu\.edu\.tr$",
    re.IGNORECASE | re.ASCII,
)


def validate_odtuclass_base_url(value: str | None) -> str | None:
    """Return a normalized ODTÜClass base URL or reject it.

    ODTÜClass publishes a stable entry point and semester hosts such as
    ``odtuclass2025f.metu.edu.tr``. The upstream MCP accepts this value as its
    HTTP base URL, so accepting an arbitrary URL would turn a student's campus
    connection into an SSRF and credential-forwarding primitive.
    """
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None

    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ODTUClass base URL is not a valid URL") from exc

    host = (parsed.hostname or "").rstrip(".").lower()
    approved_host = host == ODTUCLASS_BASE_HOST or bool(_ODTUCLASS_TERM_HOST.fullmatch(host))
    if parsed.scheme.lower() != "https" or not approved_host:
        raise ValueError("ODTUClass base URL must use HTTPS and an approved METU ODTUClass host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ODTUClass base URL cannot contain credentials, query parameters, or fragments")
    if port not in {None, 443}:
        raise ValueError("ODTUClass base URL must use the default HTTPS port")

    # Keep a caller's supported path, while removing a trailing slash so the
    # upstream client does not receive a doubled separator.
    return normalized.rstrip("/")

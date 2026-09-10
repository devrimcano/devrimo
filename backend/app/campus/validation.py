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

    # Rebuilt from the parts that were actually checked, rather than handed back
    # as typed. The check lowercases the host and drops a trailing root dot;
    # returning the raw string meant "https://ODTUCLASS.METU.EDU.TR./" passed
    # validation and then travelled to the upstream client exactly like that —
    # the same host as far as DNS is concerned, and a hostname some TLS stacks
    # will not match against the certificate's SAN, which surfaces to a student
    # as an ODTÜClass connection that inexplicably will not handshake.
    #
    # A caller's path is still kept, minus a trailing slash so the upstream
    # client does not build a doubled separator.
    return f"https://{host}{parsed.path.rstrip('/')}"

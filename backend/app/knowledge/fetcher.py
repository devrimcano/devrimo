"""A small fetcher with non-configurable SSRF and response-size boundaries."""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.config import get_settings
from app.knowledge.types import FetchedDocument


class FetchRejected(ValueError):
    pass


@dataclass(frozen=True)
class FetchPolicy:
    allowed_hosts: frozenset[str]
    respect_robots: bool = True
    max_redirects: int = 3


def _normalized_host(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise FetchRejected("Only credential-free HTTP(S) URLs are allowed")
    if parsed.port and parsed.port not in {80, 443}:
        raise FetchRejected("Non-standard ports are not allowed")
    return parsed.hostname.rstrip(".").lower()


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def _validate_destination(url: str, policy: FetchPolicy) -> tuple[str, tuple[str, ...]]:
    host = _normalized_host(url)
    if host not in policy.allowed_hosts:
        raise FetchRejected(f"Host is outside this source's allowlist: {host}")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchRejected(f"Host cannot be resolved: {host}") from exc
    addresses = {entry[4][0].split("%", 1)[0] for entry in infos}
    if not addresses or any(not _public_address(address) for address in addresses):
        raise FetchRejected("Private, local, or reserved network destinations are blocked")
    return host, tuple(sorted(addresses))


def _url_for_address(url: str, address: str) -> str:
    """Replace only the connection host, preserving path/query and scheme."""
    parsed = urlparse(url)
    rendered = f"[{address}]" if ":" in address else address
    if parsed.port:
        rendered = f"{rendered}:{parsed.port}"
    return parsed._replace(netloc=rendered).geturl()


async def _send_pinned(
    client: httpx.AsyncClient,
    url: str,
    policy: FetchPolicy,
    *,
    headers: dict[str, str] | None = None,
    stream: bool = False,
    avesis_query: dict | None = None,
) -> httpx.Response:
    """Resolve, validate, and connect to that exact address.

    TLS still verifies and sends SNI for the original hostname. DNS cannot be
    rebound between validation and the socket connect because httpx receives
    the already-resolved IP as its connection origin.
    """
    host, addresses = await _validate_destination(url, policy)
    parsed = urlparse(url)
    request_headers = {"Host": parsed.netloc, **(headers or {})}
    if avesis_query is not None and url != "https://avesis.metu.edu.tr/proxy/search/_search":
        raise FetchRejected("Search POST is restricted to the AVESIS directory")
    request = client.build_request(
        "POST" if avesis_query is not None else "GET",
        _url_for_address(url, addresses[0]), headers=request_headers,
        **({"json": avesis_query} if avesis_query is not None else {}),
    )
    request.extensions["sni_hostname"] = host
    return await client.send(request, stream=stream, follow_redirects=False)


async def _read_limited(response: httpx.Response, limit: int) -> bytes:
    size = 0
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise FetchRejected("Response exceeded the configured size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _robots_allowed(client: httpx.AsyncClient, url: str, policy: FetchPolicy, user_agent: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = await _send_pinned(client, robots_url, policy)
    except httpx.HTTPError:
        return True
    if response.status_code >= 400:
        return True
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(response.text.splitlines())
    return parser.can_fetch(user_agent, url)


async def fetch_document(
    url: str,
    policy: FetchPolicy,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> FetchedDocument:
    settings = get_settings()
    owned_client = client is None
    client = client or httpx.AsyncClient(
        timeout=settings.knowledge_fetch_timeout_seconds,
        headers={"User-Agent": settings.knowledge_fetch_user_agent},
        trust_env=False,
    )
    try:
        if policy.respect_robots and not await _robots_allowed(
            client, url, policy, settings.knowledge_fetch_user_agent
        ):
            raise FetchRejected("robots.txt does not allow this fetch")
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        current = url
        for _ in range(policy.max_redirects + 1):
            response = await _send_pinned(client, current, policy, headers=headers, stream=True)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise FetchRejected("Redirect did not include a destination")
                current = urljoin(current, location)
                continue
            if response.status_code == 304:
                await response.aclose()
                return FetchedDocument(current, b"", "", etag, last_modified, not_modified=True)
            response.raise_for_status()
            body = await _read_limited(response, settings.knowledge_fetch_max_bytes)
            await response.aclose()
            return FetchedDocument(
                url=current,
                body=body,
                content_type=response.headers.get("content-type", "application/octet-stream"),
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            )
        raise FetchRejected("Redirect limit exceeded")
    finally:
        if owned_client:
            await client.aclose()

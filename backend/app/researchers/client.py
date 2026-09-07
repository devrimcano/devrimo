"""Polite AVESIS-only transport, including optional authenticated CONNECT proxy."""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.knowledge.fetcher import FetchPolicy, FetchRejected, _read_limited, _send_pinned
from app.logging import get_logger
from app.researchers.proxy import AvesisProxyTransport

logger = get_logger(__name__)

ORIGIN = "https://avesis.metu.edu.tr"
SEARCH_URL = ORIGIN + "/proxy/search/_search"
USER_AGENT = "DevrimoAcademicImporter/1.0"
POLICY = FetchPolicy(frozenset({"avesis.metu.edu.tr"}))


class ImportFailure(ValueError):
    """Safe to print: never wrap raw transport errors or credential-bearing URLs."""


class StopImport(ImportFailure):
    """Access denial or an unavailable configured proxy requires a checkpoint."""


@dataclass
class Page:
    url: str
    body: bytes
    content_type: str


def retry_delay(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class AvesisClient:
    def __init__(self, proxy_url: str | None = None, *, interval: float = 1.0, client=None):
        if proxy_url:
            proxy = urlsplit(proxy_url)
            if proxy.scheme != "http" or not proxy.hostname or proxy.query or proxy.fragment:
                raise ImportFailure("AVESIS_PROXY_URL must be an HTTP proxy URL")
        self.proxied = bool(proxy_url)
        self.client = client or httpx.AsyncClient(
            transport=AvesisProxyTransport(proxy_url) if proxy_url else None,
            trust_env=False,
            timeout=30,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )
        self.interval = max(1.0, interval)
        self.next_request = 0.0
        self.robots = None
        self.requests = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def _pause(self, seconds=0):
        remaining = max(self.next_request - time.monotonic(), seconds)
        # Chunk long waits so operators see that the process is alive.
        while remaining > 0:
            step = min(remaining, 30)
            if remaining > 30:
                logger.info("researcher_fetch_backoff", remaining_seconds=int(remaining))
            await asyncio.sleep(step)
            remaining -= step
        self.next_request = time.monotonic() + self.interval

    async def initialize(self):
        if self.robots is not None:
            return
        page = await self.fetch(ORIGIN + "/robots.txt", robots=False, missing_ok=True)
        parser = RobotFileParser()
        parser.set_url(ORIGIN + "/robots.txt")
        parser.parse(page.body.decode("utf-8").splitlines())
        self.robots = parser
        self.interval = max(self.interval, parser.crawl_delay(USER_AGENT) or 0)
        rate = parser.request_rate(USER_AGENT)
        if rate and rate.requests:
            self.interval = max(self.interval, rate.seconds / rate.requests)

    async def fetch(self, url: str, *, query: dict | None = None, robots=True, missing_ok=False) -> Page:
        if robots:
            await self.initialize()
        current = url
        for _redirect in range(4):
            target = urlsplit(current)
            if target.scheme != "https" or target.hostname != "avesis.metu.edu.tr":
                raise ImportFailure("AVESIS redirected outside the HTTPS source allowlist")
            if robots and not self.robots.can_fetch(USER_AGENT, current):
                raise ImportFailure("AVESIS robots.txt excludes this route")
            response = None
            for attempt in range(4):
                await self._pause()
                try:
                    self.requests += 1
                    response = await _send_pinned(
                        self.client,
                        current,
                        POLICY,
                        stream=True,
                        avesis_query=query,
                    )
                    code = response.status_code
                    logger.info(
                        "researcher_fetch_response", status_code=code, attempt=attempt + 1, request_number=self.requests
                    )
                    if code in {401, 403, 407}:
                        raise StopImport(
                            "Proxy authentication failed" if code == 407 else f"AVESIS denied access ({code})"
                        )
                    if code == 429 or code >= 500:
                        delay = max(2 ** (attempt + 1), retry_delay(response.headers.get("Retry-After")))
                        await response.aclose()
                        if attempt == 3:
                            raise StopImport(f"AVESIS remains unavailable ({code}); resume later")
                        logger.warning(
                            "researcher_fetch_retry", status_code=code, attempt=attempt + 1, delay_seconds=delay
                        )
                        await self._pause(delay)
                        continue
                    break
                except httpx.HTTPError as exc:
                    logger.warning(
                        "researcher_fetch_transport_error", error_type=type(exc).__name__, attempt=attempt + 1
                    )
                    if attempt == 3 or isinstance(exc, httpx.ProxyError):
                        message = "Configured proxy connection failed" if self.proxied else "AVESIS connection failed"
                        raise StopImport(message) from None
                    await self._pause(2 ** (attempt + 1))
                except FetchRejected:
                    raise ImportFailure("AVESIS destination or response rejected by fetch policy") from None
                finally:
                    if response is not None and response.status_code >= 400:
                        await response.aclose()
            if response is None:
                raise StopImport("AVESIS did not return a response")
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if query is not None:
                        raise ImportFailure("Unexpected redirect from AVESIS search")
                    location = response.headers.get("location")
                    if not location:
                        raise ImportFailure("AVESIS redirect has no destination")
                    current = urljoin(current, location)
                    continue
                if response.status_code == 404 and missing_ok:
                    return Page(current, b"", "text/plain")
                if response.status_code != 200:
                    raise ImportFailure(f"AVESIS HTTP {response.status_code}")
                body = await _read_limited(response, 8 * 1024 * 1024)
                return Page(current, body, response.headers.get("content-type", ""))
            except httpx.HTTPError:
                raise ImportFailure("AVESIS response interrupted; previous section preserved") from None
            finally:
                await response.aclose()
        raise ImportFailure("AVESIS redirect limit exceeded")

"""AVESIS-only CONNECT transport that pins the destination and verifies its DNS name.

httpcore's standard proxy tunnel ignores the request's sni_hostname extension.
This backend supplies the fixed, allowlisted AVESIS hostname when that tunnel
starts TLS. CONNECT still targets the address validated by the guarded fetcher.
"""

import ssl
from contextlib import contextmanager

import httpcore
import httpx


@contextmanager
def mapped_errors():
    try:
        yield
    except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError, httpcore.ProxyError) as exc:
        error = getattr(httpx, type(exc).__name__, httpx.TransportError)
        raise error("AVESIS proxy transport failed") from None


class AvesisStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream):
        self.stream = stream

    async def read(self, max_bytes, timeout=None):
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        await self.stream.write(buffer, timeout)

    async def aclose(self):
        await self.stream.aclose()

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return await self.stream.start_tls(ssl_context, server_hostname="avesis.metu.edu.tr", timeout=timeout)

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)


class AvesisBackend(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return AvesisStream(await self.backend.connect_tcp(host, port, timeout, local_address, socket_options))


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        with mapped_errors():
            async for chunk in self.stream:
                yield chunk

    async def aclose(self):
        await self.stream.aclose()


class AvesisProxyTransport(httpx.AsyncBaseTransport):
    def __init__(self, proxy_url, *, ssl_context=None):
        proxy = httpx.Proxy(proxy_url)
        if proxy.url.scheme != "http":
            raise ValueError("Only HTTP CONNECT proxies are supported")
        self.pool = httpcore.AsyncHTTPProxy(
            proxy_url=str(proxy.url),
            proxy_auth=proxy.raw_auth,
            ssl_context=ssl_context or ssl.create_default_context(),
            network_backend=AvesisBackend(),
            max_connections=1,
            max_keepalive_connections=1,
        )

    async def handle_async_request(self, request):
        if request.extensions.get("sni_hostname") != "avesis.metu.edu.tr" or request.url.scheme != "https":
            raise httpx.ProxyError("Proxy transport is restricted to guarded AVESIS requests")
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with mapped_errors():
            response = await self.pool.handle_async_request(core_request)
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=ResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.pool.aclose()

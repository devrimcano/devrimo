import json
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text

from app.db.session import SessionLocal, engine
from app.researchers.client import AvesisClient, ImportFailure, Page, StopImport
from app.researchers.discovery import discover
from app.researchers.models import Researcher, ResearcherImportItem, ResearcherImportRun, ResearcherSection
from app.researchers.parser import parse_page
from app.researchers.service import LOCK_ID, process_item, synchronize

ORIGIN = "https://avesis.metu.edu.tr"
IDENTITY = {
    "id": 1,
    "network_id": 101,
    "alias": "person",
    "source_url": ORIGIN + "/person",
    "display_name": "Prof. Person",
}
GENERAL = b"""<html lang="en"><h1>Prof. Person</h1><img src="/user/image/1">
<ul id="primary-menu"><li><a href="/person/publications">Publications</a></li></ul>
<section id="main-area"><span class="corporateInformation">Engineering, Computing</span>
<strong>Research Areas:</strong><span>Software</span><a href="mailto:person@metu.edu.tr">Email</a></section></html>"""
PUBLICATIONS = b"""<html lang="en"><h1>Prof. Person</h1><section id="main-area">
<div class="pub-item"><h3>1. A study</h3>
<a href="/publication/details/12345678-abcd-abcd-abcd-123456789abc/a-study">A study</a></div>
<div class="pub-item"><h3>1. A study</h3>
<a href="/publication/details/12345678-abcd-abcd-abcd-123456789abc/a-study">A study</a></div>
<script>do_not_store_this()</script></section></html>"""
DETAIL = b'<html lang="en"><main><h1>A study</h1><p>Abstract: actual academic content</p></main></html>'


class FakeClient:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.general = GENERAL

    async def fetch(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith("publications"):
            if self.fail:
                raise ImportFailure("temporary failure")
            return Page(url, PUBLICATIONS, "text/html")
        if "/publication/details/" in url:
            return Page(url, DETAIL, "text/html")
        return Page(url, self.general, "text/html")


def test_parse_english_original_titles_and_duplicates():
    parsed = parse_page(
        PUBLICATIONS.replace(b"A study", "Türkçe çalışma".encode()), ORIGIN + "/person/publications", "person"
    )
    assert len(parsed.content["items"]) == len(parsed.details) == 1
    assert "Türkçe çalışma" in parsed.content["items"][0]["text"]
    assert "do_not_store" not in parsed.content["text"]
    assert parsed.sections == {}


def test_general_fields_and_future_language_guard():
    parsed = parse_page(GENERAL, ORIGIN + "/person", "person", expected_id=1)
    assert parsed.fields["name"] == "Person"
    assert parsed.fields["email"] == "person@metu.edu.tr"
    assert parsed.sections == {"publications": ORIGIN + "/person/publications"}
    with pytest.raises(ImportFailure, match="English"):
        parse_page(GENERAL.replace(b'lang="en"', b'lang="tr"'), ORIGIN + "/person", "person")
    with pytest.raises(ImportFailure, match="identity"):
        parse_page(GENERAL, ORIGIN + "/person", "person", expected_id=2)


def test_challenge_and_missing_structure():
    with pytest.raises(StopImport):
        parse_page(b'<html lang="en"><title>Just a moment</title></html>', ORIGIN, "person")
    with pytest.raises(ImportFailure):
        parse_page(b'<html lang="en"><h1>Error</h1></html>', ORIGIN, "person")


async def new_item(db):
    run = ResearcherImportRun(options={"language": "en"})
    db.add(run)
    await db.flush()
    item = ResearcherImportItem(run_id=run.id, source_id=1, identity=IDENTITY)
    db.add(item)
    await db.commit()
    return item


async def test_idempotent_import_and_section_failure_preserves_content():
    client = FakeClient()
    async with SessionLocal() as db:
        first = await new_item(db)
        await process_item(db, client, first)
        assert first.status == "completed"
        assert first.outcome == "created"
        assert await db.scalar(select(func.count()).select_from(ResearcherSection)) == 3
        second = await new_item(db)
        await process_item(db, client, second)
        assert second.outcome == "unchanged"
        assert await db.scalar(select(func.count()).select_from(Researcher)) == 1
        original = await db.scalar(
            select(ResearcherSection.content_hash).where(ResearcherSection.section == "publications")
        )
        third = await new_item(db)
        client.fail = True
        await process_item(db, client, third)
        assert third.status == "incomplete"
        assert (
            await db.scalar(select(ResearcherSection.content_hash).where(ResearcherSection.section == "publications"))
            == original
        )
        client.fail = False
        await process_item(db, client, third)
        assert third.status == "completed"
        assert not third.errors


async def test_rename_updates_same_researcher_and_turkish_can_attach():
    async with SessionLocal() as db:
        client = FakeClient()
        item = await new_item(db)
        await process_item(db, client, item)
        client.general = GENERAL.replace(b"Prof. Person", b"Prof. New Name")
        item2 = await new_item(db)
        await process_item(db, client, item2)
        assert item2.outcome == "updated"
        assert (await db.get(Researcher, 1)).name == "New Name"
        db.add(
            ResearcherSection(
                researcher_id=1,
                section="general",
                language="tr",
                source_url=ORIGIN,
                content={"text": "Turkish later"},
                content_hash="tr",
            )
        )
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(Researcher)) == 1
        assert await db.scalar(select(func.count()).select_from(ResearcherSection)) == 4


async def test_resume_skips_saved_sections():
    async with SessionLocal() as db:
        client = FakeClient()
        item = await new_item(db)
        await process_item(db, client, item)
        client.calls.clear()
        await process_item(db, client, item)
        assert client.calls == [ORIGIN + "/person"]


async def test_lock_blocks_concurrent_import():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_ID})
        await conn.commit()
        try:
            with pytest.raises(ImportFailure, match="already running"):
                await synchronize(engine, FakeClient())
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})


async def test_sync_report_and_resume(monkeypatch):
    async def fake_discover(client):
        return [IDENTITY], {"complete": True, "reported_total": 1, "sitemap_only": []}

    monkeypatch.setattr("app.researchers.service.discover", fake_discover)
    result = await synchronize(engine, FakeClient(), limit=1, progress=lambda _: None)
    assert result["status"] == "completed"
    assert result["outcomes"] == {"created": 1}
    assert (await synchronize(engine, FakeClient(), resume=uuid.UUID(result["run_id"]))) == result


async def test_discovery_reconciles_and_rejects_duplicate_pages():
    class Directory:
        async def fetch(self, url, query=None):
            if query:
                sources = [
                    {"_source": {"id": i, "networkuserid": i + 100, "profilepagealias": f"p{i}"}} for i in range(1, 51)
                ]
                return Page(url, json.dumps({"hits": {"total": 51, "hits": sources}}).encode(), "application/json")
            return Page(url, b"", "text/xml")

    with pytest.raises(ImportFailure, match="repeated"):
        await discover(Directory())


async def test_transport_pins_ip_preserves_sni_and_post_scope(monkeypatch):
    from app.knowledge.fetcher import FetchRejected, _send_pinned
    from app.researchers.client import POLICY, SEARCH_URL

    async def resolve(url, policy):
        return "avesis.metu.edu.tr", ("144.122.1.1",)

    monkeypatch.setattr("app.knowledge.fetcher._validate_destination", resolve)
    captured = []

    async def handler(request):
        captured.append(request)
        return httpx.Response(200, content=b"{}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await _send_pinned(client, SEARCH_URL, POLICY, avesis_query={"size": 50})
        request = captured[0]
        assert request.url.host == "144.122.1.1"
        assert request.headers["Host"] == "avesis.metu.edu.tr"
        assert request.extensions["sni_hostname"] == "avesis.metu.edu.tr"
        assert request.method == "POST"
        with pytest.raises(FetchRejected):
            await _send_pinned(client, ORIGIN + "/other", POLICY, avesis_query={})


async def test_transport_robots_rate_limit_and_redirection(monkeypatch):
    from urllib.robotparser import RobotFileParser

    responses = [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, content=b"ok")]

    async def send(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr("app.researchers.client._send_pinned", send)
    async with AvesisClient() as client:
        waits = []

        async def pause(seconds=0):
            waits.append(seconds)

        client._pause = pause
        client.robots = RobotFileParser()
        client.robots.parse(["User-agent: *", "Disallow: /person/download"])
        assert (await client.fetch(ORIGIN + "/person")).body == b"ok"
        assert 3 in waits
        with pytest.raises(ImportFailure, match="robots"):
            await client.fetch(ORIGIN + "/person/download")
        responses.append(httpx.Response(302, headers={"Location": "https://outside.example/"}))
        with pytest.raises(ImportFailure, match="allowlist"):
            await client.fetch(ORIGIN + "/person")


async def test_proxy_errors_never_expose_credentials(monkeypatch):
    async def send(*args, **kwargs):
        raise httpx.ProxyError("http://secret-user:secret-password@proxy.example:8080")

    monkeypatch.setattr("app.researchers.client._send_pinned", send)
    async with AvesisClient("http://secret-user:secret-password@proxy.example:8080") as client:
        with pytest.raises(StopImport, match="Configured proxy connection failed") as exc:
            await client.fetch(ORIGIN + "/robots.txt", robots=False)
        assert "secret" not in str(exc.value)


async def test_empty_sections_and_unknown_sections():
    page = b'<html lang="en"><h1>Person</h1><section id="main-area"><h2>New academic section</h2></section></html>'
    parsed = parse_page(page, ORIGIN + "/person/newsection", "person")
    assert parsed.content["warnings"]
    assert parsed.content["text"] == "New academic section"


async def test_authenticated_connect_proxy_preserves_tls_hostname(monkeypatch, tmp_path):
    """Exercise a real local CONNECT tunnel, not just HTTPX constructor arguments."""
    import asyncio
    import base64
    import ssl
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from app.knowledge.fetcher import _send_pinned
    from app.researchers.client import POLICY
    from app.researchers.proxy import AvesisProxyTransport

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "avesis.metu.edu.tr")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("avesis.metu.edu.tr")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ssl.load_cert_chain(cert_path, key_path)
    observed = {}

    def sni_callback(socket, hostname, context):
        observed["sni"] = hostname

    server_ssl.set_servername_callback(sni_callback)

    async def origin(reader, writer):
        observed["request"] = await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    async with await asyncio.start_server(origin, "127.0.0.1", 0, ssl=server_ssl) as origin_server:
        origin_port = origin_server.sockets[0].getsockname()[1]
        proxy_done = asyncio.Event()

        async def proxy(reader, writer):
            try:
                observed["connect"] = await reader.readuntil(b"\r\n\r\n")
                upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", origin_port)
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()

                async def relay(source, target):
                    while chunk := await source.read(65536):
                        target.write(chunk)
                        await target.drain()

                tasks = [
                    asyncio.create_task(relay(reader, upstream_writer)),
                    asyncio.create_task(relay(upstream_reader, writer)),
                ]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                upstream_writer.close()
                writer.close()
            finally:
                proxy_done.set()

        async with await asyncio.start_server(proxy, "127.0.0.1", 0) as proxy_server:
            proxy_port = proxy_server.sockets[0].getsockname()[1]

            async def resolve(url, policy):
                return "avesis.metu.edu.tr", ("127.0.0.1",)

            monkeypatch.setattr("app.knowledge.fetcher._validate_destination", resolve)
            tls = ssl.create_default_context(cafile=str(cert_path))
            async with httpx.AsyncClient(
                transport=AvesisProxyTransport(f"http://user:password@127.0.0.1:{proxy_port}", ssl_context=tls),
                trust_env=False,
            ) as client:
                response = await _send_pinned(client, f"https://avesis.metu.edu.tr:{origin_port}/profile", POLICY)
                assert response.text == "ok"
            await asyncio.wait_for(proxy_done.wait(), timeout=5)
    assert f"CONNECT 127.0.0.1:{origin_port}".encode() in observed["connect"]
    assert b"Proxy-Authorization: Basic " + base64.b64encode(b"user:password") in observed["connect"]
    assert observed["sni"] == "avesis.metu.edu.tr"
    assert b"Proxy-Authorization" not in observed["request"]


async def test_discovery_success_reports_sitemap_difference():
    class Directory:
        async def fetch(self, url, query=None):
            if query:
                return Page(
                    url,
                    json.dumps(
                        {
                            "hits": {
                                "total": 1,
                                "hits": [{"_source": {"id": 1, "networkuserid": 101, "profilepagealias": "person"}}],
                            }
                        }
                    ).encode(),
                    "application/json",
                )
            return Page(
                url,
                b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<url><loc>https://avesis.metu.edu.tr/person</loc></url>"
                b"<url><loc>https://avesis.metu.edu.tr/unmatched</loc></url></urlset>",
                "text/xml",
            )

    identities, result = await discover(Directory())
    assert len(identities) == 1
    assert result["sitemap_only"] == ["unmatched"]


async def test_partial_section_does_not_stop_other_researchers(monkeypatch):
    second = {**IDENTITY, "id": 2, "alias": "second", "source_url": ORIGIN + "/second"}

    async def fake_discover(client):
        return [IDENTITY, second], {"complete": True, "sitemap_only": []}

    class PartialClient(FakeClient):
        async def fetch(self, url, **kwargs):
            if url.endswith("/person/publications"):
                raise ImportFailure("section temporarily missing")
            if url.endswith("/second"):
                return Page(
                    url, GENERAL.replace(b"/person/", b"/second/").replace(b"/image/1", b"/image/2"), "text/html"
                )
            return await super().fetch(url, **kwargs)

    monkeypatch.setattr("app.researchers.service.discover", fake_discover)
    result = await synchronize(engine, PartialClient(), progress=lambda _: None)
    assert result["status"] == "incomplete"
    assert result["counts"] == {"incomplete": 1, "completed": 1}


async def test_section_pagination_and_loop_detection():
    from app.researchers.service import collect_section

    class Paginated:
        repeated = False

        async def fetch(self, url):
            if "page=2" in url:
                body = (
                    PUBLICATIONS
                    if self.repeated
                    else PUBLICATIONS.replace(b"A study", b"Second study").replace(b"12345678-abcd", b"87654321-abcd")
                )
            else:
                body = PUBLICATIONS.replace(
                    b"</section>", b'<div class="pagination"><a href="?page=2">2</a></div></section>'
                )
            return Page(url, body, "text/html")

    client = Paginated()
    result = await collect_section(client, ORIGIN + "/person/publications", IDENTITY)
    assert len(result["items"]) == 2
    client.repeated = True
    with pytest.raises(ImportFailure, match="repeated"):
        await collect_section(client, ORIGIN + "/person/publications", IDENTITY)

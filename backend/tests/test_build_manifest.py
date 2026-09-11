"""The build manifest: can a running container say which commits it contains?

The campus MCP servers are pinned by build arg and the image build deletes
their ``.git`` directories, so this file and the ``/health`` field it feeds are
the only way to verify a pin from a live deployment. The absent-manifest case
matters as much as the happy path: local development runs the broker with no
campus servers installed, and health must stay green there.
"""

from app.campus.manifest import BuildRecord, commits_by_slug, read_manifest

SAIS = "33c228842fcbe44e3459145055ff28a5e13bae64"
WEBMAIL = "af0fe0503806db5d704e041866853dcbbe98e25f"


def write_manifest(tmp_path, body: str) -> str:
    (tmp_path / "MANIFEST").write_text(body, encoding="utf-8")
    read_manifest.cache_clear()
    return str(tmp_path)


def test_reads_slug_commit_and_repo(tmp_path):
    root = write_manifest(
        tmp_path,
        f"sais {SAIS} https://github.com/atesahmet0/metu-sais-mcp.git\n"
        f"webmail {WEBMAIL} https://github.com/atesahmet0/metu-webmail-mcp.git\n",
    )

    assert read_manifest(root) == (
        BuildRecord("sais", SAIS, "https://github.com/atesahmet0/metu-sais-mcp.git"),
        BuildRecord("webmail", WEBMAIL, "https://github.com/atesahmet0/metu-webmail-mcp.git"),
    )
    assert commits_by_slug(root) == {"sais": SAIS, "webmail": WEBMAIL}


def test_missing_manifest_is_empty_not_an_error(tmp_path):
    """Local dev has no campus servers installed; that is not a failure."""
    read_manifest.cache_clear()
    assert read_manifest(str(tmp_path / "nonexistent")) == ()
    assert commits_by_slug(str(tmp_path / "nonexistent")) == {}


def test_malformed_line_does_not_hide_the_others(tmp_path):
    """Three reported servers beat a health check that fails over a fourth."""
    root = write_manifest(
        tmp_path,
        f"sais {SAIS} https://github.com/atesahmet0/metu-sais-mcp.git\n"
        "\n"
        "truncated-line\n"
        f"webmail {WEBMAIL} https://github.com/atesahmet0/metu-webmail-mcp.git\n",
    )

    assert commits_by_slug(root) == {"sais": SAIS, "webmail": WEBMAIL}


async def test_build_manifest_reports_campus_commits(client, tmp_path, monkeypatch):
    """The pin is only verifiable if a running process will state it."""
    from app import main
    from app.campus.manifest import read_manifest

    (tmp_path / "MANIFEST").write_text(
        f"sais {SAIS} https://github.com/atesahmet0/metu-sais-mcp.git\n", encoding="utf-8"
    )
    read_manifest.cache_clear()
    monkeypatch.setattr(main.settings, "campus_mcp_root", str(tmp_path))

    response = await client.get("/internal/build-manifest")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "campus_servers": {"sais": SAIS}}
    read_manifest.cache_clear()


async def test_health_stays_liveness_only(client):
    """The public edge serves /health; it must not describe the deployment."""
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

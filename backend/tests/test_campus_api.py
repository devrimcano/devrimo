"""Campus connection + onboarding endpoints, and the MCP config they render.

Credential verification is stubbed throughout: these tests assert what the
broker does with a verification result, never that METU's SSO behaves a
particular way.
"""

import pytest
from sqlalchemy import select

from app.api.v1 import campus as campus_routes
from app.campus import service as campus_service
from app.campus import throttle as campus_throttle
from app.campus.session_pool import session_count
from app.campus.verify import VerificationResult
from app.config import get_settings
from app.db.models import CampusCredential
from app.db.session import SessionLocal
from tests.conftest import auth_header, new_user_id


async def _specs_for(user_id):
    """The campus servers this student's agent would actually be launched with.

    The container-era tests inspected the config file written into a container;
    the equivalent now is the spec list the pool builds toolkits from.
    """
    async with SessionLocal() as db:
        return await campus_service.campus_server_specs(db, user_id)


@pytest.fixture(autouse=True)
def _fresh_verification_budget():
    """How often METU may be asked about a password is per-process state.

    Without this, a test that spends the budget would be answered 429 in the
    next test, for something the next test did not do — and which test failed
    would depend on collection order.
    """
    campus_throttle.reset()
    yield
    campus_throttle.reset()


@pytest.fixture
def accept_credentials(monkeypatch):
    async def _ok(username: str, password: str, timeout: float = 20.0) -> VerificationResult:
        return VerificationResult(ok=True)

    monkeypatch.setattr(campus_routes, "verify_metu_credentials", _ok)


@pytest.fixture
def reject_credentials(monkeypatch):
    async def _no(username: str, password: str, timeout: float = 20.0) -> VerificationResult:
        return VerificationResult(ok=False, detail="METU rejected the sign-in.")

    monkeypatch.setattr(campus_routes, "verify_metu_credentials", _no)


async def test_connection_starts_empty_but_lists_the_catalog(client):
    user_id = new_user_id()
    response = await client.get("/api/v1/campus/connection", headers=auth_header(user_id))

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert {tool["id"] for tool in body["tools"]} == {"sais", "course_info", "odtuclass", "webmail"}
    assert all(tool["active"] is False for tool in body["tools"])


async def test_connect_stores_credentials_and_never_returns_them(client, accept_credentials):
    user_id = new_user_id()
    response = await client.put(
        "/api/v1/campus/connection",
        headers=auth_header(user_id),
        json={
            "metu_username": "E123456@metu.edu.tr",
            "metu_password": "hunter2",
            "locale": "en",
            "enabled_tools": ["sais", "odtuclass"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    # Normalized: the domain is stripped and the case folded.
    assert body["metu_username"] == "e123456"
    assert body["has_password"] is True
    assert body["verified_at"] is not None
    assert body["enabled_tools"] == ["sais", "odtuclass"]
    assert "hunter2" not in response.text


async def test_rejected_credentials_are_not_stored(client, reject_credentials):
    user_id = new_user_id()
    response = await client.put(
        "/api/v1/campus/connection",
        headers=auth_header(user_id),
        json={"metu_username": "e123456", "metu_password": "wrong"},
    )
    assert response.status_code == 400

    after = await client.get("/api/v1/campus/connection", headers=auth_header(user_id))
    assert after.json()["connected"] is False


async def test_password_checking_is_not_an_unlimited_oracle(client, reject_credentials):
    """The endpoint answers "was that password right" for any username given.

    It is authenticated, but the username never had to be the caller's own, and
    METU sees the attempts as this host's. Left unlimited, one signed-up account
    could walk a list of student credentials through it. The counter is tested
    in tests/test_campus_throttle.py; this is about the endpoint being wired to
    it at all.
    """
    user_id = new_user_id()
    for _ in range(campus_throttle.PER_USER_ATTEMPTS):
        allowed = await client.post(
            "/api/v1/campus/connection/verify",
            headers=auth_header(user_id),
            json={"metu_username": "e123456", "metu_password": "guess"},
        )
        assert allowed.status_code == 200

    refused = await client.post(
        "/api/v1/campus/connection/verify",
        headers=auth_header(user_id),
        json={"metu_username": "e123457", "metu_password": "guess"},
    )
    assert refused.status_code == 429
    assert refused.headers.get("Retry-After")

    # The other door onto the same oracle draws on the same budget, or the
    # limit on this one is decoration.
    other_door = await client.put(
        "/api/v1/campus/connection",
        headers=auth_header(user_id),
        json={"metu_username": "e123458", "metu_password": "guess"},
    )
    assert other_door.status_code == 429

    # And it is this account that is out of budget, not the service.
    someone_else = await client.post(
        "/api/v1/campus/connection/verify",
        headers=auth_header(new_user_id()),
        json={"metu_username": "e999999", "metu_password": "guess"},
    )
    assert someone_else.status_code == 200


async def test_unreachable_sso_saves_unverified_rather_than_blocking(client, monkeypatch):
    async def _down(username: str, password: str, timeout: float = 20.0) -> VerificationResult:
        return VerificationResult(ok=False, unreachable=True, detail="Could not reach METU sign-in right now.")

    monkeypatch.setattr(campus_routes, "verify_metu_credentials", _down)

    user_id = new_user_id()
    response = await client.put(
        "/api/v1/campus/connection",
        headers=auth_header(user_id),
        json={"metu_username": "e123456", "metu_password": "hunter2"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["verified_at"] is None
    assert body["verification_error"]


async def test_password_may_be_omitted_when_only_toggling_tools(client, accept_credentials):
    user_id = new_user_id()
    headers = auth_header(user_id)
    await client.put(
        "/api/v1/campus/connection",
        headers=headers,
        json={"metu_username": "e123456", "metu_password": "hunter2", "enabled_tools": ["sais"]},
    )

    response = await client.put(
        "/api/v1/campus/connection",
        headers=headers,
        json={"metu_username": "e123456", "enabled_tools": ["sais", "webmail"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled_tools"] == ["sais", "webmail"]
    assert body["has_password"] is True
    # The earlier verification carries forward rather than being reset.
    assert body["verified_at"] is not None


async def test_every_saved_connection_advances_credential_revision(client, accept_credentials):
    user_id = new_user_id()
    headers = auth_header(user_id)
    await client.put(
        "/api/v1/campus/connection",
        headers=headers,
        json={"metu_username": "e123456", "metu_password": "first", "enabled_tools": ["sais"]},
    )
    async with SessionLocal() as db:
        first = (await db.execute(select(CampusCredential).where(CampusCredential.user_id == user_id))).scalar_one()
        assert first.credential_revision == 1

    await client.put(
        "/api/v1/campus/connection",
        headers=headers,
        json={"metu_username": "e123456", "metu_password": "second", "enabled_tools": ["sais"]},
    )
    async with SessionLocal() as db:
        second = (await db.execute(select(CampusCredential).where(CampusCredential.user_id == user_id))).scalar_one()
        assert second.credential_revision == 2


async def test_first_connect_requires_a_password(client, accept_credentials):
    response = await client.put(
        "/api/v1/campus/connection",
        headers=auth_header(new_user_id()),
        json={"metu_username": "e123456"},
    )
    assert response.status_code == 422


async def test_disconnect_forgets_the_credentials(client, accept_credentials):
    user_id = new_user_id()
    headers = auth_header(user_id)
    await client.put(
        "/api/v1/campus/connection",
        headers=headers,
        json={"metu_username": "e123456", "metu_password": "hunter2"},
    )

    response = await client.delete("/api/v1/campus/connection", headers=headers)
    assert response.status_code == 200
    assert response.json()["connected"] is False


async def test_agent_container_is_built_with_the_students_mcp_config(client, accept_credentials):
    user_id = new_user_id()
    headers = auth_header(user_id)
    await client.put(
        "/api/v1/campus/connection",
        headers=headers,
        json={
            "metu_username": "e123456",
            "metu_password": "hunter2",
            "enabled_tools": ["sais", "webmail"],
        },
    )

    provision = await client.post("/api/v1/agents/provision", headers=headers)
    assert provision.status_code == 201

    specs = await _specs_for(user_id)
    by_id = {spec.tool_id: spec for spec in specs}

    assert set(by_id) == {"sais", "webmail"}
    assert by_id["sais"].env["SAIS_USERNAME"] == "e123456"
    assert by_id["sais"].env["SAIS_PASSWORD"] == "hunter2"
    assert by_id["webmail"].env["METU_PASSWORD"] == "hunter2"


async def test_agent_without_a_connection_gets_no_campus_servers(client):
    user_id = new_user_id()
    headers = auth_header(user_id)
    await client.post("/api/v1/agents/provision", headers=headers)

    assert await _specs_for(user_id) == []
    # And is still perfectly usable, just without campus tools.
    response = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}], "session_id": "t1"},
    )
    assert response.status_code == 200


async def test_changing_the_connection_invalidates_integration_sessions(client, accept_credentials, monkeypatch):
    """A revoked tool has to stop being available immediately, not next session."""
    user_id = new_user_id()
    headers = auth_header(user_id)
    await client.post("/api/v1/agents/provision", headers=headers)
    await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}], "session_id": "t1"},
    )
    from unittest.mock import AsyncMock

    retired = AsyncMock()
    monkeypatch.setattr("app.agents.manager.retire_user", retired)
    assert session_count(user_id) == 0

    response = await client.put(
        "/api/v1/campus/connection",
        headers=headers,
        json={"metu_username": "e123456", "metu_password": "hunter2", "enabled_tools": ["sais"]},
    )
    assert response.status_code == 200
    # Applied eagerly, so the student doesn't have to restart anything.
    assert response.json()["needs_restart"] is False

    retired.assert_awaited_with(user_id)
    assert session_count(user_id) == 0
    assert {spec.tool_id for spec in await _specs_for(user_id)} == {"sais"}


async def test_connections_are_isolated_per_user(client, accept_credentials):
    first, second = new_user_id(), new_user_id()
    await client.put(
        "/api/v1/campus/connection",
        headers=auth_header(first),
        json={"metu_username": "e111111", "metu_password": "one"},
    )

    response = await client.get("/api/v1/campus/connection", headers=auth_header(second))
    assert response.json()["connected"] is False


async def test_campus_endpoints_require_auth(client):
    assert (await client.get("/api/v1/campus/connection")).status_code == 401
    assert (await client.get("/api/v1/profile")).status_code == 401


async def test_a_change_made_while_stopped_is_applied_on_restart(client, accept_credentials):
    """The stale-toolset case, which used to outlive the config it was built with."""
    user_id = new_user_id()
    headers = auth_header(user_id)
    await client.post("/api/v1/agents/provision", headers=headers)

    stopped = await client.post("/api/v1/agents/stop", headers=headers)
    assert stopped.json()["status"] == "stopped"

    # Revision-scoped integrations use saved credentials without a model restart.
    saved = await client.put(
        "/api/v1/campus/connection",
        headers=headers,
        json={"metu_username": "e123456", "metu_password": "hunter2", "enabled_tools": ["sais"]},
    )
    assert saved.json()["needs_restart"] is False

    started = await client.post("/api/v1/agents/start", headers=headers)
    assert started.json()["status"] == "running"

    # Starting execution does not rebuild campus processes; reads use current specs.
    assert {spec.tool_id for spec in await _specs_for(user_id)} == {"sais"}

    after = await client.get("/api/v1/campus/connection", headers=headers)
    assert after.json()["needs_restart"] is False


# --- Academic context sync on connect ---------------------------------------
# Connecting SAIS used to verify the credentials and stop there. The only code
# that filled the student's academic context ran inside a turn, when the model
# called a planning tool, so the profile page stayed empty until the student
# happened to ask a planning question.


@pytest.fixture
def record_context_sync(monkeypatch):
    synced: list = []

    async def _sync(user_id):
        synced.append(user_id)
        return True

    monkeypatch.setattr(campus_routes, "sync_student_context_from_sais", _sync)
    return synced


async def test_a_verified_connection_syncs_the_academic_context(client, accept_credentials, record_context_sync):
    user_id = new_user_id()
    response = await client.put(
        "/api/v1/campus/connection",
        headers=auth_header(user_id),
        json={"metu_username": "e123456", "metu_password": "hunter2", "enabled_tools": ["sais"]},
    )

    assert response.status_code == 200
    assert record_context_sync == [user_id]


async def test_an_unverified_connection_does_not_reach_for_sais(client, monkeypatch, record_context_sync):
    """Unverified credentials would only spawn campus servers that cannot sign in."""

    async def _down(username: str, password: str, timeout: float = 20.0) -> VerificationResult:
        return VerificationResult(ok=False, unreachable=True, detail="Could not reach METU sign-in right now.")

    monkeypatch.setattr(campus_routes, "verify_metu_credentials", _down)

    user_id = new_user_id()
    response = await client.put(
        "/api/v1/campus/connection",
        headers=auth_header(user_id),
        json={"metu_username": "e123456", "metu_password": "hunter2"},
    )

    assert response.status_code == 200
    assert response.json()["verified_at"] is None
    assert record_context_sync == []


async def test_a_failing_context_sync_does_not_lose_the_connection(client, accept_credentials, monkeypatch):
    """SAIS being unreadable is not a reason to reject credentials METU accepted."""

    async def _boom(user_id):
        raise RuntimeError("SAIS is unreachable")

    monkeypatch.setattr(campus_routes, "sync_student_context_from_sais", _boom)

    user_id = new_user_id()
    response = await client.put(
        "/api/v1/campus/connection",
        headers=auth_header(user_id),
        json={"metu_username": "e123456", "metu_password": "hunter2"},
    )

    assert response.status_code == 200
    assert response.json()["connected"] is True

    async with SessionLocal() as db:
        stored = (
            await db.execute(select(CampusCredential).where(CampusCredential.user_id == user_id))
        ).scalar_one_or_none()
    assert stored is not None


async def test_the_context_sync_can_be_switched_off(client, accept_credentials, record_context_sync, monkeypatch):
    monkeypatch.setenv("CAMPUS_CONTEXT_SYNC_ON_CONNECT", "false")
    get_settings.cache_clear()

    user_id = new_user_id()
    response = await client.put(
        "/api/v1/campus/connection",
        headers=auth_header(user_id),
        json={"metu_username": "e123456", "metu_password": "hunter2"},
    )

    assert response.status_code == 200
    assert record_context_sync == []
    get_settings.cache_clear()

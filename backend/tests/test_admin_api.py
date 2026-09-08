from uuid import UUID

from sqlalchemy import select

from app.admin.supabase import SupabaseAdmin
from app.config import get_settings
from app.db.models import AccountDirectory, AccountStatus, AdminMembership, AdminRole
from app.db.session import SessionLocal
from tests.conftest import auth_header, new_user_id


async def test_usage_reads_through_assistant_identity_when_api_cannot_access_ai(monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import create_engine, event, text

    from app.api.v1 import admin
    from app.db.engine import postgres_connect_args, postgres_driver_url

    url = postgres_driver_url(get_settings().database_url)
    assistant = create_engine(url, connect_args=postgres_connect_args(url))

    @event.listens_for(assistant, "connect")
    def use_assistant(connection, _):
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE devrimo_assistant")
        connection.commit()

    monkeypatch.setattr(admin, "get_agno_db", lambda: SimpleNamespace(db_engine=assistant))
    try:
        async with SessionLocal() as db:
            await db.execute(text("SET LOCAL ROLE devrimo_api"))
            assert not await db.scalar(text("SELECT has_schema_privilege(current_user, 'ai', 'USAGE')"))
            usage = await admin._token_usage(SimpleNamespace(organization_id=None), db)
            assert usage["runs"] == 0
            assert usage["total_tokens"] == 0
    finally:
        assistant.dispose()


async def test_admin_overview_reports_zero_usage_before_the_first_agent_run(client, monkeypatch):
    user_id = new_user_id()
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(user_id))
    await client.get("/api/v1/profile", headers=auth_header(user_id))

    response = await client.get("/api/v1/admin/overview", headers=auth_header(user_id))

    assert response.status_code == 200, response.text
    assert response.json()["usage"] == {
        "runs": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "last_24h_tokens": 0,
        "last_7d_tokens": 0,
        "estimated_cost_usd": 0.0,
        "primary_model_tokens": 0,
        "compression_tokens": 0,
        "learning_tokens": 0,
    }


async def test_non_admin_is_denied_and_bootstrap_gets_all_permissions(client, monkeypatch):
    user_id = new_user_id()
    response = await client.get("/api/v1/admin/me", headers=auth_header(user_id))
    assert response.status_code == 403

    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(user_id))
    response = await client.get("/api/v1/admin/me", headers=auth_header(user_id))
    assert response.status_code == 200
    assert response.json()["role"] == "super_admin"
    assert "runtime:write" in response.json()["permissions"]
    assert response.json()["bootstrap"] is True


async def test_runtime_settings_are_persisted_and_audited(client, monkeypatch):
    user_id = new_user_id()
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(user_id))
    body = {
        "model_id": "openai/gpt-test",
        "profile": "scholar",
        "max_tokens": 4096,
        "legacy_history_runs": 8,
        "scholar_history_runs": 4,
        "tool_call_limit": 7,
        "learning_enabled": False,
        "input_token_price": 0.000003,
        "output_token_price": 0.000015,
        "reason": "Validate a safer production default",
    }
    response = await client.put("/api/v1/admin/runtime-settings", headers=auth_header(user_id), json=body)
    assert response.status_code == 200
    assert response.json()["model_id"] == "openai/gpt-test"
    assert response.json()["revision"] == 2

    audit = await client.get("/api/v1/admin/audit", headers=auth_header(user_id))
    assert audit.status_code == 200
    assert audit.json()["items"][0]["action"] == "runtime.update"
    serialized = audit.text.lower()
    assert "api_key" not in serialized
    assert "message" not in serialized


async def test_suspension_blocks_an_already_issued_token(client, monkeypatch):
    operator_id = new_user_id()
    target_id = new_user_id()

    # Authenticated requests populate the local directory before any admin role exists.
    await client.get("/api/v1/profile", headers=auth_header(operator_id))
    await client.get("/api/v1/profile", headers=auth_header(target_id))
    async with SessionLocal() as db:
        operator = await db.get(AccountDirectory, operator_id)
        assert operator is not None
        db.add(
            AdminMembership(
                user_id=operator_id,
                organization_id=operator.organization_id,
                role=AdminRole.operator,
                granted_by=operator_id,
            )
        )
        await db.commit()

    async def fake_update(self, user_id: UUID, **values):
        return {"id": str(user_id), **values}

    monkeypatch.setattr(SupabaseAdmin, "update_user", fake_update)
    response = await client.post(
        f"/api/v1/admin/users/{target_id}/suspend",
        headers=auth_header(operator_id),
        json={"reason": "Support-confirmed account compromise"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "suspended"

    # Re-use exactly the same JWT shape: the local account-state check closes
    # the window that would otherwise remain until token expiry.
    response = await client.get("/api/v1/profile", headers=auth_header(target_id))
    assert response.status_code == 403
    async with SessionLocal() as db:
        status_value = await db.scalar(
            select(AccountDirectory.status).where(AccountDirectory.user_id == target_id)
        )
    assert status_value == AccountStatus.suspended


async def test_campus_admin_cannot_change_runtime_defaults(client):
    user_id = new_user_id()
    await client.get("/api/v1/profile", headers=auth_header(user_id))
    async with SessionLocal() as db:
        account = await db.get(AccountDirectory, user_id)
        assert account is not None
        db.add(
            AdminMembership(
                user_id=user_id,
                organization_id=account.organization_id,
                role=AdminRole.campus_admin,
                granted_by=user_id,
            )
        )
        await db.commit()

    response = await client.get("/api/v1/admin/runtime-settings", headers=auth_header(user_id))
    assert response.status_code == 200
    assert response.json()["editable"] is False

    response = await client.put(
        "/api/v1/admin/runtime-settings",
        headers=auth_header(user_id),
        json={
            "model_id": "forbidden",
            "profile": "scholar",
            "max_tokens": 4096,
            "legacy_history_runs": 2,
            "scholar_history_runs": 2,
            "tool_call_limit": 2,
            "learning_enabled": False,
            "reason": "Should be denied",
        },
    )
    assert response.status_code == 403


async def test_campus_role_is_scoped_even_when_the_caller_omits_an_organization(client, monkeypatch):
    operator_id = new_user_id()
    target_id = new_user_id()
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(operator_id))
    await client.get("/api/v1/profile", headers=auth_header(target_id))

    response = await client.put(
        f"/api/v1/admin/memberships/{target_id}",
        headers=auth_header(operator_id),
        json={
            "user_id": str(target_id),
            "role": "campus_admin",
            "organization_id": None,
            "reason": "Grant a campus-scoped role",
        },
    )
    assert response.status_code == 200

    # A null organization reads as unscoped, so the campus role must be pinned
    # to the target's own campus instead of seeing every organization.
    async with SessionLocal() as db:
        membership = await db.get(AdminMembership, target_id)
        account = await db.get(AccountDirectory, target_id)
        assert membership is not None and account is not None
        assert membership.organization_id == account.organization_id
        assert membership.organization_id is not None


async def test_an_admin_cannot_demote_their_own_membership(client):
    user_id = new_user_id()
    await client.get("/api/v1/profile", headers=auth_header(user_id))
    async with SessionLocal() as db:
        account = await db.get(AccountDirectory, user_id)
        assert account is not None
        db.add(
            AdminMembership(
                user_id=user_id,
                organization_id=account.organization_id,
                role=AdminRole.super_admin,
                granted_by=user_id,
            )
        )
        await db.commit()

    response = await client.put(
        f"/api/v1/admin/memberships/{user_id}",
        headers=auth_header(user_id),
        json={
            "user_id": str(user_id),
            "role": "campus_admin",
            "organization_id": None,
            "reason": "Should be refused",
        },
    )
    assert response.status_code == 409

    async with SessionLocal() as db:
        membership = await db.get(AdminMembership, user_id)
        assert membership is not None and membership.role == AdminRole.super_admin

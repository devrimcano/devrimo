"""Boundary tests: no live models, campus credentials or telemetry."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.agents.platform_tools import build_platform_tools
from app.auth.jwt import AuthenticatedUser
from app.workspace.gateway import create_gateway
from app.workspace.resources import EmailDraft, ResourceRef
from app.workspace.service import WorkspaceService


def test_agent_operations_are_exact_and_identity_is_not_an_argument():
    functions = build_platform_tools(uuid4())
    assert [function.name for function in functions] == [
        "search",
        "read",
        "plan",
        "update",
        "undo",
        "send_email",
        "compute",
    ]
    for function in functions:
        function.process_entrypoint()
        assert "user_id" not in function.parameters["properties"]
    assert functions[5].requires_confirmation is True


def test_resource_reference_rejects_identity_and_arbitrary_operations():
    with pytest.raises(ValidationError):
        ResourceRef(kind="student.transcript", user_id=str(uuid4()))
    with pytest.raises(ValidationError):
        ResourceRef(kind="delete_everything")


def test_a_course_read_without_a_key_is_refused_before_the_tool_runs():
    """The message names the missing field, so the model corrects in one step.

    "A course code is required" arrived only after the call had gone through and
    cost a turn; the schema now says it before anything runs.
    """
    with pytest.raises(ValidationError, match="needs"):
        ResourceRef(kind="catalog.sections")
    assert ResourceRef(kind="catalog.sections", key="5710331").key == "5710331"
    # Identity-carrying and term-carrying kinds are untouched.
    ResourceRef(kind="student.transcript")
    ResourceRef(kind="planning.timetable", term="20261")


def test_search_can_only_name_the_kinds_it_actually_searches():
    """`search catalog.courses` used to be expressible and always failed.

    The model spent a turn discovering it could not, then another recovering.
    The search schema now admits only the kinds WorkspaceService.search answers.
    """
    from app.workspace.resources import SearchRequest

    with pytest.raises(ValidationError):
        SearchRequest(resource={"kind": "catalog.courses"}, query="ceng 331")
    assert SearchRequest(resource={"kind": "campus.knowledge"}, query="yönetmelik").resource.kind == "campus.knowledge"
    assert SearchRequest(resource={"kind": "catalog.department"}, query="bilgisayar").resource.kind == "catalog.department"


@pytest.mark.asyncio
async def test_read_only_resource_cannot_be_written_or_undone(monkeypatch):
    service = WorkspaceService(uuid4())
    monkeypatch.setattr(service, "authorize", AsyncMock())
    ref = ResourceRef(kind="student.transcript")
    with pytest.raises(HTTPException) as error:
        await service.update(ref, {"grades": []}, 0, "mutation-1")
    assert error.value.status_code == 403
    with pytest.raises(HTTPException):
        await service.undo(ref, 0, "undo-1")


@pytest.mark.asyncio
async def test_transport_rejects_missing_bearer():
    _, app = create_gateway()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/", json={})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_stateless_mcp_seven_tools_and_private_identity(monkeypatch):
    user = AuthenticatedUser(uuid4(), None, "test-only")
    monkeypatch.setattr("app.workspace.gateway.verify_access_token", lambda token: user)
    server, app = create_gateway()
    async with server.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post(
                "/",
                headers={"Authorization": "Bearer test-only", "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"search", "read", "plan", "update", "undo", "send_email", "compute"}
    assert all(
        "ctx" not in tool["inputSchema"]["properties"] and "user_id" not in tool["inputSchema"]["properties"]
        for tool in tools
    )

    # These adapters may differ in descriptions and confirmation mechanics,
    # but must expose identical argument validation to the model and MCP users.
    def contract(value, definitions):
        if isinstance(value, list):
            return [contract(item, definitions) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return contract(definitions[value["$ref"].split("/")[-1]], definitions)
        return {
            key: contract(item, definitions)
            for key, item in value.items()
            if key not in {"title", "description", "$defs"}
        }

    agno_tools = {function.name: function for function in build_platform_tools(user.id)}
    for tool in tools:
        function = agno_tools[tool["name"]]
        function.process_entrypoint()
        actual = tool["inputSchema"]
        expected = function.parameters
        for part in ("properties", "required"):
            assert contract(actual[part], actual.get("$defs", {})) == contract(
                expected[part], expected.get("$defs", {})
            )


@pytest.mark.asyncio
async def test_mcp_email_never_sends_without_chat_confirmation(monkeypatch):
    user = AuthenticatedUser(uuid4(), None, "test-only")
    monkeypatch.setattr("app.workspace.gateway.verify_access_token", lambda token: user)
    monkeypatch.setattr(WorkspaceService, "authorize", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr(WorkspaceService, "send_approved_email", send)
    server, app = create_gateway()
    async with server.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post(
                "/",
                headers={"Authorization": "Bearer test-only", "Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "send_email",
                        "arguments": {"draft": {"to": "someone@example.org", "subject": "Draft", "body": "Review me"}},
                    },
                },
            )
    assert response.status_code == 200
    assert json.loads(response.json()["result"]["content"][0]["text"])["status"] == "approval_required"
    send.assert_not_called()


@pytest.mark.asyncio
async def test_memory_revision_retry_isolation_undo_and_privacy_clear(monkeypatch):
    from app.agents.memory import clear_memories, mutate_memories, read_memories

    monkeypatch.setattr("app.agents.memory.legacy_memories", lambda _: [])
    owner, other = uuid4(), uuid4()
    changes = {"memories": [{"id": "format", "content": "Use concise answers"}]}
    first = await mutate_memories(owner, changes, 0, "remember-1")
    assert first["revision"] == 1
    assert await mutate_memories(owner, changes, 0, "remember-1") == first
    assert (await read_memories(other))["memories"] == []
    with pytest.raises(HTTPException) as error:
        await mutate_memories(owner, {"memories": []}, 0, "remember-1")
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        await mutate_memories(owner, {"memories": []}, 0, "remember-2")
    undone = await mutate_memories(owner, None, 1, "undo-1", undo=True)
    assert undone["memories"] == []
    await clear_memories(owner)
    assert (await read_memories(owner))["memories"] == []
    # Deletion erases previous content, so undo cannot resurrect it.
    cleared = await read_memories(owner)
    assert (await mutate_memories(owner, None, cleared["revision"], "undo-cleared", undo=True))["memories"] == []


@pytest.mark.asyncio
async def test_memory_rejects_sensitive_content(monkeypatch):
    from app.agents.memory import mutate_memories

    with pytest.raises(HTTPException) as error:
        await mutate_memories(
            uuid4(), {"memories": [{"id": "secret", "content": "My password is secret"}]}, 0, "private"
        )
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_actual_seven_tool_agent_pauses_before_exact_email(monkeypatch):
    from agno.agent import Agent
    from agno.models.response import ModelResponse

    from app.agents.scripted_model import ScriptedModel
    from app.agents.store import get_agno_db

    user_id = uuid4()
    draft = {"to": "student@example.edu", "subject": "Review", "body": "Exact approved text"}
    sent = AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr(WorkspaceService, "send_approved_email", sent)
    model = ScriptedModel(
        responses=[
            ModelResponse(
                role="assistant",
                tool_calls=[
                    {
                        "id": "approved-message",
                        "type": "function",
                        "function": {"name": "send_email", "arguments": json.dumps({"draft": draft})},
                    }
                ],
            ),
            ModelResponse(role="assistant", content="Sent"),
        ]
    )
    agent = Agent(model=model, db=get_agno_db(), tools=build_platform_tools(user_id), telemetry=False)
    paused = await agent.arun("Please send this exact draft", user_id=str(user_id), session_id="workspace-email")
    assert paused.is_paused
    sent.assert_not_called()
    requirement = paused.active_requirements[0]
    assert requirement.tool_execution.tool_args["draft"] == draft
    requirement.confirm()
    await agent.acontinue_run(paused, requirements=paused.requirements)
    sent.assert_awaited_once()
    assert sent.call_args.args[0].body == draft["body"]


@pytest.mark.asyncio
async def test_integration_lease_checks_consent_and_closes_only_requested_server(monkeypatch):
    from types import SimpleNamespace

    from app.campus.mcp_config import CampusServerSpec
    from app.campus.sessions import integration_session

    enabled = [CampusServerSpec("sais", "/unused", ()), CampusServerSpec("webmail", "/unused", ())]
    monkeypatch.setattr("app.campus.sessions.active_account", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        "app.campus.sessions.service.campus_server_specs", AsyncMock(side_effect=lambda *_: list(enabled))
    )
    connected = [SimpleNamespace(name="campus:sais", functions={})]
    connect = AsyncMock(return_value=connected)
    close = AsyncMock()
    monkeypatch.setattr("app.campus.session_pool.connect_campus_toolkits", connect)
    monkeypatch.setattr("app.campus.session_pool.close_toolkits", close)
    user_id = uuid4()
    with pytest.raises(RuntimeError):
        async with integration_session(user_id, "sais") as leased:
            assert [item.name for item in leased] == ["campus:sais"]
            raise RuntimeError("call failed")
    assert [spec.tool_id for spec in connect.call_args.args[0]] == ["sais"]
    close.assert_not_called()
    # A later lease reuses the same credential-revision session.
    async with integration_session(user_id, "sais"):
        pass
    assert connect.await_count == 1
    enabled.clear()
    with pytest.raises(HTTPException) as error:
        async with integration_session(user_id, "sais"):
            pytest.fail("revoked integration should never start")
    assert error.value.status_code == 403
    assert connect.await_count == 1
    close.assert_awaited_once_with(connected)


@pytest.mark.asyncio
async def test_exact_reply_uses_approved_text_and_thread_headers(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    original = {
        "message_id": "<original@example.org>",
        "sender_email": "teacher@example.org",
        "headers": {"References": "<earlier@example.org>"},
    }
    received = []

    async def read_email(**kwargs):
        assert kwargs["mark_as_read"] is False
        return original

    async def send_email(**kwargs):
        received.append(kwargs)
        return {"success": True}

    toolkit = SimpleNamespace(
        functions={
            "webmail_read_email": SimpleNamespace(
                parameters={"properties": {key: {} for key in ("message_id", "folder", "mark_as_read")}},
                entrypoint=read_email,
            ),
            "webmail_send_email": SimpleNamespace(
                parameters={
                    "properties": {key: {} for key in ("to", "subject", "body_text", "in_reply_to", "references")}
                },
                entrypoint=send_email,
            ),
        }
    )

    @asynccontextmanager
    async def session(*args):
        yield [toolkit]

    monkeypatch.setattr("app.workspace.service.integration_session", session)
    workspace = WorkspaceService(uuid4())
    monkeypatch.setattr(workspace, "authorize", AsyncMock())
    draft = EmailDraft(to="teacher@example.org", subject="Re: Course", body="Only this text", reply_to_message_id="42")
    await workspace.send_approved_email(draft)
    assert received == [
        {
            "to": draft.to,
            "subject": draft.subject,
            "body_text": draft.body,
            "in_reply_to": original["message_id"],
            "references": "<earlier@example.org>",
        }
    ]
    with pytest.raises(HTTPException):
        await workspace.send_approved_email(draft.model_copy(update={"to": "someone-else@example.org"}))
    assert len(received) == 1


@pytest.mark.asyncio
async def test_revoked_catalog_consent_blocks_cached_answer(monkeypatch):
    from app.campus import course_info
    from app.db.session import SessionLocal

    user_id = uuid4()
    monkeypatch.setattr("app.admin.directory.active_account", AsyncMock(return_value=object()))
    monkeypatch.setattr(course_info.campus_service, "get_credential", AsyncMock(return_value=None))
    read_cache = AsyncMock(return_value={"cached": "private curriculum"})
    monkeypatch.setattr(course_info, "read_cached", read_cache)
    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as error:
            await course_info.call_course_info(db, user_id, "get_course_info", {"course": "2360111"})
    assert error.value.status_code == 403
    read_cache.assert_not_called()


def test_pinned_webmail_patch_preserves_draft_and_exposes_thread_headers():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "vendor_patches/webmail/patch_server.py"
    spec = importlib.util.spec_from_file_location("webmail_patch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = """def send_email(
    to: str,
    subject: str,
    body_text: str,
    attachments: Optional[List[str]] = None,
):
    return client.send_email(
        to=to, subject=subject, body_text=body_text,
        attachments=attachments,
    )
"""
    patched = module.patch(source)
    assert "in_reply_to: Optional[str] = None" in patched
    assert "references=references" in patched
    assert "to=to, subject=subject, body_text=body_text" in patched
    with pytest.raises(ValueError):
        module.patch(source.replace("attachments: Optional[List[str]]", "attachments: list[str]"))


@pytest.mark.asyncio
async def test_mail_capability_is_exact_user_scoped_single_use_and_retry_safe():
    from app.db.session import SessionLocal
    from app.workspace.approvals import execute_approved, issue_approval

    user_id = uuid4()
    draft = EmailDraft(to="student@example.org", subject="Exact", body="Approved text")
    async with SessionLocal() as db:
        token = await issue_approval(db, user_id, "approved-run", {"draft": draft.model_dump()})
        await db.commit()
    sent = AsyncMock(return_value={"success": True})
    with pytest.raises(HTTPException):
        await execute_approved(uuid4(), token, draft, sent)
    with pytest.raises(HTTPException):
        await execute_approved(user_id, token, draft.model_copy(update={"body": "Changed text"}), sent)
    sent.assert_not_called()
    assert await execute_approved(user_id, token, draft, sent) == {"success": True}
    assert await execute_approved(user_id, token, draft, sent) == {"success": True}
    sent.assert_awaited_once()
    async with SessionLocal() as db:
        second = await issue_approval(db, user_id, "uncertain-run", {"draft": draft.model_dump()})
        await db.commit()
    uncertain = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        await execute_approved(user_id, second, draft, uncertain)
    with pytest.raises(HTTPException) as error:
        await execute_approved(user_id, second, draft, uncertain)
    assert error.value.status_code == 409
    uncertain.assert_awaited_once()


@pytest.mark.asyncio
async def test_integration_pool_keeps_sdk_lifecycle_in_own_task_and_users_separate(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app.campus import session_pool
    from app.campus.mcp_config import CampusServerSpec

    tasks = {}

    async def connect(specs, **kwargs):
        owner = specs[0].env["owner"]
        tasks[owner] = [asyncio.current_task()]

        async def read():
            tasks[owner].append(asyncio.current_task())
            return owner

        return [
            SimpleNamespace(
                name="campus:sais",
                owner=owner,
                functions={"sais_get_student_info": SimpleNamespace(parameters={"properties": {}}, entrypoint=read)},
            )
        ]

    async def close(toolkits):
        for toolkit in toolkits:
            tasks[toolkit.owner].append(asyncio.current_task())

    monkeypatch.setattr(session_pool, "connect_campus_toolkits", connect)
    monkeypatch.setattr(session_pool, "close_toolkits", close)
    users = [uuid4(), uuid4()]
    for user in users:
        spec = CampusServerSpec("sais", "/unused", (), env={"owner": str(user)})
        entry, proxies = await session_pool.acquire(user, "sais", [spec], 1)
        assert await proxies[0].functions["sais_get_student_info"].entrypoint() == str(user)
        entry.active_leases -= 1
        reused, _ = await session_pool.acquire(user, "sais", [spec], 1)
        assert reused is entry
        reused.active_leases -= 1
    await session_pool.close_all()
    for owner in tasks:
        assert len(tasks[owner]) == 3
        assert len(set(tasks[owner])) == 1
        assert tasks[owner][0] is not asyncio.current_task()
    assert tasks[str(users[0])][0] is not tasks[str(users[1])][0]


@pytest.mark.asyncio
async def test_actual_mcp_sdk_roundtrip_accepts_configured_internal_host(monkeypatch):
    from app.config import get_settings
    from app.workspace.client import WorkspaceClient, trusted_workspace_token

    user = AuthenticatedUser(uuid4(), None, "test-only")
    monkeypatch.setattr("app.workspace.gateway.verify_access_token", lambda token: user)
    monkeypatch.setattr(get_settings(), "workspace_gateway_allowed_hosts", "broker:8000")
    monkeypatch.setattr(WorkspaceService, "authorize", AsyncMock())
    server, app = create_gateway()
    client = WorkspaceClient("http://broker:8000/", transport=httpx.ASGITransport(app=app))
    async with server.session_manager.run():
        with trusted_workspace_token("test-only"):
            assert await client.compute("2 + 3") == {"value": 5.0}


@pytest.mark.asyncio
async def test_session_revision_rotation_and_active_lease_retirement(monkeypatch):
    from types import SimpleNamespace

    from app.campus import session_pool
    from app.campus.mcp_config import CampusServerSpec

    monkeypatch.setattr(
        session_pool,
        "connect_campus_toolkits",
        AsyncMock(return_value=[SimpleNamespace(name="campus:sais", functions={})]),
    )
    close = AsyncMock()
    monkeypatch.setattr(session_pool, "close_toolkits", close)
    user = uuid4()
    specs = [CampusServerSpec("sais", "/unused", ())]
    first, _ = await session_pool.acquire(user, "sais", specs, 1)
    first.active_leases -= 1
    second, _ = await session_pool.acquire(user, "sais", specs, 2)
    assert first.task.done()
    assert second is not first
    await session_pool.retire_user(user)
    assert second.retired
    assert not second.task.done()
    second.active_leases -= 1
    await session_pool.invalidate(user, "sais")
    assert second.task.done()
    assert close.await_count == 2


async def test_suspension_during_mail_connection_prevents_send(monkeypatch):
    from contextlib import asynccontextmanager

    from app.admin.directory import touch_account
    from app.db.models import AccountDirectory, AccountStatus
    from app.db.session import SessionLocal

    user = uuid4()
    async with SessionLocal() as db:
        await touch_account(db, user, "test@example.edu")
        await db.commit()

    @asynccontextmanager
    async def session(*args):
        async with SessionLocal() as db:
            account = await db.get(AccountDirectory, user)
            account.status = AccountStatus.suspended
            await db.commit()
        yield []

    monkeypatch.setattr("app.workspace.service.integration_session", session)
    workspace = WorkspaceService(user)
    invoke = AsyncMock()
    monkeypatch.setattr(workspace, "invoke", invoke)
    with pytest.raises(HTTPException) as rejected:
        await workspace.send_approved_email(EmailDraft(to="teacher@example.org", subject="Course", body="Approved"))
    assert rejected.value.status_code == 403
    invoke.assert_not_awaited()

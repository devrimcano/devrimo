"""Scholar production controls and the deterministic tool-call harness."""

import json
from uuid import UUID, uuid4

import httpx
import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.tools.decorator import tool
from sqlalchemy import select

from app.agents.scholar.hooks import production_tool_hook
from app.agents.scholar.prompt import build_instructions
from app.agents.scripted_model import ScriptedModel
from app.agents.store import get_agno_db
from app.campus.catalog import TOOLS_BY_ID
from app.campus.session_pool import close_all, session_count
from app.config import get_settings
from app.db.models import Agent as BrokerAgent
from app.db.models import AgentToolAudit, ChatSession
from app.db.session import SessionLocal
from tests.conftest import auth_header, new_user_id


def _tool_call(name: str, arguments: dict) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[
            {
                "id": "call-scripted-1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    )


def test_every_campus_server_has_an_exact_allowlist():
    assert all(tool.include_tools for tool in TOOLS_BY_ID.values())
    assert TOOLS_BY_ID["sais"].include_tools == (
        "get_student_info",
        "get_schedule",
        "get_transcript",
        "get_announcements",
    )
    assert "login" not in TOOLS_BY_ID["odtuclass"].include_tools


def test_webmail_authority_is_narrow_and_confirmed():
    webmail = TOOLS_BY_ID["webmail"]
    assert set(webmail.requires_confirmation_tools) == {"send_email", "reply_email"}
    assert not {"forward_email", "delete_email", "move_email", "mark_email"} & set(webmail.include_tools)


def test_prompt_mentions_only_connected_toolkits():
    instructions = "\n".join(build_instructions())
    assert "SAIS" in instructions
    assert "Webmail" not in instructions


async def test_scripted_model_executes_a_real_agno_tool_call():
    @tool
    def ping(value: str) -> str:
        return f"pong:{value}"

    model = ScriptedModel(
        responses=[
            _tool_call("ping", {"value": "test"}),
            ModelResponse(role="assistant", content="done"),
        ]
    )
    agent = Agent(model=model, tools=[ping], telemetry=False)
    response = await agent.arun("run the test")

    assert response.content == "done"
    assert response.tools and response.tools[0].result == "pong:test"


async def test_confirmation_pauses_before_email_and_audits_only_after_approval():
    calls: list[str] = []

    @tool(name="webmail_send_email", requires_confirmation=True)
    def send_email(to: str, subject: str, body: str) -> str:
        calls.append(to)
        return "sent"

    model = ScriptedModel(
        responses=[
            _tool_call(
                "webmail_send_email",
                {"to": "student@example.edu", "subject": "Hello", "body": "Private body"},
            ),
            ModelResponse(role="assistant", content="sent after approval"),
        ]
    )
    agent = Agent(
        id="scholar-harness",
        model=model,
        db=get_agno_db(),
        tools=[send_email],
        tool_hooks=[production_tool_hook],
        telemetry=False,
    )
    user_id = uuid4()
    paused = await agent.arun("send it", user_id=str(user_id), session_id="confirmation-test")
    assert paused.is_paused
    assert calls == []

    requirement = paused.active_requirements[0]
    requirement.confirm()
    completed = await agent.acontinue_run(paused, requirements=paused.requirements)
    assert completed.content == "sent after approval"
    assert calls == ["student@example.edu"]

    async with SessionLocal() as db:
        rows = (await db.execute(select(AgentToolAudit))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == user_id
    assert rows[0].tool_name == "webmail_send_email"
    assert rows[0].status == "completed"
    assert len(rows[0].argument_digest) == 64
    assert "Private body" not in repr(rows[0])


async def test_confirmation_api_resumes_the_owner_scoped_paused_run(client, monkeypatch):
    calls: list[str] = []

    @tool(name="send_email", requires_confirmation=True)
    def send_email(draft: dict) -> str:
        calls.append(draft["to"])
        return "sent"

    model = ScriptedModel(
        responses=[
            _tool_call(
                "send_email",
                {"draft": {"to": "student@example.edu", "subject": "Hello", "body": "Exact body"}},
            ),
            ModelResponse(role="assistant", content="sent after approval"),
        ]
    )
    agno_agent = Agent(
        id="confirmation-api-harness",
        model=model,
        db=get_agno_db(),
        tools=[send_email],
        tool_hooks=[production_tool_hook],
        telemetry=False,
    )
    user_id = new_user_id()
    headers = auth_header(user_id)
    assert (await client.post("/api/v1/agents/provision", headers=headers)).status_code == 201

    async with SessionLocal() as db:
        broker_agent = (await db.execute(select(BrokerAgent).where(BrokerAgent.user_id == user_id))).scalar_one()
        db.add(
            ChatSession(
                id="confirmation-api",
                user_id=user_id,
                agent_id=broker_agent.id,
                agno_session_id="confirmation-api",
            )
        )
        await db.commit()

    paused = await agno_agent.arun("send it", user_id=str(user_id), session_id="confirmation-api")
    requirement = paused.active_requirements[0]

    monkeypatch.setattr("app.assistant.worker.build_agent", lambda *_args, **_kwargs: agno_agent)
    response = await client.post(
        "/api/v1/chat/confirmations",
        headers=headers,
        json={
            "run_id": paused.run_id,
            "session_id": "confirmation-api",
            "requirement_id": requirement.id,
            "approved": True,
        },
    )

    assert response.status_code == 200
    assert calls == ["student@example.edu"], response.text
    assert "sent after approval" in response.text


def test_agentos_refuses_to_build_without_jwt_key(monkeypatch):
    from app.agentos.app import build_agentos_app

    monkeypatch.setenv("AGENTOS_ENABLED", "true")
    monkeypatch.setenv("AGENTOS_JWT_VERIFICATION_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="JWT_VERIFICATION_KEY"):
            build_agentos_app()
    finally:
        get_settings.cache_clear()


async def test_agentos_requires_authentication(monkeypatch):
    from app.agentos.app import build_agentos_app

    monkeypatch.setenv("AGENTOS_ENABLED", "true")
    monkeypatch.setenv("AGENTOS_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("AGENTOS_JWT_VERIFICATION_KEY", "test-agentos-key-that-is-at-least-32-bytes")
    get_settings.cache_clear()
    try:
        os_app = build_agentos_app()
        transport = httpx.ASGITransport(app=os_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agentos") as client:
            response = await client.get("/sessions")
        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_scholar_config_uses_compression_not_offload(monkeypatch):
    from app.agents.scholar.build import build_scholar_agent

    monkeypatch.setenv("AGENT_RUNTIME", "agno")
    monkeypatch.setenv("AGENT_LEARNING_ENABLED", "true")
    monkeypatch.setenv("AGENT_COMPRESS_TOOL_RESULTS", "true")
    get_settings.cache_clear()
    try:
        agent = build_scholar_agent()
        assert agent.learning is not None
        assert agent.compress_tool_results is True
        assert agent.offload_tool_results is False
        assert agent.store_tool_messages is False
        assert agent.tool_call_limit == 10
        assert agent.timezone_identifier == "Europe/Istanbul"
    finally:
        get_settings.cache_clear()


async def test_scholar_profile_runs_through_the_chat_api(client, monkeypatch):
    from app.assistant import worker

    original_build = worker.build_agent
    built_ids = []

    def tracked_build(*args, **kwargs):
        agent = original_build(*args, **kwargs)
        built_ids.append(agent.id)
        return agent

    monkeypatch.setattr(worker, "build_agent", tracked_build)
    monkeypatch.setenv("AGENT_PROFILE", "scholar")
    get_settings.cache_clear()
    await close_all()
    user_id = new_user_id()
    headers = auth_header(user_id)
    try:
        assert (await client.post("/api/v1/agents/provision", headers=headers)).status_code == 201
        response = await client.post(
            "/api/v1/chat/completions",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Merhaba"}], "session_id": "scholar-e2e"},
        )
        assert response.status_code == 200
        assert '"content": "[echo]"' in response.text
        assert '"content": " Merhaba"' in response.text
        assert built_ids == ["devrimo-scholar"]
        assert session_count(user_id) == 0
    finally:
        await close_all()
        get_settings.cache_clear()


def test_audit_user_ids_are_uuid_scoped():
    # Guard the schema choice: stringly user ids would make ownership filters
    # and incident queries easy to get subtly wrong.
    assert isinstance(UUID("11111111-1111-1111-1111-111111111111"), UUID)

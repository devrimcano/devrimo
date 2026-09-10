"""The chat turn, end to end, against a real Agno agent.

``AGENT_RUNTIME=fake`` swaps only the model (see ``app/agents/echo_model.py``).
The Agent, its database, session persistence, and the SSE serialization are all
the production ones — so these tests cover the wiring that actually broke in
the Hermes era: which session a turn is written to, and whether history can be
read back without the agent being resident.
"""

import json
from types import SimpleNamespace

from tests.conftest import auth_header, new_user_id


async def provision(client, headers):
    response = await client.post("/api/v1/agents/provision", headers=headers)
    assert response.status_code == 201
    # No background provisioning any more: an agent is usable immediately.
    assert response.json()["status"] == "running"


async def send(client, headers, text, session_id="thread-1"):
    return await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": text}], "session_id": session_id},
    )


def sse_payloads(body: bytes) -> list[dict]:
    """Every JSON data frame in an SSE response, in order."""
    payloads = []
    for line in body.decode().splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        payloads.append(json.loads(data))
    return payloads


def text_of(body: bytes) -> str:
    return "".join(p["choices"][0]["delta"].get("content", "") for p in sse_payloads(body) if p.get("choices"))


async def test_chat_completions_streams_openai_compatible_sse(client):
    headers = auth_header(new_user_id())
    await provision(client, headers)

    response = await send(client, headers, "hi")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b"[DONE]" in response.content

    payloads = sse_payloads(response.content)
    assert payloads, "expected at least one data frame"
    # The frontend's parseSseDelta reads exactly this shape; anything else
    # renders as an empty message.
    assert all(p["object"] == "chat.completion.chunk" for p in payloads)
    assert all("choices" in p for p in payloads)
    assert "hi" in text_of(response.content)
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


async def test_stream_is_actually_chunked(client):
    # A single frame containing the whole answer would satisfy the parser but
    # defeat the point of streaming.
    headers = auth_header(new_user_id())
    await provision(client, headers)

    response = await send(client, headers, "one two three four")
    content_frames = [p for p in sse_payloads(response.content) if p["choices"][0]["delta"].get("content")]
    assert len(content_frames) > 1


async def test_first_chat_lazily_creates_the_agent(client):
    headers = auth_header(new_user_id())
    response = await send(client, headers, "hi")
    assert response.status_code == 200
    assert "hi" in text_of(response.content)


async def test_chat_with_no_user_message_is_400(client):
    headers = auth_header(new_user_id())
    await provision(client, headers)

    response = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "system", "content": "be brief"}], "session_id": "thread-1"},
    )
    assert response.status_code == 400


async def test_chat_creates_and_lists_session(client):
    headers = auth_header(new_user_id())
    await provision(client, headers)
    await send(client, headers, "hi")

    listing = await client.get("/api/v1/chat/sessions", headers=headers)
    assert listing.status_code == 200
    sessions = listing.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == "thread-1"
    assert sessions[0]["title"] == "hi"


async def test_history_is_readable_without_the_agent_resident(client):
    """The whole point of moving history out of the container.

    Under Hermes this read required booting the user's agent; now it is a
    database query, so evicting the agent first must change nothing.
    """
    from app.campus.session_pool import close_all

    headers = auth_header(new_user_id())
    await provision(client, headers)
    await send(client, headers, "what is my CGPA")

    await close_all()

    detail = await client.get("/api/v1/chat/sessions/thread-1", headers=headers)
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "what is my CGPA"
    assert messages[1]["content"]


async def test_a_reopened_conversation_is_served_without_asking_agno_again(client, monkeypatch):
    """The second open of an unchanged conversation costs one query, not five.

    Opening a chat measured about half a second, and 203ms of it was Agno's own
    read of a one-message conversation - roughly five sequential queries to a
    database in Frankfurt. The answer does not change while the conversation
    does not, so it is read once and remembered.
    """
    from app.api.v1 import sessions as sessions_api
    from app.campus.session_pool import close_all

    headers = auth_header(new_user_id())
    await provision(client, headers)
    await send(client, headers, "what is my CGPA")
    await close_all()

    first = await client.get("/api/v1/chat/sessions/thread-1", headers=headers)
    assert first.status_code == 200
    expected = first.json()["messages"]
    assert [m["role"] for m in expected] == ["user", "assistant"]

    # Agno is made unavailable. A second open must not need it.
    def refuse(*_args, **_kwargs):
        raise AssertionError("the remembered copy was not used")

    monkeypatch.setattr(sessions_api, "_load_history", refuse)

    second = await client.get("/api/v1/chat/sessions/thread-1", headers=headers)
    assert second.status_code == 200
    assert second.json()["messages"] == expected


async def test_a_new_turn_retires_the_remembered_copy(client):
    """A copy is only ever served for a conversation that has not changed."""
    from app.campus.session_pool import close_all

    headers = auth_header(new_user_id())
    await provision(client, headers)
    await send(client, headers, "first question")
    await close_all()

    before = await client.get("/api/v1/chat/sessions/thread-1", headers=headers)
    assert len(before.json()["messages"]) == 2

    await send(client, headers, "second question")
    await close_all()

    after = await client.get("/api/v1/chat/sessions/thread-1", headers=headers)
    contents = [m["content"] for m in after.json()["messages"]]
    assert "second question" in contents, "a stale copy hid the newest turn"
    assert len(after.json()["messages"]) > 2


async def test_a_broken_cache_costs_nothing_but_speed(client, monkeypatch):
    """The optimisation must never be the reason a conversation fails to open."""
    from app.api.v1 import sessions as sessions_api
    from app.campus.session_pool import close_all

    headers = auth_header(new_user_id())
    await provision(client, headers)
    await send(client, headers, "what is my CGPA")
    await close_all()

    async def broken(*_args, **_kwargs):
        raise RuntimeError("cache table is unavailable")

    monkeypatch.setattr(sessions_api, "_cached_transcript", broken)
    monkeypatch.setattr(sessions_api, "_remember_transcript", broken)

    detail = await client.get("/api/v1/chat/sessions/thread-1", headers=headers)
    assert detail.status_code == 200
    assert [m["role"] for m in detail.json()["messages"]] == ["user", "assistant"]


async def test_a_cache_read_that_fails_rolls_back_before_falling_back():
    """The failure that actually happened, which the test above could not see.

    Replacing the whole helper never runs its `except`, so it proved nothing
    about the state that branch leaves behind. On production the table existed
    but the API role had never been granted it: the SELECT failed, the handler
    caught it and fell back exactly as designed - and every later query on the
    same session then raised InFailedSQLTransaction, so opening any
    conversation returned 500.

    A caught statement error still poisons the transaction. The rollback is
    what makes catching it mean anything, so the rollback is what is pinned.
    """
    from app.api.v1.sessions import _cached_transcript

    class FailingSession:
        """A session whose cache lookup fails, exactly as the ungranted table did."""

        def __init__(self):
            self.rolled_back = False

        async def get(self, *_args, **_kwargs):
            raise RuntimeError("permission denied for table chat_transcript_cache")

        async def rollback(self):
            self.rolled_back = True

    db = FailingSession()
    session = SimpleNamespace(id="thread-1", updated_at=None)

    assert await _cached_transcript(db, session) is None, "a cache it cannot read must not be served"
    assert db.rolled_back, "a failed cache read left the request's transaction unusable"


async def test_history_never_includes_the_system_prompt(client):
    # Agno's stored history contains the persona; returning it would hand the
    # system prompt to anyone with devtools open.
    headers = auth_header(new_user_id())
    await provision(client, headers)
    await send(client, headers, "hi")

    detail = await client.get("/api/v1/chat/sessions/thread-1", headers=headers)
    roles = {m["role"] for m in detail.json()["messages"]}
    assert roles <= {"user", "assistant"}
    assert "Devrimo Campus Agent" not in detail.text


async def test_second_turn_continues_the_same_session(client):
    headers = auth_header(new_user_id())
    await provision(client, headers)
    await send(client, headers, "first question")
    await send(client, headers, "second question")

    detail = await client.get("/api/v1/chat/sessions/thread-1", headers=headers)
    contents = [m["content"] for m in detail.json()["messages"]]
    assert "first question" in contents
    assert "second question" in contents


async def test_delete_session_soft_deletes(client):
    headers = auth_header(new_user_id())
    await provision(client, headers)
    await send(client, headers, "hi")

    deleted = await client.delete("/api/v1/chat/sessions/thread-1", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}

    listing = await client.get("/api/v1/chat/sessions", headers=headers)
    assert listing.json()["sessions"] == []

    missing = await client.get("/api/v1/chat/sessions/thread-1", headers=headers)
    assert missing.status_code == 404


async def test_sessions_are_isolated_per_user(client):
    headers_a = auth_header(new_user_id())
    headers_b = auth_header(new_user_id())
    await provision(client, headers_a)
    await provision(client, headers_b)
    await send(client, headers_a, "hi")

    response = await client.get("/api/v1/chat/sessions/thread-1", headers=headers_b)
    assert response.status_code == 404


async def test_durable_run_retries_replay_without_another_model_turn(client):
    headers = auth_header(new_user_id())
    body = {
        "messages": [{"role": "user", "content": "saved reply"}],
        "session_id": "durable",
        "idempotency_key": "message-1",
    }
    first = await client.post("/api/v1/chat/completions", headers=headers, json=body)
    replay = await client.post("/api/v1/chat/completions", headers=headers, json=body)
    assert first.status_code == replay.status_code == 200
    assert first.headers["x-run-id"] == replay.headers["x-run-id"]
    assert sse_payloads(first.content) == sse_payloads(replay.content)
    detail = await client.get("/api/v1/chat/sessions/durable", headers=headers)
    assert len(detail.json()["messages"]) == 2
    run_id = first.headers["x-run-id"]
    resumed = await client.get(f"/api/v1/chat/runs/{run_id}/events?after=1", headers=headers)
    assert resumed.content.startswith(b"id: 2\n")
    foreign = await client.get(f"/api/v1/chat/runs/{run_id}/events", headers=auth_header(new_user_id()))
    assert foreign.status_code == 404
    conflict = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={**body, "messages": [{"role": "user", "content": "different"}]},
    )
    assert conflict.status_code == 409


async def test_message_idempotency_preserves_server_generated_session(client):
    headers = auth_header(new_user_id())
    body = {"messages": [{"role": "user", "content": "hello"}], "idempotency_key": "generated-session"}
    first = await client.post("/api/v1/chat/completions", headers=headers, json=body)
    second = await client.post("/api/v1/chat/completions", headers=headers, json=body)
    assert first.status_code == second.status_code == 200
    assert first.headers["x-run-id"] == second.headers["x-run-id"]
    assert len((await client.get("/api/v1/chat/sessions", headers=headers)).json()["sessions"]) == 1

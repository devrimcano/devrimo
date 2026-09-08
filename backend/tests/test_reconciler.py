"""Idle eviction.

The container-era reconciler also healed crashed containers and failed out
stuck provisioning; neither exists any more. An agent is built synchronously on
the turn that needs it, so there is no half-built state to reconcile — what is
left is making sure agents that stopped being used stop holding subprocesses.
"""

from datetime import UTC, datetime, timedelta

from app.agents.pool import get_pool
from app.agents.reconciler import reconcile_once
from tests.conftest import auth_header, new_user_id


async def _provision_and_chat(client, headers):
    await client.post("/api/v1/agents/provision", headers=headers)
    await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}], "session_id": "thread-1"},
    )


async def test_reconciler_evicts_idle_agents(client):
    headers = auth_header(new_user_id())
    await _provision_and_chat(client, headers)

    pool = get_pool()
    assert pool.size() == 1
    user_id = next(iter(pool._entries))
    pool._entries[user_id].last_used_at = datetime.now(UTC) - timedelta(hours=1)

    await reconcile_once()

    assert pool.size() == 0


async def test_eviction_leaves_the_agent_usable(client):
    """Eviction must be invisible: the entitlement and the history both survive."""
    headers = auth_header(new_user_id())
    await _provision_and_chat(client, headers)

    pool = get_pool()
    user_id = next(iter(pool._entries))
    pool._entries[user_id].last_used_at = datetime.now(UTC) - timedelta(hours=1)
    await reconcile_once()

    # Still `running` — status describes the entitlement, not residency.
    status = await client.get("/api/v1/agents/me", headers=headers)
    assert status.json()["status"] == "running"

    detail = await client.get("/api/v1/chat/sessions/thread-1", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["messages"]

    # And the next turn transparently rebuilds it.
    response = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "again"}], "session_id": "thread-1"},
    )
    assert response.status_code == 200
    assert get_pool().size() == 1


async def test_active_agents_are_left_alone(client):
    headers = auth_header(new_user_id())
    await _provision_and_chat(client, headers)

    await reconcile_once()

    assert get_pool().size() == 1

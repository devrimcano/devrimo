from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.agents import manager
from app.db.models import Agent
from app.db.session import SessionLocal
from tests.conftest import auth_header, new_user_id


async def _provisioned_agent(client, headers) -> Agent:
    await client.post("/api/v1/agents/provision", headers=headers)
    async with SessionLocal() as db:
        result = await db.execute(select(Agent))
        return result.scalar_one()


async def test_second_lock_attempt_is_rejected_while_held(client):
    headers = auth_header(new_user_id())
    agent = await _provisioned_agent(client, headers)

    async with SessionLocal() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        acquired_first = await manager.acquire_turn_lock(db, agent, "owner-a")
    assert acquired_first is True

    async with SessionLocal() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        acquired_second = await manager.acquire_turn_lock(db, agent, "owner-b")
    assert acquired_second is False


async def test_lock_is_reacquirable_after_release(client):
    headers = auth_header(new_user_id())
    agent = await _provisioned_agent(client, headers)

    async with SessionLocal() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        await manager.acquire_turn_lock(db, agent, "owner-a")

    async with SessionLocal() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        await manager.release_turn_lock(db, agent, "owner-a")

    async with SessionLocal() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        acquired = await manager.acquire_turn_lock(db, agent, "owner-b")
    assert acquired is True


async def test_expired_lease_can_be_stolen(client):
    headers = auth_header(new_user_id())
    agent = await _provisioned_agent(client, headers)

    async with SessionLocal() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        agent.turn_lock_owner = "owner-a"
        agent.turn_lock_until = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    async with SessionLocal() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        acquired = await manager.acquire_turn_lock(db, agent, "owner-b")
    assert acquired is True


async def test_owner_can_renew_turn_lease(client):
    headers = auth_header(new_user_id())
    agent = await _provisioned_agent(client, headers)

    async with SessionLocal() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        assert await manager.acquire_turn_lock(db, agent, "owner-a")
        await db.refresh(agent)
        before = agent.turn_lock_until

    async with SessionLocal() as db:
        assert await manager.renew_turn_lock(db, agent.id, "owner-a")
        renewed = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        assert renewed.turn_lock_until >= before


async def test_wrong_owner_cannot_renew_turn_lease(client):
    headers = auth_header(new_user_id())
    agent = await _provisioned_agent(client, headers)

    async with SessionLocal() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        assert await manager.acquire_turn_lock(db, agent, "owner-a")

    async with SessionLocal() as db:
        assert not await manager.renew_turn_lock(db, agent.id, "owner-b")

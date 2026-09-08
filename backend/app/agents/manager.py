"""Student assistant entitlement and lifecycle commands.

Inference runs in the durable assistant queue. This module changes the user's
entitlement and cancels active jobs; integration sessions have their own owner.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.campus import service as campus_service
from app.campus.session_pool import retire_user
from app.db.models import Agent, AgentStatus
from app.logging import get_logger

logger = get_logger(__name__)


async def get_agent(db: AsyncSession, user_id: UUID) -> Agent | None:
    result = await db.execute(select(Agent).where(Agent.user_id == user_id))
    return result.scalar_one_or_none()


async def get_agent_or_404(db: AsyncSession, user_id: UUID) -> Agent:
    agent = await get_agent(db, user_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No agent for this user")
    return agent


async def get_or_create_agent(db: AsyncSession, user_id: UUID) -> Agent:
    """Create the lightweight entitlement lazily on the first real use."""
    agent = await get_agent(db, user_id)
    if agent is not None:
        return agent
    try:
        return await provision(db, user_id)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_409_CONFLICT:
            raise
        agent = await get_agent(db, user_id)
        if agent is None:
            raise
        return agent


async def provision(db: AsyncSession, user_id: UUID) -> Agent:
    """Grant this user an agent.

    Instant, unlike the container era: there is nothing to build until the
    student's first turn, so this writes a row and returns ``running`` rather
    than handing off to a background provisioning task the UI has to poll.
    """
    existing = await get_agent(db, user_id)
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Agent already exists")

    agent = Agent(user_id=user_id, status=AgentStatus.running, last_active_at=datetime.now(UTC))
    db.add(agent)
    try:
        await db.commit()
    except Exception as exc:  # unique constraint race between concurrent requests
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Agent already exists") from exc
    await db.refresh(agent)
    logger.info("agent_provisioned", user_id=str(user_id))
    return agent


async def ensure_running(db: AsyncSession, agent: Agent) -> Agent:
    """Enable future queued runs; only the assistant worker builds runtimes."""
    if agent.status == AgentStatus.destroying:
        raise HTTPException(status.HTTP_409_CONFLICT, "Agent is being destroyed")

    agent.status = AgentStatus.running
    agent.error_detail = None
    await db.commit()
    await db.refresh(agent)
    return agent


async def start(db: AsyncSession, agent: Agent) -> Agent:
    return await ensure_running(db, agent)


async def stop(db: AsyncSession, agent: Agent) -> Agent:
    """Cancel active runs and retire integration sessions; keep the entitlement."""
    from app.assistant.models import AssistantRun
    from app.workspace.approvals import revoke_pending_approvals

    await revoke_pending_approvals(db, agent.user_id)

    await db.execute(
        update(AssistantRun)
        .where(AssistantRun.user_id == agent.user_id, AssistantRun.status.in_(("queued", "running")))
        .values(cancel_requested=True)
    )
    await retire_user(agent.user_id)
    agent.status = AgentStatus.stopped
    await db.commit()
    await db.refresh(agent)
    logger.info("agent_stopped", user_id=str(agent.user_id))
    return agent


async def destroy(db: AsyncSession, agent: Agent) -> None:
    from app.assistant.models import AssistantRun
    from app.workspace.approvals import revoke_pending_approvals

    await revoke_pending_approvals(db, agent.user_id)

    await db.execute(
        update(AssistantRun)
        .where(AssistantRun.user_id == agent.user_id, AssistantRun.status.in_(("queued", "running")))
        .values(cancel_requested=True)
    )
    agent.status = AgentStatus.destroying
    await db.commit()

    await retire_user(agent.user_id)
    await db.delete(agent)
    await db.commit()
    logger.info("agent_destroyed", user_id=str(agent.user_id))


async def apply_campus_config(db: AsyncSession, agent: Agent) -> Agent:
    """Push a changed campus connection into the agent.

    Now just a drop: the next turn rebuilds from current credentials. Kept as
    its own operation because the Settings UI calls it explicitly, and because
    dropping eagerly means a student who revoked a tool stops having it
    immediately rather than at the end of their current session.
    """
    if agent.status == AgentStatus.destroying:
        raise HTTPException(status.HTTP_409_CONFLICT, "Agent is being destroyed")

    await retire_user(agent.user_id)
    agent.status = AgentStatus.running
    agent.error_detail = None
    agent.last_active_at = datetime.now(UTC)
    await db.commit()
    await campus_service.mark_config_applied(db, agent.user_id)
    await db.refresh(agent)
    return agent

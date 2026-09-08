"""The agent state machine, as the database sees it.

An ``Agent`` row is a user's *entitlement* to an agent plus its last known
health — not a container any more. Whether that user's agent is currently
resident in this process is :mod:`app.agents.pool`'s business, and deliberately
not reflected in ``status``: a student whose agent was evicted for idleness is
still ``running``, because their next turn brings it back transparently and
nothing was lost.

``status`` therefore means:

``running``       usable; chat turns will be served
``stopped``       the student stopped it from Settings; a turn restarts it
``error``         the last build failed; ``error_detail`` says why
``provisioning``  kept for wire compatibility, now transient
``destroying``    being torn down
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from agno.agent import Agent as AgnoAgent
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pool import ResidentAgent, ResidentLease, get_pool
from app.agents.runtime import get_runtime_config
from app.campus import service as campus_service
from app.config import get_settings
from app.db.models import Agent, AgentStatus
from app.db.session import SessionLocal
from app.logging import get_logger
from app.observability import capture, capture_exception

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


async def resident_for(db: AsyncSession, agent: Agent) -> ResidentAgent:
    """Bring this user's agent up (if needed) and return it, ready for a turn.

    The campus specs are read fresh on every call so a credential change is
    picked up the moment it lands — the pool compares the requested toolset
    against what the resident agent was built with and rebuilds on a mismatch.
    """
    specs = await campus_service.campus_server_specs(db, agent.user_id)
    credential_revision = await campus_service.credential_revision(db, agent.user_id)
    runtime = await get_runtime_config(db)
    try:
        resident = await get_pool().acquire(
            agent.user_id,
            specs,
            runtime,
            credential_revision=credential_revision,
        )
    except Exception as exc:
        agent.status = AgentStatus.error
        agent.error_detail = str(exc)
        await db.commit()
        logger.error("agent_build_failed", user_id=str(agent.user_id), error=str(exc))
        capture_exception(
            exc,
            distinct_id=str(agent.user_id),
            **{"$exception_fingerprint": ["agent_build_failed"]},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not start agent: {exc}") from exc

    if agent.status != AgentStatus.running or agent.error_detail:
        agent.status = AgentStatus.running
        agent.error_detail = None
        await db.commit()
    await campus_service.mark_config_applied(db, agent.user_id)
    return resident


async def lease_for(db: AsyncSession, agent: Agent) -> ResidentLease:
    """Bring up this user's runtime and hold it for the complete streamed turn."""
    specs = await campus_service.campus_server_specs(db, agent.user_id)
    credential_revision = await campus_service.credential_revision(db, agent.user_id)
    runtime = await get_runtime_config(db)
    try:
        lease = await get_pool().lease(
            agent.user_id,
            specs,
            runtime,
            credential_revision=credential_revision,
        )
    except Exception as exc:
        agent.status = AgentStatus.error
        agent.error_detail = str(exc)
        await db.commit()
        logger.error("agent_build_failed", user_id=str(agent.user_id), error=str(exc))
        capture_exception(
            exc,
            distinct_id=str(agent.user_id),
            **{"$exception_fingerprint": ["agent_build_failed"]},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not start agent: {exc}") from exc

    if agent.status != AgentStatus.running or agent.error_detail:
        agent.status = AgentStatus.running
        agent.error_detail = None
        await db.commit()
    await campus_service.mark_config_applied(db, agent.user_id)
    return lease


def agno_agent_for(resident: ResidentAgent) -> AgnoAgent:
    return resident.agent


async def ensure_running(db: AsyncSession, agent: Agent) -> Agent:
    """Called on the hot chat path before a turn is served."""
    if agent.status == AgentStatus.destroying:
        raise HTTPException(status.HTTP_409_CONFLICT, "Agent is being destroyed")

    await resident_for(db, agent)
    await db.refresh(agent)
    return agent


async def start(db: AsyncSession, agent: Agent) -> Agent:
    return await ensure_running(db, agent)


async def stop(db: AsyncSession, agent: Agent) -> Agent:
    """Drop the resident agent and its subprocesses; keep the entitlement."""
    await get_pool().invalidate(agent.user_id)
    agent.status = AgentStatus.stopped
    await db.commit()
    await db.refresh(agent)
    logger.info("agent_stopped", user_id=str(agent.user_id))
    return agent


async def destroy(db: AsyncSession, agent: Agent) -> None:
    agent.status = AgentStatus.destroying
    await db.commit()

    await get_pool().invalidate(agent.user_id)
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

    await get_pool().invalidate(agent.user_id)
    agent.status = AgentStatus.running
    agent.error_detail = None
    agent.last_active_at = datetime.now(UTC)
    await db.commit()
    await campus_service.mark_config_applied(db, agent.user_id)
    await db.refresh(agent)
    return agent


# --- Turn locking ---------------------------------------------------------
# Still in the database rather than the pool's per-user asyncio lock: this has
# to hold across broker replicas, and the pool is per-process.


async def acquire_turn_lock(db: AsyncSession, agent: Agent, owner: str) -> bool:
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=get_settings().turn_lock_lease_seconds)

    result = await db.execute(
        update(Agent)
        .where(
            Agent.id == agent.id,
            (Agent.turn_lock_until.is_(None)) | (Agent.turn_lock_until < now),
        )
        .values(turn_lock_until=lease_until, turn_lock_owner=owner)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount > 0


async def renew_turn_lock(db: AsyncSession, agent_id, owner: str) -> bool:
    """Extend a live turn lease without allowing a former owner to reclaim it."""
    lease_until = datetime.now(UTC) + timedelta(seconds=get_settings().turn_lock_lease_seconds)
    result = await db.execute(
        update(Agent)
        .where(Agent.id == agent_id, Agent.turn_lock_owner == owner)
        .values(turn_lock_until=lease_until)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount > 0


async def release_turn_lock(db: AsyncSession, agent: Agent, owner: str) -> None:
    await db.execute(
        update(Agent)
        .where(Agent.id == agent.id, Agent.turn_lock_owner == owner)
        .values(turn_lock_until=None, turn_lock_owner=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def release_turn_lock_isolated(agent_id, owner: str) -> None:
    """Release a turn lock on a session of its own.

    A handler whose run failed can be holding a request session that is stuck
    mid-transaction, and releasing the lock through it would raise instead —
    stranding the agent as busy for the rest of the lease. Cleanup gets a fresh
    session so it cannot be taken down by whatever went wrong.
    """
    async with SessionLocal() as db:
        await db.execute(
            update(Agent)
            .where(Agent.id == agent_id, Agent.turn_lock_owner == owner)
            .values(turn_lock_until=None, turn_lock_owner=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()


async def turn_lock_heartbeat(agent_id, owner: str, stop_event: asyncio.Event) -> None:
    """Renew a held turn lock until ``stop_event`` is set.

    The lease is deliberately short so a crashed replica cannot strand an
    agent, which means *every* holder has to renew: a run that takes longer
    than ``turn_lock_lease_seconds`` and does not heartbeat simply loses the
    lock while it is still running, and the next request is free to drive the
    same resident agent concurrently.
    """
    settings = get_settings()
    interval = max(1, min(settings.turn_lock_heartbeat_seconds, settings.turn_lock_lease_seconds // 2))
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except TimeoutError:
            async with SessionLocal() as db:
                if not await renew_turn_lock(db, agent_id, owner):
                    # Another replica has taken the lock mid-turn. The turn
                    # keeps running but is no longer protected, so this is the
                    # signal that two replicas raced.
                    logger.error("turn_lock_heartbeat_lost", agent_id=str(agent_id))
                    capture("turn_lock_heartbeat_lost", agent_id=str(agent_id), lock_owner=owner)
                    return


@asynccontextmanager
async def turn_lock_held(agent_id, owner: str) -> AsyncIterator[None]:
    """Heartbeat a turn lock for as long as the body runs.

    For callers that await a single bounded operation. Streaming turns drive
    :func:`turn_lock_heartbeat` directly because their lock outlives the
    handler that took it.
    """
    stop_event = asyncio.Event()
    heartbeat = asyncio.create_task(turn_lock_heartbeat(agent_id, owner, stop_event))
    try:
        yield
    finally:
        stop_event.set()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def touch_last_active(db: AsyncSession, agent: Agent) -> None:
    agent.last_active_at = datetime.now(UTC)
    await db.commit()

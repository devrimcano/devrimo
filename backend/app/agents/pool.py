"""Resident Agno agents, one per user, with their campus MCP subprocesses.

This is what replaced the per-user container. An entry here owns a live
``Agent`` and the MCP subprocesses its toolkits spawned, so the same questions
the container runtime answered — when does a user's agent come up, when does it
go away, what happens when their toolset changes — are answered here instead.

Two things make this cheaper than the containers were. Building an entry costs
one process spawn per connected campus server rather than a container start, so
a cold user is seconds rather than tens of seconds. And nothing is lost by
evicting one: conversation history lives in the database (see
:mod:`app.agents.store`), not on a volume, so eviction is invisible to the
student beyond the next turn being slightly slower.

Concurrency: entries are created under a per-user lock, so two simultaneous
first turns build one agent rather than two sets of subprocesses. The lock is
*not* held across a model turn — that is the database turn lock's job
(:func:`app.agents.manager.acquire_turn_lock`), which also works across
replicas, as this pool does not.
"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from agno.agent import Agent
from agno.tools.mcp import MCPTools

from app.agents.builders import build_agent
from app.agents.runtime import AgentRuntimeConfig, default_runtime_config
from app.agents.toolset import close_toolkits, connect_campus_toolkits
from app.campus.mcp_config import CampusServerSpec
from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ResidentAgent:
    """One user's live agent, and the subprocesses it is holding open."""

    user_id: UUID
    agent: Agent
    toolkits: list[MCPTools] = field(default_factory=list)
    # Which campus tools this agent was actually built with. Compared against
    # the student's current selection to notice a toolset that went stale
    # while the agent was resident.
    tool_ids: tuple[str, ...] = ()
    credential_revision: int = 0
    runtime_revision: int = 0
    active_leases: int = 0
    retired: bool = False
    closed: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.last_used_at = datetime.now(UTC)


@dataclass
class ResidentLease:
    """Keeps credential-bearing MCP subprocesses alive for one streamed turn."""

    pool: "AgentPool"
    resident: ResidentAgent
    released: bool = False

    @property
    def agent(self) -> Agent:
        return self.resident.agent

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        await self.pool.release(self.resident)


class AgentPool:
    def __init__(self) -> None:
        # Ordered by least-recently-used first, so eviction is a popitem.
        self._entries: OrderedDict[UUID, ResidentAgent] = OrderedDict()
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def _lock_for(self, user_id: UUID) -> asyncio.Lock:
        async with self._registry_lock:
            return self._locks.setdefault(user_id, asyncio.Lock())

    async def acquire(
        self,
        user_id: UUID,
        specs: list[CampusServerSpec],
        runtime: AgentRuntimeConfig | None = None,
        *,
        credential_revision: int = 0,
    ) -> ResidentAgent:
        """The user's live agent, building it if it isn't resident or is stale."""
        wanted = tuple(spec.tool_id for spec in specs)
        runtime = runtime or default_runtime_config()
        lock = await self._lock_for(user_id)
        async with lock:
            entry = self._entries.get(user_id)
            if (
                entry is not None
                and entry.tool_ids == wanted
                and entry.credential_revision == credential_revision
                and entry.runtime_revision == runtime.revision
            ):
                entry.touch()
                self._entries.move_to_end(user_id)
                return entry

            if entry is not None:
                # Resident but built with a different toolset: the student
                # changed their campus connection since this agent came up.
                logger.info("agent_toolset_changed", user_id=str(user_id), was=entry.tool_ids, now=wanted)
                await self._discard(user_id)

            entry = await self._build(user_id, specs, wanted, credential_revision, runtime)
            self._entries[user_id] = entry
            self._entries.move_to_end(user_id)

        await self._enforce_capacity()
        return entry

    async def lease(
        self,
        user_id: UUID,
        specs: list[CampusServerSpec],
        runtime: AgentRuntimeConfig | None = None,
        *,
        credential_revision: int = 0,
    ) -> ResidentLease:
        """Acquire a runtime lease that eviction and reconfiguration must respect."""
        runtime = runtime or default_runtime_config()
        while True:
            entry = await self.acquire(user_id, specs, runtime, credential_revision=credential_revision)
            lock = await self._lock_for(user_id)
            async with lock:
                # The entry may have been retired by a concurrent capacity or
                # credential invalidation between acquire() and this lock.
                if self._entries.get(user_id) is not entry or entry.retired:
                    continue
                entry.active_leases += 1
                entry.touch()
                return ResidentLease(pool=self, resident=entry)

    async def release(self, entry: ResidentAgent) -> None:
        lock = await self._lock_for(entry.user_id)
        async with lock:
            if entry.active_leases > 0:
                entry.active_leases -= 1
            if entry.retired and entry.active_leases == 0:
                await self._close_entry(entry)

    async def _build(
        self,
        user_id: UUID,
        specs: list[CampusServerSpec],
        wanted: tuple[str, ...],
        credential_revision: int,
        runtime: AgentRuntimeConfig,
    ) -> ResidentAgent:
        settings = get_settings()
        connected = await connect_campus_toolkits(specs, timeout_seconds=settings.campus_mcp_timeout_seconds)

        agent = build_agent(user_id, connected, runtime)
        logger.info(
            "agent_built",
            user_id=str(user_id),
            requested_tools=wanted,
            connected_tools=tuple(t.name for t in connected),
        )
        return ResidentAgent(
            user_id=user_id,
            agent=agent,
            toolkits=connected,
            tool_ids=wanted,
            credential_revision=credential_revision,
            runtime_revision=runtime.revision,
        )

    async def _discard(self, user_id: UUID) -> None:
        """Drop an entry and close its subprocesses. Caller holds the user's lock."""
        entry = self._entries.pop(user_id, None)
        if entry is None:
            return
        entry.retired = True
        if entry.active_leases == 0:
            await self._close_entry(entry)

    async def _close_entry(self, entry: ResidentAgent) -> None:
        if entry.closed:
            return
        entry.closed = True
        await close_toolkits(entry.toolkits)

    async def _enforce_capacity(self) -> None:
        settings = get_settings()
        while len(self._entries) > settings.agent_pool_max_size:
            user_id, _ = next(iter(self._entries.items()))
            logger.info("agent_evicted_for_capacity", user_id=str(user_id))
            lock = await self._lock_for(user_id)
            async with lock:
                await self._discard(user_id)

    async def invalidate(self, user_id: UUID) -> bool:
        """Drop this user's agent so the next turn rebuilds it.

        Called when a campus connection changes. Returns whether anything was
        actually resident — a student whose agent had already been evicted
        needs no work here, their next turn builds from current credentials.
        """
        lock = await self._lock_for(user_id)
        async with lock:
            if user_id not in self._entries:
                return False
            await self._discard(user_id)
            return True

    async def evict_idle(self, cutoff: datetime) -> list[UUID]:
        stale = [uid for uid, entry in self._entries.items() if entry.last_used_at < cutoff]
        for user_id in stale:
            lock = await self._lock_for(user_id)
            async with lock:
                await self._discard(user_id)
        return stale

    async def close_all(self) -> None:
        for user_id in list(self._entries):
            await self._discard(user_id)

    def is_resident(self, user_id: UUID) -> bool:
        return user_id in self._entries

    def get(self, user_id: UUID) -> ResidentAgent | None:
        return self._entries.get(user_id)

    def size(self) -> int:
        return len(self._entries)


_pool: AgentPool | None = None


def get_pool() -> AgentPool:
    global _pool
    if _pool is None:
        _pool = AgentPool()
    return _pool


async def reset_pool() -> None:
    """Tear the pool down. Used by app shutdown and between tests."""
    global _pool
    if _pool is not None:
        await _pool.close_all()
    _pool = None

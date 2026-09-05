"""Background loop that keeps the agent pool honest.

Much smaller than the container-era reconciler, because most of what it used to
do no longer has a failure mode. There are no containers to notice dying, and
no provisioning to get stuck halfway through: an agent is built synchronously
on the turn that needs it, or that turn fails and says why.

What remains is eviction. A resident agent holds one subprocess per connected
campus server, each with a student's METU credentials in its environment, so
letting them accumulate for users who stopped chatting hours ago costs both
memory and exposure. Evicting is safe precisely because it is invisible:
history is in the database, so the next turn rebuilds and continues.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from app.agents.pool import get_pool
from app.config import get_settings
from app.logging import get_logger
from app.observability import observed_job, report_exception

logger = get_logger(__name__)


async def reconcile_once() -> int:
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.agent_idle_timeout_seconds)

    evicted = await get_pool().evict_idle(cutoff)
    if evicted:
        logger.info("agents_evicted_idle", count=len(evicted), resident=get_pool().size())
    return len(evicted)


async def run_reconciler_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    while not stop_event.is_set():
        # Every pass reports a terminal outcome, so a reconciler that has been
        # failing for an hour is a count rather than a discovery.
        with observed_job("agent_reconcile") as job:
            try:
                evicted = await reconcile_once()
                job.succeeded(evicted=evicted, resident=get_pool().size())
            except Exception as exc:
                # The loop deliberately survives a bad iteration, which is exactly
                # why nothing would otherwise notice it failing every minute.
                logger.error("reconcile_iteration_failed", error=str(exc))
                job.failed(exc)
                report_exception(exc, **{"$exception_fingerprint": ["reconcile_iteration_failed"]})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.reconcile_interval_seconds)
        except TimeoutError:
            pass

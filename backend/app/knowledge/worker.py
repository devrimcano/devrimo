"""The knowledge ingestion worker process.

This runs the same application modules as the broker but as its own systemd
unit, and until now it told PostHog nothing that distinguished it from one.
Three consequences, all fixed here: its logs carried the broker's service
label, its client was never initialised or flushed so a crash lost whatever it
had queued, and a job that failed produced one warning line and no outcome
anywhere — so "the campus corpus stopped updating four days ago" was only ever
discovered by noticing stale answers.
"""

import asyncio
import os
import socket
import uuid

from app.config import get_settings
from app.db.models import CampusIngestionJob
from app.db.session import SessionLocal
from app.knowledge.ingestion import (
    JobLease,
    JobLeaseLost,
    claim_job,
    enqueue_due_sources,
    fail_job,
    process_job,
    renew_lease,
)
from app.logging import configure_logging, get_logger
from app.observability.client import initialize as posthog_initialize
from app.observability.client import shutdown as posthog_shutdown
from app.observability.jobs import observed_job
from app.observability.logs import shutdown as posthog_logs_shutdown
from app.observability.runtime import SERVICE_KNOWLEDGE_WORKER, configure_service

# Before ``configure_logging`` so the OTLP resource is built with this
# process's own service name rather than the broker's.
configure_service(SERVICE_KNOWLEDGE_WORKER)
configure_logging()
logger = get_logger(__name__)


async def _heartbeat(lease: JobLease) -> None:
    interval = get_settings().knowledge_worker_lease_seconds / 3
    while True:
        await asyncio.sleep(interval)
        async with SessionLocal() as db:
            await renew_lease(db, lease)


async def _process(lease: JobLease) -> int:
    async with SessionLocal() as db:
        job = await db.get(CampusIngestionJob, lease.job_id)
        if job is None:
            raise JobLeaseLost("Claimed knowledge job disappeared")
        return await process_job(db, job, lease=lease)


async def run_leased_job(lease: JobLease) -> int:
    processing = asyncio.create_task(_process(lease))
    heartbeat = asyncio.create_task(_heartbeat(lease))
    try:
        done, _ = await asyncio.wait((processing, heartbeat), return_when=asyncio.FIRST_COMPLETED)
        if processing in done:
            return await processing
        # A lost lease or failed renewal cancels processing before it can save.
        await heartbeat
        raise JobLeaseLost("Knowledge job heartbeat stopped")
    finally:
        processing.cancel()
        heartbeat.cancel()
        await asyncio.gather(processing, heartbeat, return_exceptions=True)


async def _run_job(lease: JobLease) -> None:
    """One claimed job, with a terminal outcome whichever way it ends."""
    with observed_job(
        "knowledge_ingestion",
        job_id=str(lease.job_id),
        source_id=str(lease.source_id),
        ingestion_kind=lease.kind,
        attempt=lease.attempt,
    ) as observation:
        try:
            count = await run_leased_job(lease)
            observation.succeeded(records=count)
            logger.info("knowledge_job_completed", job_id=str(lease.job_id), records=count)
        except JobLeaseLost as exc:
            # Another worker owns it now. Expected in a multi-worker deployment,
            # and not something to raise an issue about.
            observation.expected_failure("lease_lost", detail=str(exc))
            logger.info("knowledge_job_lease_lost", job_id=str(lease.job_id))
        except Exception as exc:
            logger.warning("knowledge_job_failed", job_id=str(lease.job_id), error=str(exc))
            observation.failed(exc)
            async with SessionLocal() as db:
                status = await fail_job(db, lease, exc)
            # "Will retry" and "will never run again" are the two facts worth
            # knowing about a failed job, and neither was recorded before.
            observation.detail(job_status=status, retrying=status == "failed", dead=status == "dead")


async def run() -> None:
    settings = get_settings()
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    # Eagerly, so a worker deployed without a key says so at boot instead of
    # being discovered later as an absence of ingestion data.
    posthog_initialize()
    logger.info("knowledge_worker_started", worker_id=worker_id)
    try:
        while True:
            job_id = None
            try:
                async with SessionLocal() as db:
                    await enqueue_due_sources(db)
                    job = await claim_job(db, worker_id)
                    job_id = job.id if job is not None else None
                    lease = JobLease.from_job(job) if job is not None else None
                if lease is not None:
                    await _run_job(lease)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A database restart or a migration window must not crash-loop the
                # worker. Compose readiness handles startup ordering; this keeps a
                # running worker resilient to transient database unavailability.
                logger.warning("knowledge_worker_iteration_failed", error=str(exc))
                from app.observability.client import report_exception

                report_exception(
                    exc,
                    handler="knowledge_worker_loop",
                    **{"$exception_fingerprint": ["knowledge_worker_iteration_failed"]},
                )
            if job_id is None:
                await asyncio.sleep(settings.knowledge_worker_poll_seconds)
    finally:
        # A worker that is being restarted mid-deploy still owes us the events
        # explaining what it was doing when it stopped.
        logger.info("knowledge_worker_stopping", worker_id=worker_id)
        await asyncio.to_thread(posthog_shutdown)
        await asyncio.to_thread(posthog_logs_shutdown)


if __name__ == "__main__":
    asyncio.run(run())

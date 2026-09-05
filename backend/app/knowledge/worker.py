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


async def run() -> None:
    settings = get_settings()
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    logger.info("knowledge_worker_started", worker_id=worker_id)
    while True:
        job_id = None
        try:
            async with SessionLocal() as db:
                await enqueue_due_sources(db)
                job = await claim_job(db, worker_id)
                job_id = job.id if job is not None else None
                lease = JobLease.from_job(job) if job is not None else None
            if lease is not None:
                try:
                    count = await run_leased_job(lease)
                    logger.info("knowledge_job_completed", job_id=str(job_id), records=count)
                except JobLeaseLost:
                    logger.info("knowledge_job_lease_lost", job_id=str(job_id))
                except Exception as exc:
                    logger.warning("knowledge_job_failed", job_id=str(job_id), error=str(exc))
                    async with SessionLocal() as db:
                        await fail_job(db, lease, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A database restart or a migration window must not crash-loop the
            # worker. Compose readiness handles startup ordering; this keeps a
            # running worker resilient to transient database unavailability.
            logger.warning("knowledge_worker_iteration_failed", error=str(exc))
        if job_id is None:
            await asyncio.sleep(settings.knowledge_worker_poll_seconds)


if __name__ == "__main__":
    asyncio.run(run())

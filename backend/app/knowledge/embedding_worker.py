"""Run with DATABASE_RUNTIME_ROLE=embedding and an embedding-only login."""

import asyncio
import os
import signal
import socket
from uuid import uuid4

from app.db.session import engine, get_session_factory, validate_runtime_database
from app.knowledge.embeddings import close_embedding_client
from app.knowledge.indexes import (
    IndexLeaseLost,
    claim_index_job,
    fail_index_job,
    process_index_batch,
    renew_index_lease,
)
from app.logging import configure_logging, get_logger
from app.observability.client import initialize as posthog_initialize
from app.observability.client import shutdown as posthog_shutdown
from app.observability.jobs import capture_worker_lifecycle, observed_job
from app.observability.logs import shutdown as posthog_logs_shutdown
from app.observability.runtime import SERVICE_EMBEDDING_WORKER, configure_service

logger = get_logger(__name__)


async def run_job(
    generation_id,
    owner: str,
    attempt: int,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    sessions = get_session_factory("embedding")

    async def heartbeat():
        while True:
            await asyncio.sleep(45)
            async with sessions() as db:
                await renew_index_lease(db, generation_id, owner, attempt)

    async def process():
        while True:
            async with sessions() as db:
                if await process_index_batch(db, generation_id, owner, attempt):
                    return

    processing = asyncio.create_task(process())
    renewal = asyncio.create_task(heartbeat())
    stopping = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
    try:
        tasks = (processing, renewal) if stopping is None else (processing, renewal, stopping)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if processing in done:
            await processing
        elif renewal in done:
            await renewal
        elif stopping in done:
            # Let the observed job turn this into a cancelled outcome. The
            # finally block below cancels both the provider work and heartbeat.
            raise asyncio.CancelledError()
    finally:
        processing.cancel()
        renewal.cancel()
        if stopping is not None:
            stopping.cancel()
        await asyncio.gather(processing, renewal, return_exceptions=True)
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)


async def _wait_for_stop(stop: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except TimeoutError:
        pass


async def _run_observed_job(
    generation_id,
    owner: str,
    attempt: int,
    sessions,
    stop: asyncio.Event,
) -> None:
    """Run one claimed generation and report its real durable-job outcome."""
    with observed_job(
        "knowledge_embedding",
        job_id=str(generation_id),
        worker=SERVICE_EMBEDDING_WORKER,
        attempt=attempt,
        report_exceptions=False,
        include_failure_reason=False,
    ) as observation:
        try:
            await run_job(generation_id, owner, attempt, stop_event=stop)
        except asyncio.CancelledError:
            # The lease remains running until its expiry and can be reclaimed
            # after restart. It is therefore a retryable cancellation, not a
            # completed or permanently failed generation.
            observation.cancelled("shutdown", job_status="running", retrying=True, dead=False)
            raise
        except IndexLeaseLost:
            # Another owner has fenced this attempt. Do not write a failure row
            # from the stale worker; the next owner reports the real outcome.
            observation.expected_failure("lease_lost", retrying=True, dead=False)
            logger.info("embedding_lease_lost", generation_id=str(generation_id))
        except Exception as exc:
            try:
                async with sessions() as db:
                    await fail_index_job(db, generation_id, owner, attempt, exc)
            except asyncio.CancelledError:
                raise
            except Exception as persist_exc:
                # The original provider failure is still the useful grouping
                # key; only the persistence failure's class is added.
                observation.failed(
                    exc,
                    job_status="unknown",
                    retrying=True,
                    dead=False,
                    failure_persist_error_type=type(persist_exc).__name__,
                )
                logger.warning(
                    "embedding_job_failure_persist_failed",
                    generation_id=str(generation_id),
                    error_type=type(persist_exc).__name__,
                )
            else:
                observation.failed(exc, job_status="failed", retrying=True, dead=False)
                logger.warning(
                    "embedding_job_failed",
                    generation_id=str(generation_id),
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
        else:
            observation.succeeded(job_status="completed", retrying=False, dead=False)


async def run(stop_event: asyncio.Event | None = None) -> None:
    """Run the embedding worker until stopped, flushing all clients on exit."""
    configure_service(SERVICE_EMBEDDING_WORKER)
    configure_logging()
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    if stop_event is None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                # Task cancellation still provides a graceful path on platforms
                # where asyncio cannot install process signal handlers.
                pass

    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
    sessions = None
    started = False
    consecutive_failures = 0
    termination_reason = "cancelled"
    try:
        # Initialization is inside the guarded lifecycle so a startup failure
        # still gets the shutdown flush and local diagnostics.
        posthog_initialize()
        await validate_runtime_database("embedding")
        sessions = get_session_factory("embedding")
        capture_worker_lifecycle(SERVICE_EMBEDDING_WORKER, "started", worker_id=owner)
        started = True
        while not stop.is_set():
            pass_attempt = consecutive_failures + 1
            lease = None
            with observed_job(
                "embedding_worker_pass",
                worker=SERVICE_EMBEDDING_WORKER,
                attempt=pass_attempt,
                retrying=pass_attempt > 1,
                report_exceptions=False,
                include_failure_reason=False,
            ) as observation:
                try:
                    async with sessions() as db:
                        lease = await claim_index_job(db, owner)
                except asyncio.CancelledError:
                    observation.cancelled("shutdown", retrying=pass_attempt > 1)
                    raise
                except Exception as exc:
                    observation.failed(exc, retrying=True)
                    consecutive_failures = pass_attempt
                    logger.warning(
                        "embedding_worker_pass_failed",
                        attempt=pass_attempt,
                        error_type=type(exc).__name__,
                    )
                else:
                    observation.succeeded(claimed=lease is not None, retrying=False)
                    consecutive_failures = 0

            if stop.is_set():
                break
            if lease is not None:
                generation_id, attempt = lease
                try:
                    await _run_observed_job(generation_id, owner, attempt, sessions, stop)
                except asyncio.CancelledError:
                    if stop.is_set():
                        break
                    raise
            else:
                await _wait_for_stop(stop, 5)
    except asyncio.CancelledError:
        termination_reason = "cancelled"
        raise
    except Exception:
        termination_reason = "crashed"
        raise
    finally:
        if started:
            capture_worker_lifecycle(
                SERVICE_EMBEDDING_WORKER,
                "stopped",
                worker_id=owner,
                stop_reason="requested" if stop.is_set() else termination_reason,
            )
        try:
            await close_embedding_client()
        finally:
            try:
                await engine.dispose()
            finally:
                await asyncio.to_thread(posthog_shutdown)
                await asyncio.to_thread(posthog_logs_shutdown)


async def main() -> None:
    await run()


if __name__ == "__main__":
    asyncio.run(main())

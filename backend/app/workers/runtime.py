"""Run one responsibility under one database login.

Usage: python -m app.workers.runtime directory|catalog|retention|researcher
Set DATABASE_RUNTIME_ROLE to directory|catalog|catalog|researcher respectively.
"""

import argparse
import asyncio
import os
import signal

from app.config import get_settings
from app.db.session import engine, get_session_factory, validate_runtime_database
from app.logging import configure_logging, get_logger
from app.observability.client import initialize as posthog_initialize
from app.observability.client import shutdown as posthog_shutdown
from app.observability.jobs import capture_worker_lifecycle, observed_job
from app.observability.logs import shutdown as posthog_logs_shutdown
from app.observability.runtime import SERVICE_DOMAIN_WORKERS, configure_service

logger = get_logger(__name__)


async def _pass(kind):
    if kind == "directory":
        from app.admin.directory import synchronize_directory

        async with get_session_factory("directory")() as db:
            return await synchronize_directory(db)
    elif kind == "retention":
        from app.knowledge.retention import sweep_expired_schedule_cache

        async with get_session_factory("catalog")() as db:
            swept = await sweep_expired_schedule_cache(db)
        if get_settings().academic_catalog_ingestion_enabled or get_settings().academic_catalog_reads_enabled:
            from app.academic_catalog.retention import sweep_failed_observations

            swept += await sweep_failed_observations()
        return swept
    elif kind == "catalog":
        settings = get_settings()
        if settings.academic_catalog_ingestion_enabled:
            from app.academic_catalog.worker import run_once

            return await run_once()
        if settings.academic_catalog_reads_enabled:
            # Published mode must never fall back to the old raw-cache warmer.
            return 0
        from app.campus.warmer import warm_once

        return await warm_once()
    else:
        from app.researchers.worker import run_next

        return await run_next()


async def _wait_for_stop(stop: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except TimeoutError:
        pass


async def _run_pass(kind: str, *, attempt: int, stop_event: asyncio.Event | None = None) -> bool:
    """Run one bounded domain pass and report only aggregate safe facts."""
    with observed_job(
        f"{kind}_worker_pass",
        worker=kind,
        attempt=attempt,
        retrying=attempt > 1,
        include_failure_reason=False,
    ) as observation:
        work = asyncio.create_task(_pass(kind))
        stopping = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
        try:
            tasks = (work,) if stopping is None else (work, stopping)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                result = await work
            else:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                observation.cancelled("shutdown", retrying=attempt > 1)
                raise asyncio.CancelledError()
        except asyncio.CancelledError:
            observation.cancelled("shutdown", retrying=attempt > 1)
            raise
        except Exception as exc:
            # Provider and database errors are intentionally grouped by class;
            # their messages may contain source content or connection details.
            observation.failed(exc, retrying=True)
            logger.warning(
                "domain_worker_pass_failed",
                worker=kind,
                attempt=attempt,
                error_type=type(exc).__name__,
            )
            return False
        finally:
            if stopping is not None:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

        details = {"retrying": False}
        if isinstance(result, int) and not isinstance(result, bool):
            details["result_count"] = result
        # The catalog pass keeps its historical integer return value for
        # callers, but also carries a bounded outcome describing the claimed
        # job. Without inspecting it, a deferred source account or an import
        # failure is reported as a successful worker pass merely because the
        # wrapper did not raise.
        pass_outcome = getattr(result, "outcome", None)
        if pass_outcome is None:
            observation.succeeded(**details)
        else:
            pass_outcome = str(pass_outcome)[:64]
            details["pass_outcome"] = pass_outcome
            job_id = getattr(result, "job_id", None)
            if job_id:
                # The claimed catalog job is the useful join key for this
                # pass. Store it on the observation's reserved field so the
                # terminal event does not receive duplicate ``job_id`` keys.
                observation.job_id = str(job_id)[:128]
            scheduled_count = getattr(result, "scheduled_count", None)
            if isinstance(scheduled_count, int) and not isinstance(scheduled_count, bool):
                details["scheduled_count"] = scheduled_count
            error_code = getattr(result, "error_code", None)
            if error_code:
                details["error_code"] = str(error_code)[:128]
            if pass_outcome in {"deferred", "lease_lost"}:
                observation.expected_failure(str(error_code or pass_outcome)[:128], **details)
            elif pass_outcome == "failed":
                observation.failed_type(str(error_code or "catalog_worker_failed")[:128], **details)
            elif pass_outcome in {"idle", "progress", "completed"}:
                observation.succeeded(**details)
            else:
                observation.failed_type("unknown_catalog_pass_outcome", **details)
        return True


async def run(kind: str, stop_event: asyncio.Event | None = None) -> None:
    """Run one domain worker until its stop event is set."""
    service = SERVICE_DOMAIN_WORKERS[kind]
    configure_service(service)
    configure_logging()
    settings = get_settings()
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    if stop_event is None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                # Windows and a few embedded test loops do not expose signal
                # handlers. The worker remains stoppable by task cancellation.
                pass

    owner = "catalog" if kind == "retention" else kind
    periods = {
        "directory": settings.admin_directory_sync_seconds,
        "retention": settings.schedule_cache_sweep_seconds,
        "catalog": min(settings.catalog_warm_poll_seconds, 5) if settings.academic_catalog_ingestion_enabled else settings.catalog_warm_poll_seconds,
        "researcher": 5,
    }
    worker_id = f"{service}:{os.getpid()}"
    started = False
    consecutive_failures = 0
    termination_reason = "cancelled"
    try:
        # Initialize before validation so a startup failure still flushes the
        # diagnostic explaining why this worker never began polling.
        posthog_initialize()
        await validate_runtime_database(owner)
        if kind == "directory" and not (settings.supabase_url and settings.supabase_secret_key):
            raise RuntimeError("Directory sync requires Supabase Auth admin credentials")

        capture_worker_lifecycle(
            kind,
            "started",
            worker_id=worker_id,
            poll_seconds=periods[kind],
        )
        started = True
        while not stop.is_set():
            attempt = consecutive_failures + 1
            try:
                succeeded = await _run_pass(kind, attempt=attempt, stop_event=stop)
            except asyncio.CancelledError:
                if stop.is_set():
                    break
                raise
            consecutive_failures = 0 if succeeded else attempt
            await _wait_for_stop(stop, periods[kind])
    except asyncio.CancelledError:
        termination_reason = "cancelled"
        raise
    except Exception:
        termination_reason = "crashed"
        raise
    finally:
        if started:
            capture_worker_lifecycle(
                kind,
                "stopped",
                worker_id=worker_id,
                stop_reason="requested" if stop.is_set() else termination_reason,
            )
        try:
            await engine.dispose()
        finally:
            await asyncio.to_thread(posthog_shutdown)
            await asyncio.to_thread(posthog_logs_shutdown)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("directory", "catalog", "retention", "researcher"))
    asyncio.run(run(parser.parse_args().kind))


if __name__ == "__main__":
    main()

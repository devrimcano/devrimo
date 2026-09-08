"""Background work, observed the same way a request is.

A request that fails produces a status code somebody can count. A background
job that fails produces a warning line in a log nobody reads, which is how the
knowledge worker could spend a day failing every ingestion without a single
issue being raised.

``observed_job`` gives a job the two things a request already had: a
correlation context, so every log line and exception inside it carries the job
and source it belongs to, and exactly one terminal outcome event, so "are jobs
succeeding?" is a query.

The distinction that matters is *expected* versus *unexpected* failure. A source
that returned 404, a lease lost to another worker and a disabled embedding
provider are outcomes the system defines and recovers from — they are events. A
``KeyError`` in the parser is a defect — it becomes an issue. Recording both as
"failed" would make the issue list useless; recording neither is what happened
before.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.logging import get_logger
from app.observability.context import (
    EVENT_JOB_COMPLETED,
    OUTCOME_CANCELLED,
    OUTCOME_EXPECTED_FAILURE,
    OUTCOME_SUCCESS,
    OUTCOME_UNEXPECTED_FAILURE,
    telemetry_context,
)

logger = get_logger(__name__)


@dataclass
class JobObservation:
    """Accumulates one job's outcome, then reports it exactly once."""

    kind: str
    job_id: str | None = None
    source_id: str | None = None
    distinct_id: str | None = None
    started: float = field(default_factory=time.monotonic)
    outcome: str = OUTCOME_SUCCESS
    reason: str | None = None
    error_type: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    _exception: BaseException | None = None
    _finished: bool = False

    @property
    def duration_seconds(self) -> float:
        return round(time.monotonic() - self.started, 3)

    def detail(self, **properties: Any) -> None:
        """Attach facts to the terminal event — records written, bytes fetched."""
        self.details.update(properties)

    def succeeded(self, **properties: Any) -> None:
        self.outcome = OUTCOME_SUCCESS
        self.detail(**properties)

    def expected_failure(self, reason: str, **properties: Any) -> None:
        """A failure the system defines and handles: a 404, a lost lease, a retry."""
        self.outcome = OUTCOME_EXPECTED_FAILURE
        self.reason = reason
        self.detail(**properties)

    def cancelled(self, reason: str | None = None, **properties: Any) -> None:
        self.outcome = OUTCOME_CANCELLED
        self.reason = reason
        self.detail(**properties)

    def failed(self, exc: BaseException, **properties: Any) -> None:
        """A failure nothing anticipated. Becomes an issue, with job context."""
        self.outcome = OUTCOME_UNEXPECTED_FAILURE
        self.error_type = exc.__class__.__name__
        self.reason = str(exc) or None
        self._exception = exc
        self.detail(**properties)

    def finish(self) -> None:
        """Emit the terminal event. Idempotent, and never raises."""
        if self._finished:
            return
        self._finished = True
        try:
            from app.observability.client import capture, report_exception

            if self._exception is not None:
                report_exception(
                    self._exception,
                    distinct_id=self.distinct_id,
                    job_kind=self.kind,
                    job_id=self.job_id,
                    source_id=self.source_id,
                    handler="observed_job",
                    **self.details,
                )
            capture(
                EVENT_JOB_COMPLETED,
                distinct_id=self.distinct_id,
                job_kind=self.kind,
                job_id=self.job_id,
                source_id=self.source_id,
                outcome=self.outcome,
                reason=self.reason,
                error_type=self.error_type,
                duration_seconds=self.duration_seconds,
                **self.details,
            )
        except Exception as exc:  # pragma: no cover - observation must not break a job
            from app.observability.diagnostics import report_local

            report_local("job_observation_failed", job_kind=self.kind, error=exc.__class__.__name__)


@contextmanager
def observed_job(
    kind: str,
    *,
    job_id: str | None = None,
    source_id: str | None = None,
    distinct_id: str | None = None,
    request_id: str | None = None,
    **tags: Any,
) -> Iterator[JobObservation]:
    """Run a unit of background work inside its own correlation context.

    An exception that escapes the block is recorded as an unexpected failure and
    re-raised, so a caller that wants to keep its own retry or logging behaviour
    does not have to choose between that and being observed.
    """
    observation = JobObservation(kind=kind, job_id=job_id, source_id=source_id, distinct_id=distinct_id)
    # Extra tags describe the job, so they belong on the terminal event as well
    # as on everything logged inside it — not only as context the SDK happens
    # to merge in.
    observation.detail(**tags)
    context_tags = {
        "job_kind": kind,
        **({"job_id": job_id} if job_id else {}),
        **({"source_id": source_id} if source_id else {}),
        **tags,
    }
    with telemetry_context(
        request_id=request_id,
        distinct_id=distinct_id,
        tags=context_tags,
        log_fields=context_tags,
    ):
        try:
            yield observation
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
            # Shutdown is not a defect. Recording it as one would fill the issue
            # list with every worker restart.
            if observation.outcome == OUTCOME_SUCCESS:
                observation.cancelled(exc.__class__.__name__)
            raise
        except BaseException as exc:
            if observation.outcome == OUTCOME_SUCCESS:
                observation.failed(exc)
            raise
        finally:
            observation.finish()

"""Shared correlation context and the vocabulary of outcomes.

Two things live here because both a request and a background job need them, and
neither should have to know which one it is running inside.

*Correlation.* One id follows a unit of work from the browser through the
Next.js proxy, into a broker request, down every log line it writes, and onto
any exception it raises. Passing that id through call signatures would mean
touching every function between the middleware and the failure; a contextvar
inherits across ``await`` and into Agno's nested model calls for free.

*Outcomes.* "Did it work?" needs the same three answers everywhere, or the
question cannot be asked across surfaces. An **expected failure** is control
flow the product defines — a busy agent, a rejected confirmation, invalid
input; it is an event, not an issue. An **unexpected failure** is a bug or a
dependency that broke, and becomes an issue. Recording both under one property
is what makes an error rate computable rather than guessable.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

import structlog

# Correlation id for the current unit of work. Set by ObservabilityMiddleware
# for requests and by ``job_context`` for background work.
current_request_id: ContextVar[str | None] = ContextVar("devrimo_request_id", default=None)

# The header the browser and every Next.js proxy hop use to carry it.
REQUEST_ID_HEADER = "x-request-id"

# --- outcomes ---------------------------------------------------------------

OUTCOME_SUCCESS = "success"
OUTCOME_EXPECTED_FAILURE = "expected_failure"
OUTCOME_UNEXPECTED_FAILURE = "unexpected_failure"
OUTCOME_CANCELLED = "cancelled"

OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_EXPECTED_FAILURE, OUTCOME_UNEXPECTED_FAILURE, OUTCOME_CANCELLED)

# The one event name every request-shaped unit of work reports under, so a
# single query answers "what is failing" across chat, admin, campus and jobs.
EVENT_REQUEST_COMPLETED = "api_request_completed"
EVENT_JOB_COMPLETED = "background_job_completed"


def new_request_id() -> str:
    return str(uuid4())


def outcome_for_status(status_code: int) -> str:
    """HTTP status to outcome. 4xx is the product saying no; 5xx is a defect."""
    if status_code >= 500:
        return OUTCOME_UNEXPECTED_FAILURE
    if status_code >= 400:
        return OUTCOME_EXPECTED_FAILURE
    return OUTCOME_SUCCESS


def correlation_properties() -> dict[str, Any]:
    """Ids that tie an event to the request, replay session and AI trace around it."""
    from app.observability.llm import current_session_id, current_trace_id

    properties: dict[str, Any] = {}
    request_id = current_request_id.get()
    if request_id:
        properties["request_id"] = request_id
    trace_id = current_trace_id.get()
    if trace_id:
        properties["$ai_trace_id"] = trace_id
    session_id = current_session_id.get()
    if session_id:
        properties["chat_session_id"] = session_id
    return properties


@contextmanager
def telemetry_context(
    *,
    request_id: str | None = None,
    distinct_id: str | None = None,
    session_id: str | None = None,
    tags: dict[str, Any] | None = None,
    log_fields: dict[str, Any] | None = None,
) -> Iterator[str]:
    """Bind one unit of work to PostHog, structlog and the correlation id.

    Exceptions are deliberately *not* captured by the PostHog context here.
    Doing so hands the exception to whichever module-level client the SDK
    happens to have, which on this project is a disabled one — every call site
    reports through :func:`app.observability.client.report_exception` instead,
    which knows about the configured client.
    """
    from app.config import get_settings
    from app.observability.runtime import service_properties

    resolved_request_id = request_id or new_request_id()
    request_token = current_request_id.set(resolved_request_id)
    merged_tags = {**service_properties(), "request_id": resolved_request_id, **(tags or {})}

    structlog.contextvars.bind_contextvars(
        request_id=resolved_request_id,
        **({"user_id": distinct_id} if distinct_id else {}),
        **({"session_id": session_id} if session_id else {}),
        **(log_fields or {}),
    )

    if not get_settings().posthog_configured:
        try:
            yield resolved_request_id
        finally:
            structlog.contextvars.clear_contextvars()
            current_request_id.reset(request_token)
        return

    from posthog import identify_context, new_context, set_context_session, tag

    from app.observability.client import get_posthog

    # `capture_exceptions=False` with an explicit client: the context is for
    # tags and identity only. See the docstring above.
    with new_context(capture_exceptions=False, client=get_posthog()):
        if distinct_id:
            identify_context(distinct_id)
        if session_id:
            set_context_session(session_id)
        for key, value in merged_tags.items():
            tag(key, value)
        try:
            yield resolved_request_id
        finally:
            structlog.contextvars.clear_contextvars()
            current_request_id.reset(request_token)

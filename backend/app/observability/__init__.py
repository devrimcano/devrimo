"""PostHog observability for the broker.

Import from here rather than from the submodules; the split is an
implementation detail.
"""

from app.observability.client import capture, capture_exception, get_posthog, report_exception, shutdown
from app.observability.context import (
    OUTCOME_CANCELLED,
    OUTCOME_EXPECTED_FAILURE,
    OUTCOME_SUCCESS,
    OUTCOME_UNEXPECTED_FAILURE,
    REQUEST_ID_HEADER,
    current_request_id,
    telemetry_context,
)
from app.observability.jobs import observed_job
from app.observability.llm import (
    build_traced_async_client,
    current_session_id,
    current_trace_id,
    llm_turn,
    new_trace_id,
    observed_ai_operation,
    trace_properties,
)
from app.observability.middleware import ObservabilityMiddleware

__all__ = [
    "OUTCOME_CANCELLED",
    "OUTCOME_EXPECTED_FAILURE",
    "OUTCOME_SUCCESS",
    "OUTCOME_UNEXPECTED_FAILURE",
    "REQUEST_ID_HEADER",
    "ObservabilityMiddleware",
    "build_traced_async_client",
    "capture",
    "capture_exception",
    "current_request_id",
    "current_session_id",
    "current_trace_id",
    "get_posthog",
    "llm_turn",
    "new_trace_id",
    "observed_ai_operation",
    "observed_job",
    "report_exception",
    "shutdown",
    "telemetry_context",
    "trace_properties",
]

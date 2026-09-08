"""One chat turn, observed.

A turn is the unit a student experiences: they send a message, the agent thinks,
calls tools, maybe pauses for confirmation, and answers. Inside, Agno may make
many model calls — each captured independently as an ``$ai_generation`` by
``app.observability.llm``. This module records the turn that contains them.

Two events come out of every turn:

``$ai_trace``
    The AI-observability parent. PostHog would synthesise a pseudo-trace from
    the generations alone, but an explicit one carries the turn's own latency
    and, crucially, ``$ai_is_error`` — which is what makes "show me every turn
    that failed" a filter rather than an archaeology exercise.

``chat_turn_completed``
    The product event: tokens, cost, tool counts, and how the turn ended. This
    is the row that answers what a student costs and how often the agent works.

Nothing here may raise. A turn that fails to be *observed* must still be a turn
that succeeded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.observability.client import capture, report_exception
from app.observability.context import (
    OUTCOME_CANCELLED as RESULT_CANCELLED,
)
from app.observability.context import (
    OUTCOME_SUCCESS as RESULT_SUCCESS,
)
from app.observability.context import (
    OUTCOME_UNEXPECTED_FAILURE as RESULT_UNEXPECTED_FAILURE,
)

# Turn outcomes, in the order of severity we care about. Every started turn ends
# on exactly one of these — including the ones that end because the student
# closed the tab, which previously ended on none of them.
OUTCOME_COMPLETED = "completed"
OUTCOME_PAUSED = "paused"
OUTCOME_RUN_ERROR = "run_error"
OUTCOME_STREAM_ERROR = "stream_error"
OUTCOME_CANCELLED = "cancelled"

# How each turn outcome maps onto the vocabulary every other surface reports in,
# so one query spans chat turns, API requests and background jobs.
_RESULTS = {
    OUTCOME_COMPLETED: RESULT_SUCCESS,
    OUTCOME_PAUSED: RESULT_SUCCESS,
    OUTCOME_CANCELLED: RESULT_CANCELLED,
    OUTCOME_RUN_ERROR: RESULT_UNEXPECTED_FAILURE,
    OUTCOME_STREAM_ERROR: RESULT_UNEXPECTED_FAILURE,
}


def _token_totals(metrics: Any) -> dict[str, Any]:
    """Flatten Agno's RunMetrics into event properties.

    ``details`` is keyed by model role, so the Scholar learning and tool-result
    compression passes stay separable from the model that answered the student
    — otherwise a turn's token count silently includes work the student never
    saw.
    """
    if metrics is None:
        return {}
    try:
        raw = metrics.to_dict() if hasattr(metrics, "to_dict") else dict(metrics)
    except Exception:
        return {}

    properties: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cost",
        "time_to_first_token",
        "duration",
    ):
        value = raw.get(key)
        if value:
            properties[key] = value

    details = raw.get("details") or {}
    for model_role, entries in details.items():
        totals = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        for entry in entries or []:
            totals["input_tokens"] += entry.get("input_tokens") or 0
            totals["output_tokens"] += entry.get("output_tokens") or 0
            totals["cost"] += entry.get("cost") or 0.0
        if any(totals.values()):
            properties[f"tokens_{model_role}"] = totals["input_tokens"] + totals["output_tokens"]
            if totals["cost"]:
                properties[f"cost_{model_role}"] = totals["cost"]
    return properties


@dataclass
class TurnObservation:
    """Accumulates one turn's outcome, then reports it exactly once."""

    trace_id: str
    user_id: str
    session_id: str | None = None
    kind: str = "chat_turn"
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    tool_errors: int = 0
    tools_used: list[str] = field(default_factory=list)
    paused: bool = False
    outcome: str = OUTCOME_COMPLETED
    error_message: str | None = None
    error_type: str | None = None
    metrics: Any = None
    # Tools that failed and were recovered from. Kept separately from the turn
    # outcome: the agent retrying a failed campus call and then answering
    # correctly is a successful turn that contains a failure, and flattening
    # the two loses whichever one you flatten.
    recovered_tool_errors: list[str] = field(default_factory=list)
    _finished: bool = False

    @property
    def duration_seconds(self) -> float:
        return round(time.monotonic() - self.started, 3)

    @property
    def failed(self) -> bool:
        return self.outcome in (OUTCOME_RUN_ERROR, OUTCOME_STREAM_ERROR)

    @property
    def result(self) -> str:
        return _RESULTS.get(self.outcome, RESULT_UNEXPECTED_FAILURE)

    def tool_started(self, tool: str | None) -> None:
        self.tool_calls += 1
        if tool and tool not in self.tools_used:
            self.tools_used.append(tool)

    def tool_failed(self, tool: str | None, detail: str | None = None) -> None:
        self.tool_errors += 1
        if tool and tool not in self.tools_used:
            self.tools_used.append(tool)
        if tool and tool not in self.recovered_tool_errors:
            self.recovered_tool_errors.append(tool)
        # A tool error is not necessarily a turn error — the agent may recover
        # — so it is reported on its own rather than by setting `outcome`.
        capture(
            "agent_tool_error",
            distinct_id=self.user_id,
            tool=tool,
            detail=detail,
            turn_kind=self.kind,
            **{"$ai_trace_id": self.trace_id, "$ai_session_id": self.session_id},
        )

    def run_failed(self, detail: str | None, error_type: str | None = None) -> None:
        self.outcome = OUTCOME_RUN_ERROR
        self.error_message = detail
        self.error_type = error_type

    def cancelled(self, reason: str | None = None) -> None:
        """The turn stopped without finishing and without failing.

        A student closing the tab cancels the ASGI task, and the turn used to
        end reporting nothing at all — the outcome constant existed and was
        never reached from the disconnect path. An abandoned turn is a real
        product signal, and it is not an error.
        """
        if self.failed:
            return
        self.outcome = OUTCOME_CANCELLED
        self.error_message = reason

    def stream_failed(self, exc: BaseException) -> None:
        self.outcome = OUTCOME_STREAM_ERROR
        self.error_message = str(exc)
        self.error_type = exc.__class__.__name__
        report_exception(
            exc,
            distinct_id=self.user_id,
            turn_kind=self.kind,
            **{"$ai_trace_id": self.trace_id, "$ai_session_id": self.session_id},
        )

    def finish(self) -> None:
        """Emit the trace and the product event. Idempotent, and never raises."""
        if self._finished:
            return
        self._finished = True
        try:
            if self.paused and self.outcome == OUTCOME_COMPLETED:
                self.outcome = OUTCOME_PAUSED

            shared = {
                "$ai_trace_id": self.trace_id,
                "$ai_session_id": self.session_id,
                "$ai_latency": self.duration_seconds,
                "$ai_span_name": self.kind,
            }
            if self.failed:
                shared["$ai_is_error"] = True
                shared["$ai_error"] = self.error_message or self.error_type or "unknown"

            capture("$ai_trace", distinct_id=self.user_id, **shared)

            capture(
                "chat_turn_completed",
                distinct_id=self.user_id,
                turn_kind=self.kind,
                outcome=self.outcome,
                duration_seconds=self.duration_seconds,
                tool_calls=self.tool_calls,
                tool_errors=self.tool_errors,
                tools_used=self.tools_used,
                recovered_tool_errors=self.recovered_tool_errors,
                paused_for_confirmation=self.paused,
                error_type=self.error_type,
                error_message=self.error_message,
                result=self.result,
                trace_id=self.trace_id,
                chat_session_id=self.session_id,
                **_token_totals(self.metrics),
            )
        except Exception as exc:  # pragma: no cover - observation must not break a turn
            from app.observability.diagnostics import report_local

            report_local("turn_observation_failed", error=exc.__class__.__name__)

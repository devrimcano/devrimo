"""AI observability: one PostHog trace per chat turn, every generation inside it.

Agno owns the model loop, so there is no single place in this codebase where a
turn's LLM calls are visible — one turn can be a first completion, N tool
round-trips, a compression pass and a learning pass, each a separate request to
the provider. Instrumenting the two ``arun``/``acontinue_run`` call sites in the
chat route would therefore miss most of them.

Instead the *client* is instrumented. ``app.agents.models.build_model`` is the
only place a model client is constructed, Agno's ``OpenAIChat``/``OpenAIResponses``
accept an injected ``async_client``, and PostHog's ``AsyncOpenAI`` is a genuine
subclass of ``openai.AsyncOpenAI`` — so swapping it in captures every generation
in the process, including the ones Agno makes on its own initiative.

Two things the stock wrapper cannot know are supplied here from contextvars set
by the chat route:

``$ai_trace_id``
    PostHog mints a fresh UUID per call when none is passed, which would shatter
    one turn into a dozen unrelated single-generation traces. The trace id is
    the turn.

``$ai_session_id``
    The chat session the student sees in their sidebar, so a whole conversation
    can be read as one thing.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)

# PostHog's AI event names. Embeddings are a first-class event type, so an
# embedding batch belongs beside the generations rather than in a bespoke
# product event nothing else knows how to read.
EVENT_AI_EMBEDDING = "$ai_embedding"
EVENT_AI_SPAN = "$ai_span"

# Set per turn by the chat route; read by every nested model call.
current_trace_id: ContextVar[str | None] = ContextVar("posthog_ai_trace_id", default=None)
current_session_id: ContextVar[str | None] = ContextVar("posthog_ai_session_id", default=None)


def new_trace_id() -> str:
    return str(uuid4())


@contextmanager
def llm_turn(trace_id: str, session_id: str | None = None) -> Iterator[str]:
    """Scope every LLM call made inside to one PostHog trace."""
    trace_token = current_trace_id.set(trace_id)
    session_token = current_session_id.set(session_id)
    try:
        yield trace_id
    finally:
        current_trace_id.reset(trace_token)
        current_session_id.reset(session_token)


def trace_properties() -> dict[str, Any]:
    """Properties every AI event in the current turn should carry."""
    properties: dict[str, Any] = {}
    session_id = current_session_id.get()
    if session_id:
        properties["$ai_session_id"] = session_id
    return properties


def _cost_properties(input_token_price: float, output_token_price: float) -> dict[str, Any]:
    """Per-token prices for a model PostHog has no price table for.

    The broker talks to an OpenAI-compatible endpoint serving
    ``muse-spark-1.2-contributor``. Without these, every generation reports a
    cost of zero and per-student cost analysis is silently meaningless.
    Admin-editable (see ``AgentRuntimeConfig``), falling back to the
    ``POSTHOG_INPUT_TOKEN_PRICE``/``POSTHOG_OUTPUT_TOKEN_PRICE`` env defaults.
    """
    properties: dict[str, Any] = {}
    if input_token_price:
        properties["$ai_input_token_price"] = input_token_price
    if output_token_price:
        properties["$ai_output_token_price"] = output_token_price
    return properties


def _defaults(kwargs: dict[str, Any], input_token_price: float, output_token_price: float) -> dict[str, Any]:
    """Fill in the PostHog arguments Agno has no way to pass."""
    settings = get_settings()

    if kwargs.get("posthog_trace_id") is None:
        trace_id = current_trace_id.get()
        if trace_id is not None:
            kwargs["posthog_trace_id"] = trace_id

    merged = {**trace_properties(), **_cost_properties(input_token_price, output_token_price)}
    if merged:
        # An explicit caller value always wins over the ambient one.
        kwargs["posthog_properties"] = {**merged, **(kwargs.get("posthog_properties") or {})}

    # This project deliberately captures complete prompts and completions.
    # The client-level before_send hook removes credentials without discarding
    # the conversational content needed to investigate a bad turn.
    kwargs["posthog_privacy_mode"] = False

    if kwargs.get("posthog_provider_override") is None:
        # Reported as $ai_provider so cost attribution does not claim these
        # were OpenAI calls. The wrapper still parses OpenAI-shaped responses.
        kwargs["posthog_provider_override"] = _provider_name(settings.agent_openai_base_url)

    return kwargs


def _provider_name(base_url: str) -> str:
    if "opencode.ai" in base_url:
        return "opencode"
    if "openrouter.ai" in base_url:
        return "openrouter"
    return "openai"


def build_traced_async_client(
    input_token_price: float = 0.0,
    output_token_price: float = 0.0,
    **client_kwargs: Any,
):
    """A PostHog-instrumented ``AsyncOpenAI`` that inherits the ambient trace.

    ``input_token_price``/``output_token_price`` are resolved once, at client
    build time, from the caller's ``AgentRuntimeConfig`` — the same admin
    revision that gates when the agent pool rebuilds this client, so an
    admin-edited price takes effect on the next turn without a fresh DB read
    per generation.

    Returns ``None`` when PostHog is unconfigured, so ``build_model`` falls back
    to the stock Agno client and nothing about the agent changes.
    """
    from app.observability.client import get_posthog

    posthog_client = get_posthog()
    if posthog_client is None:
        return None

    try:
        from posthog.ai.openai.openai_async import (
            AsyncOpenAI as PostHogAsyncOpenAI,
        )
        from posthog.ai.openai.openai_async import (
            WrappedChat,
            WrappedCompletions,
            WrappedResponses,
        )
    except Exception as exc:  # pragma: no cover - posthog[otel] not installed
        logger.warning("posthog_ai_wrapper_unavailable", error=exc.__class__.__name__)
        return None

    class _TracedCompletions(WrappedCompletions):
        async def create(self, **kwargs: Any):
            return await super().create(**_defaults(kwargs, input_token_price, output_token_price))

        async def parse(self, **kwargs: Any):
            return await super().parse(**_defaults(kwargs, input_token_price, output_token_price))

    class _TracedChat(WrappedChat):
        @property
        def completions(self):
            # `WrappedChat.completions` is a property that builds a fresh
            # wrapper per access, so this override — not an attribute
            # assignment — is what makes the tracing stick.
            return _TracedCompletions(self._client, self._original.completions)

    class _TracedResponses(WrappedResponses):
        async def create(self, **kwargs: Any):
            return await super().create(**_defaults(kwargs, input_token_price, output_token_price))

    class _TracedAsyncOpenAI(PostHogAsyncOpenAI):
        def __init__(self, **kwargs: Any):
            super().__init__(posthog_client=posthog_client, **kwargs)
            # Re-wrap the two resources Agno actually calls with the traced
            # subclasses. `_original_*` is set by PostHog's own wrapping pass.
            self.chat = _TracedChat(self, self._original_chat)
            self.responses = _TracedResponses(self, self._original_responses)

    try:
        return _TracedAsyncOpenAI(**client_kwargs)
    except Exception as exc:
        # A model client that cannot be built must not take the agent down with
        # it; the caller falls back to the uninstrumented client.
        logger.warning("posthog_ai_client_build_failed", error=exc.__class__.__name__)
        return None


@dataclass
class AiOperation:
    """One non-chat model call — an embedding batch, a classification, a rerank.

    ``build_traced_async_client`` covers everything Agno routes through the chat
    model client, which is most of the model traffic but not all of it: the
    embedding provider is reached with a plain ``httpx`` call and was therefore
    invisible. A failing embedding endpoint showed up as a slow ingestion queue
    and nothing else.

    Usage is recorded as *reported by the provider*, and its absence is recorded
    explicitly. "This provider does not return token counts" and "this call used
    no tokens" are different facts, and collapsing them into a missing property
    makes cost analysis quietly wrong.
    """

    operation: str
    provider: str
    model: str
    event: str = EVENT_AI_SPAN
    distinct_id: str | None = None
    started: float = field(default_factory=time.monotonic)
    input_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    http_status: int | None = None
    is_error: bool = False
    error_type: str | None = None
    error_message: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    _finished: bool = False

    @property
    def duration_seconds(self) -> float:
        return round(time.monotonic() - self.started, 4)

    def detail(self, **properties: Any) -> None:
        self.properties.update(properties)

    def usage(self, *, input_tokens: int | None = None, total_tokens: int | None = None) -> None:
        self.input_tokens = input_tokens
        self.total_tokens = total_tokens

    def failed(self, exc: BaseException, *, http_status: int | None = None) -> None:
        self.is_error = True
        self.error_type = exc.__class__.__name__
        self.error_message = str(exc) or None
        if http_status is not None:
            self.http_status = http_status

    def finish(self) -> None:
        """Emit the AI event. Idempotent, and never raises."""
        if self._finished:
            return
        self._finished = True
        try:
            from app.observability.client import USAGE_UNAVAILABLE, capture

            payload: dict[str, Any] = {
                "$ai_provider": self.provider,
                "$ai_model": self.model,
                "$ai_latency": self.duration_seconds,
                "$ai_span_name": self.operation,
                "ai_operation": self.operation,
                **trace_properties(),
                **self.properties,
            }
            trace_id = current_trace_id.get()
            if trace_id:
                payload["$ai_trace_id"] = trace_id
            if self.http_status is not None:
                payload["$ai_http_status"] = self.http_status

            if self.input_tokens is not None:
                payload["$ai_input_tokens"] = self.input_tokens
            if self.total_tokens is not None:
                payload["$ai_total_tokens"] = self.total_tokens
            if self.input_tokens is None and self.total_tokens is None:
                payload["ai_usage"] = USAGE_UNAVAILABLE
            if self.cost_usd is not None:
                payload["$ai_total_cost_usd"] = self.cost_usd
            else:
                payload["ai_cost"] = USAGE_UNAVAILABLE

            if self.is_error:
                payload["$ai_is_error"] = True
                payload["$ai_error"] = self.error_message or self.error_type or "unknown"
                payload["error_type"] = self.error_type

            capture(self.event, distinct_id=self.distinct_id, **payload)
        except Exception as exc:  # pragma: no cover - observation must not break a call
            from app.observability.diagnostics import report_local

            report_local("ai_operation_observation_failed", operation=self.operation, error=exc.__class__.__name__)


@contextmanager
def observed_ai_operation(
    operation: str,
    *,
    provider: str,
    model: str,
    event: str = EVENT_AI_SPAN,
    distinct_id: str | None = None,
    **properties: Any,
) -> Iterator[AiOperation]:
    """Observe a model call this process makes outside Agno's client.

    An exception escaping the block is recorded on the event and re-raised; the
    caller keeps whatever fallback behaviour it already had.
    """
    call = AiOperation(operation=operation, provider=provider, model=model, event=event, distinct_id=distinct_id)
    call.detail(**properties)
    try:
        yield call
    except BaseException as exc:
        call.failed(exc, http_status=getattr(getattr(exc, "response", None), "status_code", None))
        raise
    finally:
        call.finish()

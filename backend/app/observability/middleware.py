"""Per-request observability context and the request-outcome event.

Everything downstream — PostHog events, captured exceptions, structlog lines,
and the AI generations Agno makes inside the request — reads its identity from
here rather than passing a user id down through every call signature. Both
mechanisms are contextvar-based, so an ``await`` chain and Agno's own nested
model calls inherit it for free.

Three things this layer must get right, because nothing above it can.

*Identity comes from the token.* The distinct id is taken from the *verified*
JWT and never from a request header. ``X-POSTHOG-DISTINCT-ID`` is accepted only
to be ignored: honouring it would let any caller attribute events, LLM traces
and exceptions to any other student. ``X-POSTHOG-SESSION-ID`` is different — it
is not an identity claim, just the browser's replay session, and it is what
links a backend LLM trace to the recording of the student who triggered it.

*Exceptions are captured here, not above.* Starlette builds its stack as
``ServerErrorMiddleware -> user middleware -> router``, so the application's
``@app.exception_handler(Exception)`` runs *outside* this middleware, after the
request context has been torn down. Reporting from there produced issues with no
request id, no user and no tags — which is what the audit found. Anything that
escapes the router is therefore reported here, while the context is still
alive, and marked so the outer handler stays a fallback rather than a duplicate.

*Every request reports an outcome.* One event and one structured log line per
application request, carrying the route template rather than the concrete path,
so "which endpoint is failing" is a group-by instead of a regex.
"""

from __future__ import annotations

import asyncio
import time

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.logging import get_logger
from app.observability.context import (
    EVENT_REQUEST_COMPLETED,
    OUTCOME_CANCELLED,
    OUTCOME_UNEXPECTED_FAILURE,
    REQUEST_ID_HEADER,
    new_request_id,
    outcome_for_status,
    telemetry_context,
)

logger = get_logger(__name__)

# Paths that would otherwise emit a request event on every health probe. A
# health check that *fails* is still reported; it is the successful ones that
# are noise.
_QUIET_PATHS = frozenset({"/health", "/api/v1/health"})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == name:
            decoded = value.decode("latin-1").strip()
            return decoded or None
    return None


def _user_from_bearer(scope: Scope):
    """Best-effort identity. Never raises: this is telemetry, not authorization.

    The route's own ``get_current_user`` dependency remains the only thing that
    decides whether a request is allowed to proceed.
    """
    authorization = _header(scope, b"authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        from app.auth.jwt import verify_access_token

        return verify_access_token(authorization[7:].strip())
    except Exception:
        return None


def _route_template(scope: Scope, path: str) -> str:
    """``/api/v1/sessions/{session_id}``, not ``/api/v1/sessions/9f2c...``.

    Grouping by the concrete path gives one row per student and answers
    nothing; grouping by the template is the question anyone actually asks.

    Reconstructed from the path and its parameters rather than read off
    ``scope["route"].path_format``: this FastAPI version mounts an included
    router as a nested application, so the matched route's own
    ``path_format`` is relative to it — ``/me`` rather than
    ``/api/v1/agents/me`` — and the prefix appears nowhere in the scope.
    Parameters are available only after the application has run, which is
    where this is read.
    """
    params = {str(value): name for name, value in (scope.get("path_params") or {}).items()}
    if not params:
        return path

    substituted: set[str] = set()
    segments = []
    for segment in path.split("/"):
        name = params.get(segment)
        if name is None:
            segments.append(segment)
            continue
        substituted.add(segment)
        segments.append("{" + name + "}")
    template = "/".join(segments)

    # A ``:path`` converter captures several segments at once, so it never
    # matches one. There is no such route today; this keeps the template
    # correct if one is added rather than leaking a concrete value.
    for value, name in params.items():
        if value and value not in substituted:
            template = template.replace(value, "{" + name + "}", 1)
    return template


def _operation(scope: Scope) -> str | None:
    """The handler that served the request, for grouping across path variants."""
    endpoint = scope.get("endpoint")
    name = getattr(endpoint, "__name__", None)
    return name if isinstance(name, str) else None


class ObservabilityMiddleware:
    """Bind request identity, report the outcome, and own exception capture."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        request_id = _header(scope, REQUEST_ID_HEADER.encode()) or new_request_id()
        session_id = _header(scope, b"x-posthog-session-id")
        authenticated_user = _user_from_bearer(scope)
        user_id = str(authenticated_user.id) if authenticated_user else None
        if authenticated_user is not None:
            # Starlette exposes this same mapping as request.state. The route
            # dependency reuses the verified token instead of doing the same
            # signature/JWKS work a second time.
            scope.setdefault("state", {})["authenticated_user"] = authenticated_user
        path = scope.get("path", "")
        method = scope.get("method", "")

        structlog.contextvars.clear_contextvars()
        with telemetry_context(
            request_id=request_id,
            distinct_id=user_id,
            session_id=session_id,
            tags={
                "path": path,
                "method": method,
                "agent_profile": settings.agent_profile,
                "agent_runtime": settings.agent_runtime,
            },
            log_fields={"path": path, "method": method},
        ):
            await self._observe(scope, receive, send, request_id, path, method, user_id)

    async def _observe(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request_id: str,
        path: str,
        method: str,
        user_id: str | None,
    ) -> None:
        started = time.monotonic()
        status_code: int | None = None
        outcome: str | None = None
        error_type: str | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                structlog.contextvars.bind_contextvars(status_code=status_code)
                # Additive, and the other half of end-to-end correlation: the
                # browser and the Next.js proxy can log the id the broker used
                # even when they generated none themselves.
                headers = list(message.get("headers") or [])
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            # The client went away mid-request. Not a defect, and not something
            # to raise an issue about, but it is an outcome and it was being
            # recorded as nothing at all.
            outcome = OUTCOME_CANCELLED
            raise
        except BaseException as exc:
            outcome = OUTCOME_UNEXPECTED_FAILURE
            error_type = exc.__class__.__name__
            self._report(exc, user_id, request_id, scope, path, method)
            raise
        finally:
            self._finish(
                scope,
                request_id=request_id,
                path=path,
                method=method,
                user_id=user_id,
                status_code=status_code,
                outcome=outcome,
                error_type=error_type,
                duration_seconds=round(time.monotonic() - started, 4),
            )

    def _report(
        self,
        exc: BaseException,
        user_id: str | None,
        request_id: str,
        scope: Scope,
        path: str,
        method: str,
    ) -> None:
        from app.observability.client import report_exception

        report_exception(
            exc,
            distinct_id=user_id,
            request_id=request_id,
            path=path,
            route=_route_template(scope, path),
            operation=_operation(scope),
            method=method,
            handler="observability_middleware",
        )

    def _finish(
        self,
        scope: Scope,
        *,
        request_id: str,
        path: str,
        method: str,
        user_id: str | None,
        status_code: int | None,
        outcome: str | None,
        error_type: str | None,
        duration_seconds: float,
    ) -> None:
        """One event and one log line per request. Never raises."""
        try:
            resolved = outcome or outcome_for_status(status_code or 500)
            if status_code is not None and status_code < 400 and path in _QUIET_PATHS:
                return

            route = _route_template(scope, path)
            fields = {
                "request_id": request_id,
                "route": route,
                "path": path,
                "method": method,
                "operation": _operation(scope),
                "status_code": status_code,
                "outcome": resolved,
                "error_type": error_type,
                "duration_seconds": duration_seconds,
            }

            from app.observability.client import capture

            capture(EVENT_REQUEST_COMPLETED, distinct_id=user_id, **fields)
            logger.info("api_request_completed", **{k: v for k, v in fields.items() if v is not None})
        except Exception as exc:  # pragma: no cover - observation must not break a response
            from app.observability.diagnostics import report_local

            report_local("request_observation_failed", error=exc.__class__.__name__)

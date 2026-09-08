"""Bounded tool results, mutation audit, and per-tool observability.

``production_tool_hook`` is the one place every tool execution passes through,
which makes it the natural seam for observability: it already times the call and
already holds the run context. Each execution becomes an ``$ai_span`` under the
turn's trace, so a slow or failing campus server is visible in the same timeline
as the generations around it.

The audit trail and the spans have deliberately different privacy rules. The
audit rows in ``agent_tool_audit`` keep only a SHA-256 digest of the arguments —
they are a permanent record of external mutations and must not retain contents.
The spans carry the real arguments and results, because their purpose is
debugging what the agent actually did.
"""

import hashlib
import inspect
import json
import time
from typing import Any
from uuid import UUID, uuid4

from app.db.models import AgentToolAudit
from app.db.session import SessionLocal
from app.logging import get_logger
from app.observability import capture, capture_exception
from app.observability.client import _scrub
from app.observability.llm import current_session_id, current_trace_id

logger = get_logger(__name__)
MAX_TOOL_RESULT_CHARS = 16_000
MUTATING_TOOL_NAMES = {"webmail_send_email", "webmail_reply_email"}
# Span payloads are bounded separately from tool results: a 16k result is
# fine for the model but wasteful on every span.
MAX_SPAN_STATE_CHARS = 4_000


def _canonical_digest(arguments: dict[str, Any]) -> str:
    serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


def _bound_result(result: Any) -> Any:
    if isinstance(result, str) and len(result) > MAX_TOOL_RESULT_CHARS:
        return (
            result[:MAX_TOOL_RESULT_CHARS]
            + f"\n\n[Result truncated by Devrimo after {MAX_TOOL_RESULT_CHARS} characters. Narrow the query.]"
        )
    if isinstance(result, (dict, list)):
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) > MAX_TOOL_RESULT_CHARS:
            return {
                "truncated": True,
                "preview": serialized[:MAX_TOOL_RESULT_CHARS],
                "instruction": "Narrow the query before using this result.",
            }
    return result


async def record_confirmation_rejection(
    *, user_id: str, session_id: str, run_id: str, tool_name: str, arguments: dict[str, Any]
) -> None:
    """Record a declined external mutation without retaining its contents."""
    if tool_name not in MUTATING_TOOL_NAMES:
        return
    try:
        async with SessionLocal() as db:
            db.add(
                AgentToolAudit(
                    user_id=UUID(user_id),
                    session_id=session_id,
                    run_id=run_id,
                    tool_name=tool_name,
                    status="rejected",
                    argument_digest=_canonical_digest(arguments),
                    duration_ms=0,
                    error_code=None,
                )
            )
            await db.commit()
    except Exception as audit_error:
        logger.error("agent_tool_audit_failed", tool=tool_name, error=audit_error.__class__.__name__)
        capture_exception(audit_error, tool=tool_name, **{"$exception_fingerprint": ["agent_tool_audit_failed"]})


async def _write_mutation_audit(
    *, run_context, name: str, arguments: dict[str, Any], status: str, started: float, error
):
    if name not in MUTATING_TOOL_NAMES or run_context is None or not run_context.user_id:
        return
    try:
        async with SessionLocal() as db:
            db.add(
                AgentToolAudit(
                    user_id=UUID(run_context.user_id),
                    session_id=run_context.session_id,
                    run_id=run_context.run_id,
                    tool_name=name,
                    status=status,
                    argument_digest=_canonical_digest(arguments),
                    duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                    error_code=error.__class__.__name__[:128] if error else None,
                )
            )
            await db.commit()
    except Exception as audit_error:  # the audit sink must not leak arguments or break a read/write result
        logger.error("agent_tool_audit_failed", tool=name, error=audit_error.__class__.__name__)
        # A silent audit sink is the one failure that would let an external
        # mutation happen with no record of it at all.
        capture_exception(audit_error, tool=name, **{"$exception_fingerprint": ["agent_tool_audit_failed"]})


def _tool_server(tool_name: str) -> str | None:
    """The campus server a prefixed tool belongs to (``webmail_send_email`` -> ``webmail``).

    Tool names are prefixed per server by ``MCPTools(tool_name_prefix=...)``, so
    this is what lets error rates be grouped by server rather than by tool.
    """
    return tool_name.split("_", 1)[0] if "_" in tool_name else None


def _span_state(value: Any) -> Any:
    """Secret-scrub and bound a span payload without hiding that it was bounded."""
    try:
        safe_value = _scrub(value)
        serialized = (
            safe_value if isinstance(safe_value, str) else json.dumps(safe_value, ensure_ascii=False, default=str)
        )
    except Exception:
        return "[unserializable]"
    if len(serialized) > MAX_SPAN_STATE_CHARS:
        return serialized[:MAX_SPAN_STATE_CHARS] + f"...[truncated at {MAX_SPAN_STATE_CHARS} chars]"
    return serialized


def _capture_tool_span(
    *,
    name: str,
    arguments: dict[str, Any],
    result: Any,
    started: float,
    error: BaseException | None,
    run_context,
) -> None:
    """Report one tool execution as an $ai_span under the current turn's trace."""
    trace_id = current_trace_id.get()
    if trace_id is None:
        # Outside a chat turn (an eval run, a background pass) there is no trace
        # to attach to, and an orphaned span is noise.
        return

    user_id = getattr(run_context, "user_id", None) if run_context is not None else None
    properties: dict[str, Any] = {
        "$ai_trace_id": trace_id,
        "$ai_session_id": current_session_id.get(),
        "$ai_parent_id": trace_id,
        "$ai_span_id": str(uuid4()),
        "$ai_span_name": name,
        "$ai_latency": round(time.monotonic() - started, 3),
        "$ai_input_state": _span_state(arguments),
        "tool": name,
        "tool_server": _tool_server(name),
        "requires_confirmation": name in MUTATING_TOOL_NAMES,
        "run_id": getattr(run_context, "run_id", None) if run_context is not None else None,
    }
    if error is not None:
        properties["$ai_is_error"] = True
        properties["$ai_error"] = f"{error.__class__.__name__}: {error}"
    else:
        properties["$ai_output_state"] = _span_state(result)

    capture("$ai_span", distinct_id=user_id, **properties)


async def production_tool_hook(function_name, function, arguments, run_context=None):
    """Execute one tool, cap text output, audit external mutations, and observe it."""
    started = time.monotonic()
    error = None
    result = None
    try:
        result = function(**arguments)
        if inspect.isawaitable(result):
            result = await result
        logger.info("agent_tool_completed", tool=function_name, duration_ms=round((time.monotonic() - started) * 1000))
        result = _bound_result(result)
        return result
    except Exception as exc:
        error = exc
        logger.warning(
            "agent_tool_failed",
            tool=function_name,
            duration_ms=round((time.monotonic() - started) * 1000),
            error=exc.__class__.__name__,
        )
        # The span below carries the failure as a property; this makes it a
        # first-class issue with a stack trace, grouped by server rather than
        # by whichever line inside the MCP client happened to raise.
        capture_exception(
            exc,
            distinct_id=getattr(run_context, "user_id", None) if run_context is not None else None,
            tool=function_name,
            tool_server=_tool_server(function_name),
            **{"$exception_fingerprint": ["agent_tool_failed", _tool_server(function_name) or function_name]},
        )
        raise
    finally:
        _capture_tool_span(
            name=function_name,
            arguments=arguments,
            result=result,
            started=started,
            error=error,
            run_context=run_context,
        )
        await _write_mutation_audit(
            run_context=run_context,
            name=function_name,
            arguments=arguments,
            status="failed" if error else "completed",
            started=started,
            error=error,
        )

"""Single Agno-to-application SSE mapper shared by durable execution."""

import json
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from app.logging import get_logger
from app.observability.turns import TurnObservation

logger = get_logger(__name__)


def _chunk(model: str, *, delta: dict | None = None, extension: dict | None = None, finish: str | None = None) -> bytes:
    payload: dict = {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if extension is not None:
        # Namespaced so it can never be mistaken for an OpenAI field, and so a
        # client that doesn't know about it simply sees an empty delta.
        payload["devrimo"] = extension
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _tool_name(event) -> str | None:
    tool = getattr(event, "tool", None)
    if tool is None:
        return None
    return getattr(tool, "tool_name", None) or getattr(tool, "name", None)


def _tool_server(tool_name: str | None) -> str | None:
    """The campus server a prefixed tool belongs to (``sais_get_transcript`` -> ``sais``)."""
    if not tool_name:
        return None
    for server in ("course_info", "odtuclass", "webmail", "sais"):
        if tool_name.startswith(f"{server}_"):
            return server
    return None


def _tool_error_detail(event) -> str:
    """The message from a ToolCallErrorEvent.

    ``ToolExecution.tool_call_error`` is a flag, not a message — the text lives
    on the event's ``error`` field, falling back to the tool's own result.
    """
    detail = getattr(event, "error", None)
    if not detail:
        tool = getattr(event, "tool", None)
        detail = getattr(tool, "result", None) if tool is not None else None
    if not detail:
        detail = getattr(event, "content", None)
    return str(detail) if detail else "The tool call failed."


def _confirmation_payload(event) -> list[dict]:
    requirements = []
    for requirement in getattr(event, "active_requirements", []) or []:
        if not getattr(requirement, "needs_confirmation", False):
            continue
        execution = getattr(requirement, "tool_execution", None)
        requirements.append(
            {
                "id": requirement.id,
                "tool": getattr(execution, "tool_name", None),
                "arguments": getattr(execution, "tool_args", None) or {},
            }
        )
    return requirements


async def _serialize_events(
    events,
    model: str,
    user_id: str,
    observation: TurnObservation,
) -> AsyncIterator[bytes]:
    """Agno run events -> OpenAI-compatible SSE, observed as one PostHog trace."""
    from agno.run.agent import RunEvent

    try:
        async for event in events:
            name = getattr(event, "event", None)

            if name == RunEvent.run_content.value:
                content = getattr(event, "content", None)
                if isinstance(content, str) and content:
                    yield _chunk(model, delta={"role": "assistant", "content": content})

            elif name == RunEvent.tool_call_started.value:
                tool = _tool_name(event)
                observation.tool_started(tool)
                yield _chunk(model, extension={"type": "tool_call_started", "tool": tool, "server": _tool_server(tool)})

            elif name == RunEvent.tool_call_completed.value:
                tool = _tool_name(event)
                yield _chunk(
                    model, extension={"type": "tool_call_completed", "tool": tool, "server": _tool_server(tool)}
                )

            elif name == RunEvent.tool_call_error.value:
                # Previously unhandled: Agno emitted this and the broker dropped it,
                # so a failed tool reached neither the student nor any log. The
                # agent may still recover on its own, so this is reported without
                # ending the turn.
                tool = _tool_name(event)
                detail = _tool_error_detail(event)
                logger.warning("agent_tool_call_error", user_id=user_id, tool=tool, detail=detail)
                observation.tool_failed(tool, detail)
                yield _chunk(
                    model,
                    extension={
                        "type": "tool_call_error",
                        "tool": tool,
                        "server": _tool_server(tool),
                        "message": detail,
                    },
                )

            elif name == RunEvent.run_paused.value:
                observation.paused = True
                yield _chunk(
                    model,
                    extension={
                        "type": "confirmation_required",
                        "run_id": getattr(event, "run_id", None),
                        "session_id": getattr(event, "session_id", None),
                        "requirements": _confirmation_payload(event),
                    },
                )

            elif name == RunEvent.run_completed.value:
                # Token counts, cost and time-to-first-token for the whole turn,
                # broken down per model role.
                observation.metrics = getattr(event, "metrics", None)

            elif name == RunEvent.run_cancelled.value:
                observation.cancelled("run_cancelled")

            elif name == RunEvent.run_error.value:
                detail = getattr(event, "content", None) or "The agent could not complete this turn."
                error_type = getattr(event, "error_type", None)
                logger.error("agent_run_error", user_id=user_id, detail=str(detail), error_type=error_type)
                observation.run_failed(str(detail), error_type)
                yield _chunk(
                    model,
                    extension={"type": "error", "code": error_type or "run_error", "message": str(detail)},
                    finish="stop",
                )
                return

        yield _chunk(model, delta={}, finish="stop")
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            await close()


async def _serialize_run(
    agno_agent,
    text: str,
    session_id: str,
    user_id: str,
    model: str,
    dependencies: dict,
    observation: TurnObservation,
) -> AsyncIterator[bytes]:
    stream = agno_agent.arun(
        input=text,
        session_id=session_id,
        user_id=user_id,
        dependencies=dependencies,
        stream=True,
        stream_events=True,
    )
    async for chunk in _serialize_events(stream, model, user_id, observation):
        yield chunk

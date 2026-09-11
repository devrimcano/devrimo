"""Single Agno-to-application SSE mapper shared by durable execution."""

import json
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from app.logging import get_logger
from app.observability.turns import TurnObservation

logger = get_logger(__name__)

# How much text is held back before it is committed as the answer rather than
# an announcement of the next tool call.
#
# The instructions already say "Never narrate your thinking, search process, or
# tool-selection process"; the model narrates anyway, one short sentence before
# each tool call. Measured on real turns, those are 30-60 characters -
# "PHYS 213 şubelerini kontrol ediyorum." is 37 - and a real answer runs to
# hundreds. Two hundred is far above every announcement seen and far below
# every answer, and the cost of being wrong either way is small: a very short
# answer arrives in one piece at the end instead of streaming, and an
# improbably long announcement streams as text the way it does today.
_PREAMBLE_CHARACTERS = 200


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

    # Text the model has produced that may turn out to be a preamble. See
    # _PREAMBLE_CHARACTERS: the model announces what it is about to do before
    # each tool call - "PHYS 213 şubelerini kontrol ediyorum." - and those
    # announcements used to be appended to the answer and stay there, four and
    # six deep, run together without spaces. The stored transcript never had
    # them; only the live stream did, which is why reopening a conversation
    # showed a clean answer and watching it arrive did not.
    held: list[str] = []
    # False until this segment has been committed as the answer. Once committed
    # the rest of the segment streams through live, as it always did.
    streaming = False
    # Nothing is held back until this run has actually called a tool. A turn
    # that answers without tools has no announcements to strip - there is
    # nothing for the model to announce - and holding its reply back would only
    # cost it the streaming that test_stream_is_actually_chunked exists to
    # protect. It is also why the first announcement of a turn survives: it is
    # written before any tool call, and at that point this cannot yet tell an
    # announcement from a reply. One lead-in sentence is not the reported
    # defect; four of them stacked in front of the answer is.
    used_a_tool = False

    def _hold(content: str):
        """Buffer a fragment, and hand back whatever is safe to send now."""
        nonlocal streaming
        if streaming or not used_a_tool:
            return content
        held.append(content)
        if sum(len(part) for part in held) <= _PREAMBLE_CHARACTERS:
            return None
        # Longer than any announcement the model makes: this is the answer.
        streaming = True
        return "".join(held)

    def _discard_preamble() -> str | None:
        """Drop the held announcement, returning it for the reasoning panel."""
        nonlocal streaming
        if streaming or not held:
            held.clear()
            streaming = False
            return None
        preamble = "".join(held)
        held.clear()
        return preamble.strip() or None

    try:
        async for event in events:
            name = getattr(event, "event", None)

            if name == RunEvent.run_content.value:
                content = getattr(event, "content", None)
                if isinstance(content, str) and content:
                    sendable = _hold(content)
                    if sendable:
                        yield _chunk(model, delta={"role": "assistant", "content": sendable})

            elif name == RunEvent.tool_call_started.value:
                # Whatever the model said just before reaching for a tool was
                # about reaching for the tool. It belongs beside the tool row in
                # the chain-of-thought, not in front of the answer.
                preamble = _discard_preamble()
                if preamble:
                    yield _chunk(model, extension={"type": "reasoning", "text": preamble})
                used_a_tool = True
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

        # A short answer never crossed the threshold, so it is still held. No
        # tool call followed it, which is what makes it the answer rather than
        # an announcement.
        if held and not streaming:
            yield _chunk(model, delta={"role": "assistant", "content": "".join(held)})
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

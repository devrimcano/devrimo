"""A failing tool has to be able to say why, all the way to the model.

From a real run on 2026-09-10: "MATH 219 almak icin on kosul dersi var mi?"
took 280 seconds, made eight failing tool calls, and answered that it could not
verify the prerequisite record. Everything needed to answer in one step was
said and then discarded three times over.

  the workspace said           "This resource supports read, not text search"
  the client replaced it with  502 "Workspace operation failed"
  anyio wrapped that in        ExceptionGroup(ExceptionGroup([HTTPException]))
  and journald recorded        error="ExceptionGroup"

The model was told nothing actionable, so it retried the same call with
different wording; and afterwards there was no way to find out what it had
asked for, because the log had a class name and no message.

These tests are about that sentence surviving the trip.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.agents.scholar import hooks
from app.workspace.client import WorkspaceClient, trusted_workspace_token, workspace_error_text


def _result(*, is_error: bool, text: str | None = None, structured=None):
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    return SimpleNamespace(isError=is_error, content=content, structuredContent=structured)


# --- what the workspace said -------------------------------------------------


def test_the_error_the_workspace_wrote_is_what_the_caller_gets():
    """"This resource supports read, not text search" is the whole answer.

    It tells the model exactly what to do next. Replacing it with "Workspace
    operation failed" is what turned one wrong call into eight.
    """
    detail = workspace_error_text(_result(is_error=True, text="This resource supports read, not text search"), "search")
    assert "read, not text search" in detail
    assert "search" in detail, "the model makes several calls; the message has to say which one this was"


def test_an_error_with_no_message_says_so_rather_than_inventing_one():
    detail = workspace_error_text(_result(is_error=True), "read")
    assert "read" in detail
    assert "no reason" in detail


def test_a_very_long_upstream_error_is_bounded():
    """This becomes model input, and an upstream server may return a page."""
    detail = workspace_error_text(_result(is_error=True, text="x" * 5000), "plan")
    assert len(detail) < 700


# --- and how it travels ------------------------------------------------------


class _Transport(httpx.AsyncBaseTransport):
    """A workspace that answers one MCP tool call with whatever it is given."""

    def __init__(self, payload):
        self.payload = payload

    async def handle_async_request(self, request):  # pragma: no cover - shape only
        raise AssertionError("the session transport is not exercised in this test")


async def test_a_failed_call_raises_the_message_and_not_a_task_group(monkeypatch):
    """The failure must arrive unwrapped.

    Raising inside the nested `async with` blocks let anyio's task groups wrap
    it twice, and an ExceptionGroup's own message is "unhandled errors in a
    TaskGroup (1 sub-exception)" - which is what both the model and the log got.
    """
    client = WorkspaceClient("http://workspace.invalid/mcp/")

    class _Session:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _name, _arguments):
            return _result(is_error=True, text="Course Info tool schema is unsupported; missing arguments: semester_code")

    class _Streams:
        async def __aenter__(self):
            return (object(), object())

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr("app.workspace.client.ClientSession", _Session)
    monkeypatch.setattr("app.workspace.client.streamable_http_client", lambda *_a, **_k: _Streams())

    with trusted_workspace_token("a-token"):
        with pytest.raises(HTTPException) as raised:
            await client.call("read", {"resource": {"kind": "catalog.prerequisites"}})

    assert raised.value.status_code == 502
    assert "semester_code" in raised.value.detail
    assert "TaskGroup" not in raised.value.detail


async def test_a_successful_call_still_returns_its_content(monkeypatch):
    """The whole point is that only the failure path changed."""
    client = WorkspaceClient("http://workspace.invalid/mcp/")

    class _Session:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, _name, _arguments):
            return _result(is_error=False, text=json.dumps({"data": {"prerequisites": []}}))

    class _Streams:
        async def __aenter__(self):
            return (object(), object())

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr("app.workspace.client.ClientSession", _Session)
    monkeypatch.setattr("app.workspace.client.streamable_http_client", lambda *_a, **_k: _Streams())

    with trusted_workspace_token("a-token"):
        value = await client.call("read", {"resource": {"kind": "catalog.prerequisites"}})
    assert value == {"data": {"prerequisites": []}}


# --- and what the log keeps --------------------------------------------------


def test_the_log_detail_unwraps_the_task_groups():
    """The real error is inside two wrappers, and it is the only useful part."""
    inner = HTTPException(502, "Course catalog request failed: SAIS said no")
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("unhandled errors", [inner])])
    detail = hooks._tool_error_detail(wrapped)
    assert "SAIS said no" in detail
    assert "TaskGroup" not in detail


def test_the_log_records_the_shape_of_the_call_but_not_the_student_s_text():
    """Enough to reproduce a failing call; nothing the boundary exists to hold.

    A resource kind, a course code and a term are what make a failure
    reproducible. A search query or a mail body is the student's own content and
    belongs on the other side of the workspace boundary, not in journald.
    """
    shown = hooks._loggable_arguments(
        "search",
        {
            "resource": {"kind": "catalog.prerequisites", "key": "2400219", "term": "20261"},
            "query": "hocam ben bu dersi gecemedim ne yapmaliyim",
            "limit": 5,
        },
    )
    assert shown["resource"] == {"kind": "catalog.prerequisites", "key": "2400219", "term": "20261"}
    assert shown["limit"] == 5
    assert "query" not in shown
    assert "gecemedim" not in json.dumps(shown, ensure_ascii=False)


def test_arguments_that_carry_nothing_loggable_are_omitted_rather_than_empty():
    assert hooks._loggable_arguments("send_email", {"draft": {"body": "private"}}) is None
    assert hooks._loggable_arguments("compute", "not-a-dict") is None

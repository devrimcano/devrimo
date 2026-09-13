"""Assistant-worker client: domain writes cross the authenticated MCP boundary."""

import json
from contextlib import contextmanager
from contextvars import ContextVar

import httpx
from fastapi import HTTPException
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_approval_token: ContextVar[str | None] = ContextVar("workspace_email_approval", default=None)

_access_token: ContextVar[str | None] = ContextVar("workspace_access_token", default=None)


@contextmanager
def trusted_workspace_token(token: str, approval_token: str | None = None):
    """Set only by the authenticated request/turn boundary, never model input."""
    marker = _access_token.set(token)
    approval_marker = _approval_token.set(approval_token)
    try:
        yield
    finally:
        _approval_token.reset(approval_marker)
        _access_token.reset(marker)


# A catalog read that answers one of these cannot succeed on a retry: the course
# is absent from the published release for the requested term, not temporarily
# unavailable. A run on 2026-09-11 spent 317 seconds and twenty-three model calls
# re-asking for a missing course as sections, then prerequisites, then
# eligibility, because each 404 read as "try something else". Marking it final in
# the tool result stops the loop even when the same rule in the instructions is
# ignored.
_TERMINAL_CATALOG_MISSES = ("not available in the published release",)
# A memory shape the server rejects is not fixed by sending it again, so the
# result says so rather than letting the model retry six times and then tell the
# student it saved the preference.
_TERMINAL_MEMORY_REJECTIONS = ("for memorychanges",)


def error_detail(exc: BaseException, depth: int = 0) -> str:
    """The real message, with anyio's task-group wrappers unwrapped.

    A failure that leaves the nested `async with` blocks arrives as
    ``ExceptionGroup(ExceptionGroup([...]))``, whose own message is "unhandled
    errors in a TaskGroup (1 sub-exception)". That string tells the model
    nothing, so it retried a read seven times in one measured run. The real
    error is inside; this is what makes it visible.
    """
    inner = getattr(exc, "exceptions", None)
    if inner and depth < 4:
        return "; ".join(error_detail(child, depth + 1) for child in inner[:3])
    detail = getattr(exc, "detail", None)
    message = str(detail if detail is not None else exc).strip()
    return message or exc.__class__.__name__


def is_terminal_catalog_miss(detail: object, *, name: str = "read", arguments: dict | None = None) -> bool:
    """Whether a workspace failure is a final catalog miss for this read.

    The MCP transport reports tool failures as an error result, which used to
    become a generic 502 even when Course Info had given the definitive
    ``not available in the published release`` answer. Only catalog reads are
    promoted to 404; a similarly worded failure from search or another domain
    operation must retain its ordinary gateway error semantics.
    """
    if name != "read":
        return False
    resource = (arguments or {}).get("resource") if isinstance(arguments, dict) else None
    kind = resource.get("kind") if isinstance(resource, dict) else None
    if not isinstance(kind, str) or not kind.startswith("catalog."):
        return False
    folded = str(detail or "").casefold()
    return any(marker in folded for marker in _TERMINAL_CATALOG_MISSES)


def workspace_error_text(result, name: str) -> str:
    """What the workspace actually said, rather than that something went wrong.

    An MCP error result carries its explanation in the content block. This used
    to be discarded in favour of "Workspace operation failed", and the sentence
    lost was routinely the whole answer: "This resource supports read, not text
    search" tells the model exactly what to do next, and instead it retried the
    same search eight times over four and a half minutes.

    Prefixed with the tool, because by the time the model reads this the call
    that produced it is one of several it made.
    """
    for item in getattr(result, "content", None) or []:
        text = str(getattr(item, "text", "") or "").strip()
        if text:
            # Bounded: this is model input, and an upstream server is free to
            # return a page of HTML in an error block.
            detail = text[:600]
            if any(marker in detail.casefold() for marker in _TERMINAL_CATALOG_MISSES):
                detail += (
                    " This is final for the requested term: do not retry it as another catalog resource kind, "
                    "another term, or a search. Tell the student the course is not in the catalog and stop."
                )
            elif any(marker in detail.casefold() for marker in _TERMINAL_MEMORY_REJECTIONS):
                detail += (
                    " The memory change was rejected and was not saved. Do not retry it and do not tell the "
                    "student it was saved."
                )
            return f"{name} failed: {detail}"
    return f"{name} failed, and the workspace gave no reason"


class WorkspaceClient:
    def __init__(self, url: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url
        self.transport = transport

    async def call(self, name, arguments):
        token = _access_token.get()
        if not token:
            raise HTTPException(401, "Authenticated workspace token is unavailable")
        headers = {"Authorization": f"Bearer {token}"}
        if name == "send_email" and _approval_token.get():
            headers["X-Devrimo-Mail-Approval"] = _approval_token.get()
        # Collected inside the session and raised after it, because raising
        # inside these nested `async with` blocks is what turned a one-sentence
        # failure into ExceptionGroup(ExceptionGroup([HTTPException])). anyio's
        # task groups wrap anything that leaves them, so the agent - and the
        # log - saw the class name of the wrapper and nothing else.
        failure: HTTPException | None = None
        try:
            async with httpx.AsyncClient(headers=headers, timeout=120, transport=self.transport) as http_client:
                async with streamable_http_client(self.url, http_client=http_client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments)
                        if result.isError:
                            detail = workspace_error_text(result, name)
                            status = 404 if is_terminal_catalog_miss(detail, name=name, arguments=arguments) else 502
                            failure = HTTPException(status, detail)
                        elif result.structuredContent is not None:
                            return result.structuredContent
                        else:
                            for item in result.content:
                                if item.type == "text":
                                    return json.loads(item.text)
                            failure = HTTPException(502, f"The workspace returned no result for {name}")
        except HTTPException:
            raise
        except Exception as exc:
            # A transport failure or a server-side exception arrives wrapped in
            # an ExceptionGroup; surface its real content instead of the wrapper.
            raise HTTPException(502, f"{name} failed: {error_detail(exc)}") from exc
        raise failure

    async def search(self, request):
        # Flat arguments: the gateway's search takes resource/query/limit
        # directly. See gateway.create_gateway.
        return await self.call("search", request.model_dump())

    async def read(self, resource):
        return await self.call("read", {"resource": resource.model_dump()})

    async def plan(self, request):
        return await self.call("plan", {"request": request})

    async def update(self, resource, changes, expected_revision, idempotency_key):
        return await self.call(
            "update",
            {
                "resource": resource.model_dump(),
                "changes": changes,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
        )

    async def undo(self, resource, expected_revision, idempotency_key):
        return await self.call(
            "undo",
            {
                "resource": resource.model_dump(),
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
        )

    async def send_approved_email(self, draft):
        # Only an API-issued exact-draft capability authorizes the gateway;
        # the worker's assertion that Agno resumed is never sufficient.
        return await self.call("send_email", {"draft": draft.model_dump()})

    async def compute(self, expression):
        return await self.call("compute", {"expression": expression})

    async def authorize(self):
        if not _access_token.get():
            raise HTTPException(401, "Authenticated workspace token is unavailable")

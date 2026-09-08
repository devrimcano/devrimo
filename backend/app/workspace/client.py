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
        async with httpx.AsyncClient(headers=headers, timeout=120, transport=self.transport) as http_client:
            async with streamable_http_client(self.url, http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    if result.isError:
                        raise HTTPException(502, "Workspace operation failed")
                    if result.structuredContent is not None:
                        return result.structuredContent
                    for item in result.content:
                        if item.type == "text":
                            return json.loads(item.text)
                    raise HTTPException(502, "Workspace returned no result")

    async def search(self, request):
        return await self.call("search", {"request": request.model_dump()})

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

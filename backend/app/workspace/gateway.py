"""Authenticated, stateless MCP transport over the canonical workspace service."""

import hashlib
import json
from urllib.parse import urlsplit

from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from app.auth.jwt import verify_access_token
from app.config import get_settings
from app.planning.service import SemesterPlanRequest
from app.workspace.resources import EmailDraft, ResourceRef, SearchRequest, SearchResource
from app.workspace.service import WorkspaceService


class BearerIdentity:
    """Verify every HTTP request; transport sessions cannot substitute identity."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = authorization.partition(" ")
        try:
            if scheme.lower() != "bearer" or not token:
                raise HTTPException(401, "Unauthorized")
            user = verify_access_token(token)
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
        scope.setdefault("state", {})["workspace_user"] = user
        await self.app(scope, receive, send)


def create_gateway():
    origins = get_settings().cors_origin_list
    hosts = ["localhost", "127.0.0.1", "[::1]", "localhost:*", "127.0.0.1:*", "[::1]:*"]
    hosts.extend(urlsplit(origin).netloc for origin in origins)
    hosts.extend(host.strip() for host in get_settings().workspace_gateway_allowed_hosts.split(",") if host.strip())
    if get_settings().workspace_gateway_url:
        hosts.append(urlsplit(get_settings().workspace_gateway_url).netloc)
    server = FastMCP(
        "Devrimo Workspace",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
        ),
    )

    def service(ctx: Context):
        user = ctx.request_context.request.state.workspace_user
        return WorkspaceService(user.id)

    @server.tool()
    async def search(
        resource: SearchResource,
        query: str = "",
        limit: int = 10,
        record_types: list[str] | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        ctx: Context = None,
    ) -> dict:
        """Search an authenticated workspace resource.

        Flat arguments rather than a nested `request` object: the model kept
        sending the resource and query at the top level, which the wrapper
        rejected as a missing field. platform_tools.search must match this.
        """
        return await service(ctx).search(
            SearchRequest(
                resource=resource,
                query=query,
                limit=limit,
                record_types=record_types or [],
                starts_after=starts_after,
                starts_before=starts_before,
            )
        )

    @server.tool()
    async def read(resource: ResourceRef, ctx: Context) -> dict:
        """Read one typed workspace resource."""
        return await service(ctx).read(resource)

    @server.tool()
    async def plan(request: SemesterPlanRequest, ctx: Context) -> dict:
        """Calculate a semester proposal using verified records."""
        return await service(ctx).plan(request.model_dump())

    @server.tool()
    async def update(
        resource: ResourceRef,
        changes: dict,
        expected_revision: int,
        idempotency_key: str,
        ctx: Context,
    ) -> dict:
        """Update an editable resource with optimistic concurrency.

        `changes` is shaped by `resource.kind` and validated against it by
        WorkspaceService.update. It is deliberately untyped here: as a union of
        four models it expanded to roughly 17,500 characters of JSON schema,
        larger than the other six tools together and carried on every model
        step of every turn. See platform_tools.update, which this must match -
        test_stateless_mcp_seven_tools_and_private_identity pins the two
        surfaces together.
        """
        return await service(ctx).update(
            resource,
            changes.model_dump(exclude_unset=True) if hasattr(changes, "model_dump") else changes,
            expected_revision,
            idempotency_key,
        )

    @server.tool()
    async def undo(resource: ResourceRef, expected_revision: int, idempotency_key: str, ctx: Context) -> dict:
        """Undo the most recent resource revision."""
        return await service(ctx).undo(resource, expected_revision, idempotency_key)

    @server.tool()
    async def send_email(draft: EmailDraft, ctx: Context) -> dict:
        """Prepare an immutable message reference; sending requires Devrimo chat approval."""
        workspace = service(ctx)
        await workspace.authorize()
        capability = ctx.request_context.request.headers.get("x-devrimo-mail-approval")
        if capability:
            from app.workspace.approvals import execute_approved

            return await execute_approved(workspace.user_id, capability, draft, workspace.send_approved_email)
        canonical = json.dumps(
            {"user_id": str(workspace.user_id), "draft": draft.model_dump()}, sort_keys=True, separators=(",", ":")
        )
        return {
            "status": "approval_required",
            "action_reference": hashlib.sha256(canonical.encode()).hexdigest(),
            "draft": draft.model_dump(),
            "approval_channel": "devrimo_chat",
            "detail": "Submit this exact draft in Devrimo chat and approve its pending send_email call.",
        }

    @server.tool()
    async def compute(expression: str, ctx: Context) -> dict:
        """Evaluate bounded arithmetic."""
        return await service(ctx).compute(expression)

    app = server.streamable_http_app()
    app.add_middleware(BearerIdentity)
    return server, app

"""Exactly seven operations over the same workspace services as the API."""

from uuid import UUID

from agno.tools.decorator import tool

from app.planning.service import SemesterPlanRequest
from app.workspace.resources import EmailDraft, ResourceRef, SearchRequest, SearchResource
from app.workspace.service import WorkspaceService


def build_platform_tools(user_id: UUID) -> list:
    from app.config import get_settings

    settings = get_settings()
    if settings.database_runtime_role == "assistant":
        from app.workspace.client import WorkspaceClient

        gateway_url = getattr(settings, "workspace_gateway_url", "")
        if not gateway_url:
            raise RuntimeError("Assistant workers require WORKSPACE_GATEWAY_URL")
        workspace = WorkspaceClient(gateway_url)
    else:
        workspace = WorkspaceService(user_id)

    @tool(name="search")
    async def search(
        resource: SearchResource,
        query: str = "",
        limit: int = 10,
        record_types: list[str] | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
    ) -> dict:
        """Text-search campus.knowledge, researcher, mail.messages, catalog.departments or catalog.department."""
        return await workspace.search(
            SearchRequest(
                resource=resource,
                query=query,
                limit=limit,
                record_types=record_types or [],
                starts_after=starts_after,
                starts_before=starts_before,
            )
        )

    @tool(name="read")
    async def read(resource: ResourceRef) -> dict:
        """Read one resource by kind.

        Course kinds take the code in `key` ("EE 201" or 5670201) or a department in `department`; `term`
        defaults to the active term. Find a course by name via catalog.department, then catalog.courses.
        student.registered_schedule is SAIS; planning.timetable is the editable week. Set `expand` true
        only when the student asked for every item after a partial list.
        """
        return await workspace.read(ResourceRef.model_validate(resource))

    @tool(name="plan")
    async def plan(request: SemesterPlanRequest) -> dict:
        """Propose a deterministic semester plan from verified transcript and catalog; does not save."""
        return await workspace.plan(SemesterPlanRequest.model_validate(request).model_dump())

    @tool(name="update")
    async def update(
        resource: ResourceRef,
        changes: dict,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict:
        """Save an editable resource against its current revision with a unique request key.

        `changes` follows `resource.kind`:
          - planning.timetable: the `application` object a prior `plan` returned, copied verbatim - it
            already holds the exact entries to write.
          - my.preferences / my.update_state: the shape that key expects.
          - my.memory: {"memories": [{"content": ...}]}. Read my.memory first and send the whole list with
            its revision; the server replaces it atomically, so a partial list deletes the rest. `id` is
            optional for new entries.
        """
        return await workspace.update(
            ResourceRef.model_validate(resource),
            changes.model_dump(exclude_unset=True) if hasattr(changes, "model_dump") else changes,
            expected_revision,
            idempotency_key,
        )

    @tool(name="undo")
    async def undo(resource: ResourceRef, expected_revision: int, idempotency_key: str) -> dict:
        """Undo the latest saved timetable revision, preserving history and rejecting stale edits."""
        return await workspace.undo(ResourceRef.model_validate(resource), expected_revision, idempotency_key)

    @tool(name="send_email", requires_confirmation=True)
    async def send_email(draft: EmailDraft) -> dict:
        """Send or reply to the exact message shown for explicit student confirmation."""
        return await workspace.send_approved_email(EmailDraft.model_validate(draft))

    @tool(name="compute")
    async def compute(expression: str) -> dict:
        """Evaluate bounded arithmetic without executing code."""
        return await workspace.compute(expression)

    return [search, read, plan, update, undo, send_email, compute]

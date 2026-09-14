"""Exactly seven operations over the same workspace services as the API."""

from uuid import UUID

from agno.tools.decorator import tool

from app.planning.service import SemesterPlanRequest
from app.workspace.resources import (
    EmailDraft,
    ResourceKind,
    ResourceRef,
    SearchableKind,
    SearchRequest,
    SearchResource,
    scoped_ref,
)
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
        resource: SearchResource | None = None,
        kind: SearchableKind | None = None,
        query: str = "",
        limit: int = 10,
        record_types: list[str] | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        department: str | None = None,
        category: str | None = None,
        term: str | None = None,
    ) -> dict:
        """Text-search campus.knowledge, researcher, mail.messages, catalog.departments or catalog.department.

        Pass `kind` directly (plus `query`) or the same fields inside a `resource` object; both work.
        """
        ref = scoped_ref(SearchResource, "search", resource, kind, department=department, category=category, term=term)
        return await workspace.search(
            SearchRequest(
                resource=ref,
                query=query,
                limit=limit,
                record_types=record_types or [],
                starts_after=starts_after,
                starts_before=starts_before,
            )
        )

    @tool(name="read")
    async def read(
        resource: ResourceRef | None = None,
        kind: ResourceKind | None = None,
        key: str | None = None,
        department: str | None = None,
        category: str | None = None,
        program_type: str | None = None,
        folder: str | None = None,
        attachment: str | None = None,
        term: str | None = None,
        section: str | None = None,
        expand: bool = False,
    ) -> dict:
        """Read one resource by kind.

        Course kinds take the code in `key` ("EE 201" or 5670201) or a department in `department`; `term`
        defaults to the active term. Find a course by name via catalog.department, then catalog.courses.
        student.registered_schedule is SAIS; planning.timetable is the editable week. Set `expand` true
        only when the student asked for every item after a partial list.
        Pass the fields flat ({"kind": ..., "key": ...}) or inside a `resource` object; both work.
        """
        ref = scoped_ref(
            ResourceRef,
            "read",
            resource,
            kind,
            key=key,
            department=department,
            category=category,
            program_type=program_type,
            folder=folder,
            attachment=attachment,
            term=term,
            section=section,
            expand=expand,
        )
        return await workspace.read(ref)

    @tool(name="plan")
    async def plan(request: SemesterPlanRequest) -> dict:
        """Propose a deterministic semester plan from verified transcript and catalog; does not save."""
        return await workspace.plan(SemesterPlanRequest.model_validate(request).model_dump())

    @tool(name="update")
    async def update(
        changes: dict,
        expected_revision: int,
        idempotency_key: str,
        resource: ResourceRef | None = None,
        kind: ResourceKind | None = None,
        key: str | None = None,
        department: str | None = None,
        term: str | None = None,
    ) -> dict:
        """Save an editable resource against its current revision with a unique request key.

        Pass the resource flat (`kind`, `key`, `term`) or as a `resource` object; both work.

        `changes` follows `resource.kind`:
          - planning.timetable: the `application` object a prior `plan` returned, copied verbatim - it
            already holds the exact entries to write.
          - my.preferences / my.update_state: the shape that key expects.
          - my.memory: {"memories": [{"content": ...}]}. Read my.memory first and send the whole list with
            its revision; the server replaces it atomically, so a partial list deletes the rest. `id` is
            optional for new entries.
        """
        ref = scoped_ref(ResourceRef, "update", resource, kind, key=key, department=department, term=term)
        return await workspace.update(
            ref,
            changes.model_dump(exclude_unset=True) if hasattr(changes, "model_dump") else changes,
            expected_revision,
            idempotency_key,
        )

    @tool(name="undo")
    async def undo(
        expected_revision: int,
        idempotency_key: str,
        resource: ResourceRef | None = None,
        kind: ResourceKind | None = None,
        key: str | None = None,
        department: str | None = None,
        term: str | None = None,
    ) -> dict:
        """Undo the latest saved timetable revision, preserving history and rejecting stale edits."""
        ref = scoped_ref(ResourceRef, "undo", resource, kind, key=key, department=department, term=term)
        return await workspace.undo(ref, expected_revision, idempotency_key)

    @tool(name="send_email", requires_confirmation=True)
    async def send_email(draft: EmailDraft) -> dict:
        """Send or reply to the exact message shown for explicit student confirmation."""
        return await workspace.send_approved_email(EmailDraft.model_validate(draft))

    @tool(name="compute")
    async def compute(expression: str) -> dict:
        """Evaluate bounded arithmetic without executing code."""
        return await workspace.compute(expression)

    return [search, read, plan, update, undo, send_email, compute]

"""Transport-independent workspace commands with trusted user identity."""

import inspect
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException

from app.admin.directory import active_account
from app.campus.mcp_results import mcp_payload
from app.campus.sessions import integration_session
from app.db.session import SessionLocal
from app.workspace.resources import UPSTREAM, EmailDraft, ResourceRef, SearchRequest


def current_term():
    from app.planning.service import current_term as resolve

    return resolve()


def _catalog_source() -> str:
    try:
        from app.planning.catalog_service import published_catalog_reads_enabled

        return "academic_catalog" if published_catalog_reads_enabled() else "course_info"
    except Exception:
        return "course_info"


def envelope(ref: ResourceRef, data, *, source: str = "devrimo", freshness: str = "cached") -> dict:
    return {
        "resource": ref.model_dump(exclude_none=True),
        "data": data.model_dump(mode="json") if hasattr(data, "model_dump") else data,
        "provenance": {"source": source, "accessed_at": datetime.now(UTC).isoformat(), "freshness": freshness},
    }


class WorkspaceService:
    def __init__(self, user_id: UUID):
        self.user_id = user_id

    async def authorize(self):
        async with SessionLocal() as db:
            if await active_account(db, self.user_id) is None:
                raise HTTPException(403, "Account is inactive")

    async def domain(self, name, **arguments):
        from app.workspace.domain_reads import build_domain_reads

        # These functions are adapters to existing owner services, never exposed
        # as additional tools to the model.
        function = build_domain_reads(self.user_id)[name]
        value = function(**arguments)
        return await value if inspect.isawaitable(value) else value

    async def search(self, request: SearchRequest):
        await self.authorize()
        ref = request.resource
        if ref.kind == "researcher":
            from app.workspace.queries import search_researchers

            async with SessionLocal() as db:
                return envelope(ref, await search_researchers(db, request.query, request.limit), source="avesis")
        if ref.kind == "campus.knowledge":
            data = await self.domain(
                "search_campus_knowledge",
                query=request.query,
                limit=request.limit,
                record_types=request.record_types,
                starts_after=request.starts_after,
                starts_before=request.starts_before,
            )
            return envelope(ref, data, source="campus_index")
        if ref.kind in {"mail.messages", "catalog.departments"}:
            return await self.upstream(ref, query=request.query, limit=request.limit)
        if ref.kind == "catalog.department":
            return envelope(ref, await self.domain("lookup_department", value=request.query))
        if request.query:
            # Names the call that works, because this is read by a model that
            # will otherwise try the same search again with different wording.
            # It did: twelve failing search calls on one free-elective question,
            # because "supports read, not text search" said what was wrong and
            # not what to do instead.
            raise HTTPException(
                422,
                f"{ref.kind} has no text search. Call read with "
                f'{{"kind": "{ref.kind}", "key": "<identifier>"}} instead. '
                "To find a course by name, search catalog.departments for the department, "
                "then read catalog.courses with that department.",
            )
        return await self.read(ref)

    async def read(self, ref: ResourceRef):
        await self.authorize()
        if ref.kind in {"my.preferences", "my.update_state"}:
            from app.student.workspace import read_resource

            if not ref.key:
                raise HTTPException(422, "A resource key is required")
            async with SessionLocal() as db:
                return envelope(
                    ref,
                    await read_resource(
                        db, self.user_id, "preference" if ref.kind == "my.preferences" else "update", ref.key
                    ),
                )
        if ref.kind == "my.memory":
            from app.agents.memory import read_memories

            return envelope(ref, await read_memories(self.user_id))
        if ref.kind == "researcher":
            from app.workspace.queries import read_researcher

            async with SessionLocal() as db:
                return envelope(ref, await read_researcher(db, ref.key), source="avesis")
        if ref.kind == "my.academic_snapshot":
            from app.workspace.queries import academic_snapshot

            async with SessionLocal() as db:
                return envelope(ref, await academic_snapshot(db, self.user_id, ref.term), source="sais")
        if ref.kind == "my.updates":
            from app.student.updates import get_updates

            async with SessionLocal() as db:
                return envelope(ref, await get_updates(db, self.user_id))
        if ref.kind == "campus.page":
            return envelope(ref, await self.domain("read_campus_page", url=ref.key or ""), source="campus_index")
        if ref.kind == "catalog.department":
            return envelope(ref, await self.domain("lookup_department", value=ref.key or ""))
        if ref.kind in {"catalog.sections", "catalog.eligibility"}:
            arguments = {"course_code": ref.key or "", "semester": ref.term or ""}
            name = "get_course_sections"
            if ref.kind == "catalog.eligibility":
                name = "check_section_eligibility"
                arguments["section"] = ref.section or ""
            return envelope(ref, await self.domain(name, **arguments), source=_catalog_source())
        if ref.kind == "planning.course_group":
            return envelope(
                ref,
                await self.domain("get_course_group", course=ref.key or "", term=ref.term or "", section=ref.section),
            )
        if ref.kind == "planning.timetable":
            from app.planning.workspace import read_timetable

            async with SessionLocal() as db:
                return envelope(ref, await read_timetable(db, self.user_id, ref.term or current_term()))
        if ref.kind not in UPSTREAM:
            raise HTTPException(422, "Resource does not support read")
        return await self.upstream(ref)

    @staticmethod
    def _department(ref) -> str | None:
        """The department this resource belongs to, which is not always its key.

        `catalog.department` is keyed by a department code, so its key is the
        department. Every other catalog resource is keyed by a *course* code -
        seven digits whose first three are the department - and passing the
        whole thing as the department is what made every prerequisite read fail
        with 502 "Error executing tool get_course_prerequisites" while the same
        course's sections read fine. Verified against the source: department
        "236" with course "2360219" returns MATH 219's prerequisites; department
        "2360219" with the same course returns the 502.

        An explicit `ref.department` still wins, because a model that names the
        department knows something the key does not carry.
        """
        if ref.department:
            return ref.department
        key = (ref.key or "").strip()
        if ref.kind == "catalog.department":
            return key or None
        # A METU course code is seven digits and begins with its department.
        if len(key) == 7 and key.isdigit():
            return key[:3]
        return key or None

    async def upstream(self, ref, *, query="", limit=10):
        integration, method = UPSTREAM[ref.kind]
        if ref.kind == "mail.messages" and query:
            method = "search_emails"
        if ref.kind == "catalog.departments" and query:
            method = "search_departments"
        values = {
            "query": query,
            "limit": limit,
            "mark_as_read": False,
            "course_id": ref.key,
            "course_code": ref.key,
            "course": ref.key,
            "department": self._department(ref),
            "message_id": ref.key,
            "email_id": ref.key,
            "uid": ref.key,
            "folder": ref.folder,
            "attachment_id": ref.attachment,
            "semester": ref.term,
            "term": ref.term,
            "category": ref.category or ref.key,
            "category_id": ref.category or ref.key,
            "program_type": ref.program_type,
        }
        if integration == "course_info":
            from app.campus.course_info import call_course_info

            async with SessionLocal() as db:
                data = await call_course_info(
                    db,
                    self.user_id,
                    method,
                    {
                        k: str(values[k])
                        for k in ("department", "course", "semester", "category", "program_type", "query")
                        if values.get(k) is not None
                    },
                )
            return envelope(ref, data, source=_catalog_source())
        async with integration_session(self.user_id, integration) as connected:
            data = await self.invoke(connected, integration, method, values)
        return envelope(ref, data, source=integration, freshness="live")

    @staticmethod
    async def invoke(connected, integration, method, values):
        function = next(
            (
                item.functions.get(f"{integration}_{method}")
                for item in connected
                if item.functions.get(f"{integration}_{method}")
            ),
            None,
        )
        if function is None:
            raise HTTPException(502, "Requested resource is unavailable")
        parameters = function.parameters or {}
        args = {key: values[key] for key in parameters.get("properties", {}) if values.get(key) is not None}
        if any(key not in args for key in parameters.get("required", [])):
            raise HTTPException(422, "Resource reference is missing required identifiers")
        value = function.entrypoint(**args)
        if inspect.isawaitable(value):
            value = await value
        content = getattr(value, "content", None)
        if isinstance(content, str) and content.startswith("Error from MCP tool "):
            raise HTTPException(502, "Campus integration reported an error")
        payload = mcp_payload(value)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise HTTPException(502, "Campus integration reported an unsuccessful operation")
        return payload

    async def plan(self, request: dict):
        await self.authorize()
        from app.planning.proposals import proposal_changes
        from app.planning.service import SemesterPlanRequest, plan_semester
        from app.planning.workspace import read_timetable

        parsed = SemesterPlanRequest.model_validate(request)
        async with SessionLocal() as db:
            proposal = await plan_semester(db, self.user_id, parsed)
            changes = proposal_changes(proposal)
            proposal["application"] = None
            if changes is not None:
                current = await read_timetable(db, self.user_id, parsed.term)
                proposal["application"] = {
                    "resource": {"kind": "planning.timetable", "term": parsed.term},
                    "expected_revision": current.revision,
                    "changes": changes.model_dump(mode="json", exclude_none=True),
                    "effect": (
                        "Replace timetable entries; apply with update and a new idempotency_key only when requested."
                    ),
                }
            return envelope(
                ResourceRef(kind="planning.proposal", term=parsed.term), proposal
            )

    async def update(self, resource: ResourceRef, changes: dict, expected_revision: int, idempotency_key: str):
        await self.authorize()
        if resource.kind in {"my.preferences", "my.update_state"}:
            from app.student.workspace import update_resource

            if not resource.key:
                raise HTTPException(422, "A resource key is required")
            async with SessionLocal() as db:
                return envelope(
                    resource,
                    await update_resource(
                        db,
                        self.user_id,
                        "preference" if resource.kind == "my.preferences" else "update",
                        resource.key,
                        changes,
                        expected_revision,
                        idempotency_key,
                    ),
                )
        if resource.kind == "my.memory":
            from app.agents.memory import mutate_memories

            return envelope(resource, await mutate_memories(self.user_id, changes, expected_revision, idempotency_key))
        if resource.kind != "planning.timetable":
            raise HTTPException(403, "Resource is read-only")
        from app.planning.models import PlanChanges, PlanIdempotencyError
        from app.planning.workspace import update_timetable

        async with SessionLocal() as db:
            try:
                result = await update_timetable(
                    db,
                    self.user_id,
                    resource.term or current_term(),
                    PlanChanges.model_validate(changes),
                    expected_revision,
                    idempotency_key,
                )
            except PlanIdempotencyError as exc:
                raise HTTPException(409, str(exc)) from exc
            return envelope(resource, result)

    async def undo(self, resource: ResourceRef, expected_revision: int, idempotency_key: str):
        await self.authorize()
        if resource.kind == "my.memory":
            from app.agents.memory import mutate_memories

            return envelope(
                resource, await mutate_memories(self.user_id, None, expected_revision, idempotency_key, undo=True)
            )
        if resource.kind != "planning.timetable":
            raise HTTPException(403, "Resource is read-only")
        from app.planning.models import PlanIdempotencyError
        from app.planning.workspace import undo_timetable

        async with SessionLocal() as db:
            try:
                result = await undo_timetable(
                    db, self.user_id, resource.term or current_term(), expected_revision, idempotency_key
                )
            except PlanIdempotencyError as exc:
                raise HTTPException(409, str(exc)) from exc
            return envelope(resource, result)

    async def compute(self, expression: str):
        from app.planning.calculator import compute

        await self.authorize()
        return {"value": compute(expression)}

    async def send_approved_email(self, draft: EmailDraft):
        """Called ONLY by the Agno function after its persisted confirmation resumes."""
        await self.authorize()
        async with integration_session(self.user_id, "webmail") as connected:
            values = {
                "to": draft.to,
                "subject": draft.subject,
                "body_text": draft.body,
                "cc": draft.cc or None,
                "bcc": draft.bcc or None,
                "body_html": draft.body_html,
                "reply_to": draft.reply_to,
            }
            if draft.reply_to_message_id:
                from email.utils import getaddresses

                original = await self.invoke(
                    connected,
                    "webmail",
                    "read_email",
                    {
                        "message_id": draft.reply_to_message_id,
                        "folder": draft.folder,
                        "mark_as_read": False,
                    },
                )
                if not isinstance(original, dict) or not original.get("message_id"):
                    raise HTTPException(502, "Original message headers could not be verified")
                recipient = original.get("reply_to") or original.get("sender_email") or original.get("sender")
                expected = {address.lower() for _, address in getaddresses([str(recipient or "")]) if address}
                approved = {address.lower() for _, address in getaddresses([draft.to]) if address}
                if not expected or approved != expected:
                    raise HTTPException(422, "Reply recipient must match the original message reply address")
                function = next(
                    (
                        item.functions.get("webmail_send_email")
                        for item in connected
                        if item.functions.get("webmail_send_email")
                    ),
                    None,
                )
                if function is None or not {"in_reply_to", "references"} <= set(
                    (function.parameters or {}).get("properties", {})
                ):
                    raise HTTPException(502, "Webmail must support exact-draft reply headers")
                references = (original.get("headers") or {}).get("References", original["message_id"])
                values.update(in_reply_to=original["message_id"], references=references)
            # The adapter sends the approved text verbatim. Upstream reply_email
            # appends quotations and recomputes the recipient, so it is not used.
            await self.authorize()
            return await self.invoke(connected, "webmail", "send_email", values)

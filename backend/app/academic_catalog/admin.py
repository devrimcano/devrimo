"""Administrator HTTP API for reviewing and publishing academic catalog data."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.academic_catalog import service
from app.academic_catalog.models import CatalogCourse, CatalogTerm
from app.academic_catalog.schemas import (
    CatalogCourseDetailOut,
    CatalogCourseListOut,
    CatalogDraftOut,
    CatalogImportJobOut,
    CatalogImportsOut,
    CatalogOperationOut,
    CatalogReleasesOut,
    CatalogSourceObservationOut,
    DraftCreateIn,
    DraftPatchIn,
    ImportIn,
    PublishIn,
    RemoveOverridesIn,
    RollbackIn,
)
from app.admin.audit import record_event
from app.admin.auth import AdminPermission, AdminPrincipal, require
from app.admin.directory import METU_ID
from app.config import get_settings
from app.db.session import get_db
from app.logging import get_logger
from app.observability.client import capture

router = APIRouter(prefix="/catalog")
logger = get_logger(__name__)


def _organization(principal: AdminPrincipal) -> UUID:
    # Super administrators may operate on the seeded METU catalog.  A future
    # multi-campus UI can select an explicit organization after the auth layer
    # supplies that scope; ordinary campus administrators stay pinned to their
    # membership organization.
    return principal.organization_id or METU_ID


def _as_error(exc: ValueError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


def _catalog_outcome(result: str) -> str:
    if result in {"success", "queued"}:
        return "success"
    if result in {"blocked", "denied"}:
        return "expected_failure"
    return "unexpected_failure"


async def _record_catalog_event(
    db: AsyncSession,
    principal: AdminPrincipal,
    *,
    action: str,
    result: str,
    term: str,
    draft_id: str | None = None,
    job_id: str | None = None,
    operation_id: str | None = None,
    release_id: str | None = None,
    target_release_id: str | None = None,
    idempotency_key: str | None = None,
    status_value: str | None = None,
    item_count: int | None = None,
) -> None:
    """Report one committed catalog mutation using identifiers and counts only."""
    actor_id = principal.user.id
    organization_id = _organization(principal)
    after = {
        key: value
        for key, value in {
            "term": term,
            "draft_id": draft_id,
            "job_id": job_id,
            "operation_id": operation_id,
            "release_id": release_id,
            "target_release_id": target_release_id,
            "idempotency_key": idempotency_key,
            "status": status_value,
            "item_count": item_count,
        }.items()
        if value is not None
    }
    try:
        await record_event(
            db,
            actor_user_id=actor_id,
            action=action,
            result=result,
            organization_id=organization_id,
            after=after,
        )
    except Exception as exc:  # pragma: no cover - telemetry must not break a committed mutation
        logger.warning("catalog_admin_audit_failed", action=action, error_type=type(exc).__name__)
    try:
        # Publication retries are request events. The stable operation and
        # idempotency identifiers let dashboards deduplicate them instead of
        # counting a replay as another release.
        capture(
            "catalog_admin_action",
            distinct_id=str(actor_id),
            actor_user_id=str(actor_id),
            organization_id=str(organization_id),
            action=action,
            result=result,
            outcome=_catalog_outcome(result),
            term=term,
            draft_id=draft_id,
            job_id=job_id,
            operation_id=operation_id,
            release_id=release_id,
            target_release_id=target_release_id,
            idempotency_key=idempotency_key,
            status=status_value,
            item_count=item_count,
        )
    except Exception as exc:  # pragma: no cover - telemetry must not break a committed mutation
        logger.warning("catalog_admin_event_failed", action=action, error_type=type(exc).__name__)


@router.get("/courses", response_model=CatalogCourseListOut)
async def list_courses(
    term: str = Query(..., min_length=1, max_length=32),
    department: str | None = Query(default=None, max_length=32),
    query: str | None = Query(default=None, max_length=200),
    state: str | None = Query(default=None, max_length=32),
    offset: int = Query(default=0, ge=0, le=100_000),
    limit: int = Query(default=50, ge=1, le=500),
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_read)),
    db: AsyncSession = Depends(get_db),
) -> CatalogCourseListOut:
    try:
        return await service.list_course_rows(
            db,
            _organization(principal),
            term,
            department=department,
            query=query,
            state=state,
            offset=offset,
            limit=limit,
        )
    except ValueError as exc:
        raise _as_error(exc) from exc


@router.get("/courses/{course_code}", response_model=CatalogCourseDetailOut)
async def course_detail(
    course_code: str,
    term: str = Query(..., min_length=1, max_length=32),
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_read)),
    db: AsyncSession = Depends(get_db),
) -> CatalogCourseDetailOut:
    try:
        return await service.course_detail(db, _organization(principal), term, course_code)
    except ValueError as exc:
        raise _as_error(exc) from exc


@router.post("/drafts", status_code=status.HTTP_201_CREATED, response_model=CatalogDraftOut)
async def create_draft(
    body: DraftCreateIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_write)),
    db: AsyncSession = Depends(get_db),
) -> CatalogDraftOut:
    try:
        draft = await service.create_draft(
            db,
            _organization(principal),
            body.term,
            body.course_code,
            base_revision_id=body.base_revision_id,
            data=body.typed_data(),
            reason=body.reason,
            created_by=principal.user.id,
        )
        # ``created_at``/``updated_at`` are server defaults.  Refresh before
        # synchronous serialization so async SQLAlchemy never attempts an
        # implicit lazy load while building the response.
        await db.refresh(draft)
        payload = service.serialize_draft(draft, course_code=body.course_code, term=body.term)
        await db.commit()
        await _record_catalog_event(
            db,
            principal,
            action="catalog.draft.create",
            result="success",
            term=body.term,
            draft_id=payload.get("id"),
            status_value=payload.get("state"),
            item_count=1,
        )
        return payload
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.patch("/drafts/{draft_id}", response_model=CatalogDraftOut)
async def patch_draft(
    draft_id: UUID,
    body: DraftPatchIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_write)),
    db: AsyncSession = Depends(get_db),
) -> CatalogDraftOut:
    try:
        draft = await service.patch_draft(
            db,
            _organization(principal),
            draft_id,
            expected_revision=body.expected_revision,
            patch=body.typed_patch(),
            reason=body.reason,
            updated_by=principal.user.id,
            verify=body.verify,
            verification_evidence=body.verification_evidence,
        )
        await db.refresh(draft)
        term = await db.get(CatalogTerm, draft.term_id)
        course = await db.get(CatalogCourse, draft.course_id)
        payload = service.serialize_draft(draft, term=term.term_code if term else None,
                                          course_code=course.course_code if course else None)
        await db.commit()
        await _record_catalog_event(
            db,
            principal,
            action="catalog.draft.patch",
            result="success",
            term=payload.get("term"),
            draft_id=str(draft_id),
            status_value=payload.get("state"),
            item_count=1,
        )
        return payload
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.post("/publish", response_model=CatalogOperationOut)
async def publish(
    body: PublishIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_publish)),
    db: AsyncSession = Depends(get_db),
) -> CatalogOperationOut:
    try:
        result = await service.publish_drafts(
            db,
            _organization(principal),
            body.term,
            body.draft_ids,
            expected_release_id=body.expected_release_id,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
            created_by=principal.user.id,
            acknowledge_conflicts=body.acknowledge_conflicts,
        )
        await db.commit()
        await _record_catalog_event(
            db,
            principal,
            action="catalog.publish",
            result="success",
            term=body.term,
            operation_id=result.get("operation_id"),
            release_id=result.get("release_id"),
            idempotency_key=body.idempotency_key,
            status_value=result.get("status"),
            item_count=len(result.get("published_draft_ids") or []),
        )
        return result
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.post("/rollback", response_model=CatalogOperationOut)
async def rollback(
    body: RollbackIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_publish)),
    db: AsyncSession = Depends(get_db),
) -> CatalogOperationOut:
    try:
        result = await service.rollback_release(
            db,
            _organization(principal),
            body.term,
            body.target_release_id,
            expected_release_id=body.expected_release_id,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
            created_by=principal.user.id,
        )
        await db.commit()
        await _record_catalog_event(
            db,
            principal,
            action="catalog.rollback",
            result="success",
            term=body.term,
            operation_id=result.get("operation_id"),
            release_id=result.get("release_id"),
            target_release_id=result.get("target_release_id"),
            idempotency_key=body.idempotency_key,
            status_value=result.get("status"),
            item_count=result.get("course_count"),
        )
        return result
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.post("/imports", status_code=status.HTTP_202_ACCEPTED, response_model=CatalogImportJobOut)
async def create_import(
    body: ImportIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_write)),
    db: AsyncSession = Depends(get_db),
) -> CatalogImportJobOut:
    if not get_settings().academic_catalog_ingestion_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Catalog imports are disabled. Enable ACADEMIC_CATALOG_INGESTION_ENABLED on the API and catalog worker.")
    try:
        job = await service.enqueue_import(
            db,
            _organization(principal),
            body.term,
            department=body.department,
            course_codes=body.course_codes,
            reason=body.reason,
            requested_by=principal.user.id,
        )
        await db.flush()
        await db.refresh(job)
        payload = service.serialize_import_job(job)
        await db.commit()
        await _record_catalog_event(
            db,
            principal,
            action="catalog.import.enqueue",
            result="success",
            term=body.term,
            job_id=payload.get("id"),
            status_value=payload.get("status"),
            item_count=len(payload["course_codes"]) if payload.get("course_codes") else None,
        )
        return payload
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.get("/imports", response_model=CatalogImportsOut)
async def list_imports(
    term: str | None = Query(default=None, max_length=32),
    job_status: str | None = Query(default=None, alias="status", max_length=32),
    limit: int = Query(default=50, ge=1, le=500),
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_read)),
    db: AsyncSession = Depends(get_db),
) -> CatalogImportsOut:
    return await service.list_import_jobs(db, _organization(principal), term=term, job_status=job_status, limit=limit)


@router.get("/releases", response_model=CatalogReleasesOut)
async def list_releases(
    term: str = Query(..., min_length=1, max_length=32),
    limit: int = Query(default=50, ge=1, le=500),
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_read)),
    db: AsyncSession = Depends(get_db),
) -> CatalogReleasesOut:
    return await service.list_catalog_releases(db, _organization(principal), term, limit=limit)


@router.post("/drafts/{draft_id}/remove-overrides", response_model=CatalogDraftOut)
async def remove_draft_overrides(
    draft_id: UUID,
    body: RemoveOverridesIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_write)),
    db: AsyncSession = Depends(get_db),
) -> CatalogDraftOut:
    from app.academic_catalog.overrides import remove_overrides

    try:
        draft = await remove_overrides(
            db, _organization(principal), draft_id,
            expected_revision=body.expected_revision, fields=body.fields,
            reason=body.reason, updated_by=principal.user.id,
        )
        await db.refresh(draft)
        term = await db.get(CatalogTerm, draft.term_id)
        course = await db.get(CatalogCourse, draft.course_id)
        payload = service.serialize_draft(draft, term=term.term_code if term else None,
                                          course_code=course.course_code if course else None)
        await db.commit()
        await _record_catalog_event(
            db,
            principal,
            action="catalog.overrides.remove",
            result="success",
            term=payload.get("term"),
            draft_id=str(draft_id),
            status_value=payload.get("state"),
            item_count=len(body.fields),
        )
        return payload
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


#: A raw source payload is retained so an administrator can audit it, but the
#: inspector is a review surface and must not stream an arbitrarily large MCP
#: response into the browser.  Larger payloads come back as a bounded preview.
_OBSERVATION_PAYLOAD_MAX_CHARS = 64_000


def _bounded_json(value: object) -> tuple[object, bool]:
    if value is None:
        return None, False
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) <= _OBSERVATION_PAYLOAD_MAX_CHARS:
        return value, False
    return {"truncated": True, "preview": rendered[:_OBSERVATION_PAYLOAD_MAX_CHARS]}, True


@router.get("/observations/{observation_id}", response_model=CatalogSourceObservationOut)
async def inspect_observation(
    observation_id: UUID,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_read)),
    db: AsyncSession = Depends(get_db),
) -> CatalogSourceObservationOut:
    from app.academic_catalog.models import CatalogSourceObservation

    row = await db.scalar(select(CatalogSourceObservation).where(
        CatalogSourceObservation.id == observation_id,
        CatalogSourceObservation.organization_id == _organization(principal),
    ))
    if row is None:
        raise HTTPException(404, "Source observation not found")
    payload, payload_truncated = _bounded_json(row.payload)
    candidate_data, candidate_truncated = _bounded_json(row.candidate_data)
    return {
        "id": str(row.id), "tool": row.tool, "arguments": row.arguments,
        "payload": payload, "candidate_data": candidate_data,
        "payload_truncated": payload_truncated or candidate_truncated,
        "status": row.status, "observed_at": row.observed_at,
        "source_fetched_at": row.source_fetched_at,
        "parser_version": row.parser_version, "issues": row.issues,
    }


__all__ = ["router"]

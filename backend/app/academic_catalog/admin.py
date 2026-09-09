"""Administrator HTTP API for reviewing and publishing academic catalog data."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.academic_catalog import service
from app.academic_catalog.schemas import DraftCreateIn, DraftPatchIn, ImportIn, PublishIn, RollbackIn, RemoveOverridesIn
from app.admin.auth import AdminPermission, AdminPrincipal, require
from app.admin.directory import METU_ID
from app.db.session import get_db


router = APIRouter(prefix="/catalog")


def _organization(principal: AdminPrincipal) -> UUID:
    # Super administrators may operate on the seeded METU catalog.  A future
    # multi-campus UI can select an explicit organization after the auth layer
    # supplies that scope; ordinary campus administrators stay pinned to their
    # membership organization.
    return principal.organization_id or METU_ID


def _as_error(exc: ValueError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


@router.get("/courses")
async def list_courses(
    term: str = Query(..., min_length=1, max_length=32),
    department: str | None = Query(default=None, max_length=32),
    query: str | None = Query(default=None, max_length=200),
    state: str | None = Query(default=None, max_length=32),
    offset: int = Query(default=0, ge=0, le=100_000),
    limit: int = Query(default=50, ge=1, le=500),
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_read)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
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


@router.get("/courses/{course_code}")
async def course_detail(
    course_code: str,
    term: str = Query(..., min_length=1, max_length=32),
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_read)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await service.course_detail(db, _organization(principal), term, course_code)
    except ValueError as exc:
        raise _as_error(exc) from exc


@router.post("/drafts", status_code=status.HTTP_201_CREATED)
async def create_draft(
    body: DraftCreateIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_write)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
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
        payload = service.serialize_draft(draft)
        await db.commit()
        return payload
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.patch("/drafts/{draft_id}")
async def patch_draft(
    draft_id: UUID,
    body: DraftPatchIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_write)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
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
        payload = service.serialize_draft(draft)
        await db.commit()
        return payload
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.post("/publish")
async def publish(
    body: PublishIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_publish)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
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
        return result
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.post("/rollback")
async def rollback(
    body: RollbackIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_publish)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
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
        return result
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.post("/imports", status_code=status.HTTP_202_ACCEPTED)
async def create_import(
    body: ImportIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_write)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
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
        await db.refresh(job)
        payload = service.serialize_import_job(job)
        await db.commit()
        return payload
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.get("/imports")
async def list_imports(
    term: str | None = Query(default=None, max_length=32),
    job_status: str | None = Query(default=None, alias="status", max_length=32),
    limit: int = Query(default=50, ge=1, le=500),
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_read)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await service.list_import_jobs(db, _organization(principal), term=term, job_status=job_status, limit=limit)


@router.get("/releases")
async def list_releases(
    term: str = Query(..., min_length=1, max_length=32),
    limit: int = Query(default=50, ge=1, le=500),
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_read)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await service.list_catalog_releases(db, _organization(principal), term, limit=limit)


@router.post("/drafts/{draft_id}/remove-overrides")
async def remove_draft_overrides(
    draft_id: UUID,
    body: RemoveOverridesIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_write)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.academic_catalog.overrides import remove_overrides

    try:
        draft = await remove_overrides(
            db, _organization(principal), draft_id,
            expected_revision=body.expected_revision, fields=body.fields,
            reason=body.reason, updated_by=principal.user.id,
        )
        await db.refresh(draft)
        payload = service.serialize_draft(draft)
        await db.commit()
        return payload
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise _as_error(exc) from exc


@router.get("/observations/{observation_id}")
async def inspect_observation(
    observation_id: UUID,
    principal: AdminPrincipal = Depends(require(AdminPermission.catalog_read)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.academic_catalog.models import CatalogSourceObservation

    row = await db.scalar(select(CatalogSourceObservation).where(
        CatalogSourceObservation.id == observation_id,
        CatalogSourceObservation.organization_id == _organization(principal),
    ))
    if row is None:
        raise HTTPException(404, "Source observation not found")
    return {
        "id": str(row.id), "tool": row.tool, "arguments": row.arguments,
        "payload": row.payload, "candidate_data": row.candidate_data,
        "status": row.status, "observed_at": row.observed_at,
        "source_fetched_at": row.source_fetched_at,
        "parser_version": row.parser_version, "issues": row.issues,
    }


__all__ = ["router"]

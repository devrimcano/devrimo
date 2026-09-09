from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.audit import record_event
from app.admin.auth import AdminPermission, AdminPrincipal, require
from app.admin.directory import METU_ID
from app.core.crypto import encrypt_secret
from app.db.models import (
    CampusIngestionJob,
    CampusKnowledgeRecord,
    CampusSource,
    CampusSourceRevision,
    CourseGroupLink,
    CourseOffering,
    CourseRule,
    KnowledgeEmbeddingSettings,
    PlanningPolicy,
)
from app.db.session import get_db
from app.knowledge import registry
from app.knowledge.embeddings import embedding_endpoint_origin, get_embedding_config
from app.knowledge.indexes import (
    IndexNotReady,
    activate_generation,
    active_generation,
    create_generation,
    generation_config,
    list_generations,
    lock_index_publication,
)
from app.knowledge.retrieval import SearchFilters, search_knowledge
from app.knowledge.templates import DEFAULT_SOURCE_TEMPLATES
from app.observability.client import report_exception

router = APIRouter()

SourceKind = Literal[
    "drupal",
    "html_page",
    "html_table",
    "rss",
    "ical",
    "json",
    "pdf",
    "approved_social",
    "email_facts",
    "curated",
]


def _reject_secret_config(value: dict[str, Any]) -> dict[str, Any]:
    blocked = {"password", "secret", "token", "api_key", "authorization", "cookie"}

    def walk(item: Any) -> bool:
        if isinstance(item, dict):
            return any(str(key).lower() in blocked or walk(child) for key, child in item.items())
        if isinstance(item, list):
            return any(walk(child) for child in item)
        return False

    if walk(value):
        raise ValueError("Source configuration cannot contain credentials or tokens")
    return value


class SourceCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=300)
    kind: SourceKind
    url: str | None = Field(default=None, max_length=2000)
    language: Literal["tr", "en"] = "tr"
    authority: int = Field(default=50, ge=0, le=100)
    audience: dict[str, Any] = Field(default_factory=dict)
    schedule_seconds: int = Field(default=3600, ge=300, le=2_592_000)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def no_secrets(cls, value: dict) -> dict:
        return _reject_secret_config(value)


class SourceBatchCreateIn(BaseModel):
    items: list[SourceCreateIn] = Field(min_length=1, max_length=100)


class SourceBatchPublishIn(BaseModel):
    source_ids: list[UUID] = Field(min_length=1, max_length=100)


class RevisionIn(BaseModel):
    config: dict[str, Any]

    @field_validator("config")
    @classmethod
    def no_secrets(cls, value: dict) -> dict:
        return _reject_secret_config(value)


class SourceBulkChanges(BaseModel):
    enabled: bool | None = None
    language: Literal["tr", "en"] | None = None
    authority: int | None = Field(default=None, ge=0, le=100)
    schedule_seconds: int | None = Field(default=None, ge=300, le=2_592_000)


class SourceBulkUpdateIn(BaseModel):
    source_ids: list[UUID] = Field(min_length=1, max_length=200)
    changes: SourceBulkChanges

    @model_validator(mode="after")
    def has_changes(self) -> "SourceBulkUpdateIn":
        if not self.changes.model_dump(exclude_none=True):
            raise ValueError("At least one source field must be changed")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Source IDs must be unique")
        return self


class EmbeddingSettingsIn(BaseModel):
    provider: Literal["disabled", "local", "remote"]
    model: str = Field(min_length=1, max_length=300)
    base_url: str | None = Field(default=None, max_length=2000)
    dimensions: Literal[384, 768, 1536] = 1536
    batch_size: int = Field(default=32, ge=1, le=128)
    # Retrieval models expect a query and the passage answering it to be
    # embedded with different instructions; the wording is provider-specific.
    query_prefix: str = Field(default="", max_length=500)
    document_prefix: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=4000)
    clear_api_key: bool = False

    @model_validator(mode="after")
    def valid_endpoint(self) -> "EmbeddingSettingsIn":
        if self.provider == "disabled":
            return self
        parsed = urlparse(self.base_url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("An HTTP(S) embedding endpoint is required")
        if self.provider == "remote" and parsed.scheme != "https":
            raise ValueError("Remote embedding endpoints must use HTTPS")
        return self


class PolicyIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    rules: dict[str, Any]
    activate: bool = True
    reason: str = Field(min_length=3, max_length=1000)


class OfferingIn(BaseModel):
    term: str = Field(min_length=3, max_length=32)
    course_code: str = Field(min_length=2, max_length=32)
    section: str = Field(default="1", max_length=16)
    title: str = Field(min_length=2, max_length=500)
    credits: float = Field(gt=0, le=30)
    schedule: list[dict] = Field(default_factory=list)
    campus: str | None = None
    department: str | None = None
    source_url: str | None = None


class RuleIn(BaseModel):
    course_code: str = Field(min_length=2, max_length=32)
    prerequisites: dict[str, Any] = Field(default_factory=dict)
    exclusions: list[str] = Field(default_factory=list)
    catalog_url: str | None = None


class AcademicCatalogIn(BaseModel):
    term: str | None = Field(default=None, min_length=3, max_length=32)
    offerings: list[OfferingIn] = Field(default_factory=list, max_length=5000)
    rules: list[RuleIn] = Field(default_factory=list, max_length=5000)
    reason: str = Field(min_length=3, max_length=1000)


class GroupIn(BaseModel):
    course_code: str = Field(min_length=2, max_length=32)
    section: str | None = Field(default=None, max_length=16)
    invite_url: str = Field(min_length=10, max_length=2000)
    eligibility: dict[str, Any] = Field(default_factory=dict)
    valid_until: datetime | None = None


def _org(principal: AdminPrincipal) -> UUID:
    return principal.organization_id or METU_ID


async def _source(db: AsyncSession, principal: AdminPrincipal, source_id: UUID) -> CampusSource:
    source = await db.get(CampusSource, source_id)
    if source is None or source.organization_id != _org(principal):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    return source


def _source_out(
    source: CampusSource,
    revisions: int | None = None,
    records: int | None = None,
    drafts: int | None = None,
) -> dict:
    return {
        "id": str(source.id),
        "name": source.name,
        "kind": source.kind,
        "url": source.url,
        "language": source.language,
        "authority": source.authority,
        "audience": source.audience,
        "schedule_seconds": source.schedule_seconds,
        "enabled": source.enabled,
        "status": source.status,
        "active_revision_id": str(source.active_revision_id) if source.active_revision_id else None,
        "last_fetched_at": source.last_fetched_at,
        "last_success_at": source.last_success_at,
        "last_error": source.last_error,
        "revisions": revisions,
        "draft_revisions": drafts,
        "records": records,
    }


@router.get("/sources")
async def list_sources(
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(
            select(
                CampusSource,
                func.count(func.distinct(CampusSourceRevision.id)),
                func.count(func.distinct(CampusKnowledgeRecord.id)).filter(CampusKnowledgeRecord.is_current.is_(True)),
                func.count(func.distinct(CampusSourceRevision.id)).filter(CampusSourceRevision.published_at.is_(None)),
            )
            .outerjoin(CampusSourceRevision, CampusSourceRevision.source_id == CampusSource.id)
            .outerjoin(CampusKnowledgeRecord, CampusKnowledgeRecord.source_id == CampusSource.id)
            .where(CampusSource.organization_id == _org(principal))
            .group_by(CampusSource.id)
            .order_by(CampusSource.name)
        )
    ).all()
    return {"items": [_source_out(source, revisions, records, drafts) for source, revisions, records, drafts in rows]}


@router.get("/source-templates")
async def source_templates(
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_read)),
) -> dict:
    return {"items": list(DEFAULT_SOURCE_TEMPLATES)}


@router.post("/source-templates/install-defaults", status_code=status.HTTP_201_CREATED)
async def install_source_templates(
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    existing = {
        (item.kind, item.url)
        for item in (
            await db.execute(select(CampusSource).where(CampusSource.organization_id == _org(principal)))
        ).scalars()
    }
    created = []
    for template in DEFAULT_SOURCE_TEMPLATES:
        if (template["kind"], template["url"]) in existing:
            continue
        source, revision = await registry.create_source(
            db,
            organization_id=_org(principal),
            actor_id=principal.user.id,
            **{key: value for key, value in template.items() if key != "id"},
        )
        created.append({"id": str(source.id), "template_id": template["id"], "validation": revision.validation})
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=_org(principal),
        action="knowledge_source.install_templates",
        result="success",
        after={"created": len(created)},
    )
    return {"created": created}


@router.post("/sources", status_code=status.HTTP_201_CREATED)
async def create_source(
    body: SourceCreateIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source, revision = await registry.create_source(
        db,
        organization_id=_org(principal),
        actor_id=principal.user.id,
        **body.model_dump(),
    )
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=_org(principal),
        action="knowledge_source.create",
        result="success",
        after={"source_id": str(source.id), "kind": source.kind, "revision": revision.revision},
    )
    return {**_source_out(source), "revision": revision.revision, "validation": revision.validation}


@router.post("/sources/batch", status_code=status.HTTP_201_CREATED)
async def batch_create_sources(
    body: SourceBatchCreateIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    created = []
    for item in body.items:
        source, revision = await registry.create_source(
            db,
            organization_id=_org(principal),
            actor_id=principal.user.id,
            **item.model_dump(),
        )
        created.append({**_source_out(source), "revision": revision.revision, "validation": revision.validation})
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=_org(principal),
        action="knowledge_source.batch_create",
        result="success",
        after={"created": len(created), "source_ids": [s["id"] for s in created]},
    )
    return {"items": created, "count": len(created)}


@router.post("/sources/batch/publish")
async def batch_publish_sources(
    body: SourceBatchPublishIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    published: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for source_id in body.source_ids:
        try:
            source = await _source(db, principal, source_id)
        except HTTPException as exc:
            failed.append({"source_id": str(source_id), "reason": exc.detail})
            continue

        statement = (
            select(CampusSourceRevision)
            .where(CampusSourceRevision.source_id == source.id)
            .order_by(CampusSourceRevision.revision.desc())
        )
        revisions = (await db.execute(statement)).scalars().all()
        candidate = next((r for r in revisions if r.validation.get("ok")), None)
        if not candidate:
            failed.append(
                {
                    "source_id": str(source.id),
                    "name": source.name,
                    "reason": "No valid revision available to publish",
                }
            )
            continue

        try:
            job = await registry.publish_revision(db, source, candidate)
            published.append(
                {
                    "source_id": str(source.id),
                    "name": source.name,
                    "revision": candidate.revision,
                    "job_id": str(job.id),
                }
            )
        except ValueError as exc:
            failed.append(
                {
                    "source_id": str(source.id),
                    "name": source.name,
                    "reason": str(exc),
                }
            )

    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=_org(principal),
        action="knowledge_source.batch_publish",
        result="success" if published else "failed",
        after={
            "published_count": len(published),
            "failed_count": len(failed),
            "published_source_ids": [p["source_id"] for p in published],
        },
    )

    return {
        "published": published,
        "failed": failed,
        "count": len(published),
    }


@router.put("/sources/bulk")
async def bulk_update_sources(
    body: SourceBulkUpdateIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await lock_index_publication(db, _org(principal))
    sources = (
        (
            await db.execute(
                select(CampusSource).where(
                    CampusSource.organization_id == _org(principal),
                    CampusSource.id.in_(body.source_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(sources) != len(body.source_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "One or more sources were not found")
    changes = body.changes.model_dump(exclude_none=True)
    for source in sources:
        for field, value in changes.items():
            setattr(source, field, value)
    if "authority" in changes:
        await db.execute(
            update(CampusKnowledgeRecord)
            .where(CampusKnowledgeRecord.source_id.in_(body.source_ids))
            .values(authority=changes["authority"])
        )
    await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=_org(principal),
        action="knowledge_source.bulk_update",
        result="success",
        after={"source_ids": [str(item.id) for item in sources], "changes": changes},
    )
    return {"updated": len(sources), "items": [_source_out(item) for item in sources]}


@router.get("/sources/{source_id}")
async def source_detail(
    source_id: UUID,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source = await _source(db, principal, source_id)
    revisions = (
        await db.execute(
            select(CampusSourceRevision)
            .where(CampusSourceRevision.source_id == source.id)
            .order_by(CampusSourceRevision.revision.desc())
        )
    ).scalars()
    return {
        **_source_out(source),
        "revision_history": [
            {
                "id": str(item.id),
                "revision": item.revision,
                "status": item.status,
                "config": item.config,
                "validation": item.validation,
                "created_at": item.created_at,
                "published_at": item.published_at,
            }
            for item in revisions
        ],
    }


@router.post("/sources/{source_id}/revisions", status_code=status.HTTP_201_CREATED)
async def create_revision(
    source_id: UUID,
    body: RevisionIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source = await _source(db, principal, source_id)
    revision = await registry.revise_source(db, source, actor_id=principal.user.id, config=body.config)
    return {"id": str(revision.id), "revision": revision.revision, "validation": revision.validation}


async def _revision(db: AsyncSession, source: CampusSource, revision_id: UUID) -> CampusSourceRevision:
    revision = await db.get(CampusSourceRevision, revision_id)
    if revision is None or revision.source_id != source.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revision not found")
    return revision


@router.post("/sources/{source_id}/revisions/{revision_id}/preview")
async def preview_revision(
    source_id: UUID,
    revision_id: UUID,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source = await _source(db, principal, source_id)
    revision = await _revision(db, source, revision_id)
    try:
        records = await registry.preview_revision(source, revision)
    except Exception as exc:
        # Flattened to a 422 so the admin sees why their parsing settings did
        # not work — but an adapter or a fetch blowing up is our defect, and a
        # 422 is the one status the outcome event will not treat as one.
        report_exception(
            exc,
            distinct_id=str(principal.user.id),
            handler="knowledge_preview_revision",
            source_id=str(source.id),
            source_kind=source.kind,
            revision=revision.revision,
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return {
        "count": len(records),
        "items": [
            {
                "external_id": item.external_id,
                "type": item.record_type,
                "title": item.title,
                "summary": item.summary,
                "url": item.url,
                "starts_at": item.starts_at,
                "ends_at": item.ends_at,
                "audience": item.audience,
            }
            for item in records[:20]
        ],
    }


@router.post("/sources/{source_id}/revisions/{revision_id}/publish")
async def publish_revision(
    source_id: UUID,
    revision_id: UUID,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source = await _source(db, principal, source_id)
    revision = await _revision(db, source, revision_id)
    try:
        job = await registry.publish_revision(db, source, revision)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=_org(principal),
        action="knowledge_source.publish",
        result="success",
        after={"source_id": str(source.id), "revision": revision.revision, "job_id": str(job.id)},
    )
    return {"source_id": str(source.id), "active_revision": revision.revision, "job_id": str(job.id)}


@router.post("/sources/{source_id}/revisions/{revision_id}/rollback")
async def rollback_revision(
    source_id: UUID,
    revision_id: UUID,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source = await _source(db, principal, source_id)
    revision = await _revision(db, source, revision_id)
    try:
        job = await registry.rollback_source(db, source, revision)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"source_id": str(source.id), "active_revision": revision.revision, "job_id": str(job.id)}


@router.post("/sources/{source_id}/ingest")
async def enqueue_ingestion(
    source_id: UUID,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source = await _source(db, principal, source_id)
    if source.active_revision_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Publish a valid revision first")
    job = CampusIngestionJob(source_id=source.id, revision_id=source.active_revision_id)
    db.add(job)
    await db.commit()
    return {"job_id": str(job.id), "status": job.status}


async def _embedding_out(db: AsyncSession, organization_id: UUID) -> dict:
    config = await get_embedding_config(db, organization_id)
    generations = await list_generations(db, organization_id)
    active = next((item for item in generations if item["active"]), None)
    candidate = generations[0] if generations else None
    total_records = int(
        await db.scalar(
            select(func.count(CampusKnowledgeRecord.id))
            .join(CampusSource)
            .where(
                CampusSource.organization_id == organization_id,
                CampusSource.status == "published",
                CampusSource.enabled.is_(True),
                CampusKnowledgeRecord.is_current.is_(True),
            )
        )
        or 0
    )
    embedded_records = active["ready"] if active else 0
    current_model_records = candidate["ready"] if candidate else 0
    active_jobs = sum(
        item["status"] in {"queued", "running", "failed"} and item["ready"] < item["total"] for item in generations
    )
    return {
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "dimensions": config.dimensions,
        "batch_size": config.batch_size,
        "query_prefix": config.query_prefix,
        "document_prefix": config.document_prefix,
        "has_api_key": bool(config.api_key),
        "has_database_override": config.database_override,
        "model_label": config.model_label if config.enabled else None,
        "total_records": total_records,
        "embedded_records": embedded_records,
        "current_model_records": current_model_records,
        "active_jobs": active_jobs,
        "generations": generations,
        "active_generation_id": active["id"] if active else None,
    }


@router.get("/embedding-settings")
async def embedding_settings(
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _embedding_out(db, _org(principal))


@router.get("/knowledge/search")
async def debug_knowledge_search(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=10, ge=1, le=25),
    source_id: UUID | None = None,
    generation_id: UUID | None = None,
    language: Literal["tr", "en"] | None = None,
    record_type: str | None = Query(default=None, max_length=50),
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Run the same ranked retrieval the agent uses, exposed for admins."""
    query = q.strip()
    if not query:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A non-empty search query is required")
    organization_id = _org(principal)
    if generation_id:
        from app.knowledge.index_models import KnowledgeIndexGeneration

        generation = await db.scalar(
            select(KnowledgeIndexGeneration).where(
                KnowledgeIndexGeneration.id == generation_id,
                KnowledgeIndexGeneration.organization_id == organization_id,
            )
        )
        if generation is None:
            raise HTTPException(404, "Index generation not found")
    else:
        generation = await active_generation(db, organization_id)
    config = generation_config(generation) if generation else None
    results = await search_knowledge(
        db,
        query,
        SearchFilters(source_id=source_id, language=language, record_types=(record_type,) if record_type else ()),
        organization_id=organization_id,
        limit=limit,
        generation_id=generation.id if generation else None,
    )
    return {
        "query": query,
        "count": len(results),
        "embedding_model": config.model_label if config and config.enabled else None,
        "items": results,
    }


@router.put("/embedding-settings")
async def update_embedding_settings(
    body: EmbeddingSettingsIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    organization_id = _org(principal)
    row = await db.scalar(
        select(KnowledgeEmbeddingSettings)
        .where(KnowledgeEmbeddingSettings.organization_id == organization_id)
        .with_for_update()
    )
    if row is None:
        row = KnowledgeEmbeddingSettings(organization_id=organization_id)
        db.add(row)

    supplied_key = body.api_key.strip() if body.api_key else None
    retained_key = row.api_key_enc if not body.clear_api_key else None
    if body.provider == "remote" and not (supplied_key or retained_key):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Remote embedding requires an API key")

    new_base_url = body.base_url.strip().rstrip("/") if body.base_url and body.provider != "disabled" else None
    if (
        body.provider == "remote"
        and retained_key is not None
        and supplied_key is None
        and embedding_endpoint_origin(row.base_url) != embedding_endpoint_origin(new_base_url)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Changing the embedding endpoint origin requires a replacement API key",
        )

    row.provider = body.provider
    row.model = body.model.strip()
    row.base_url = new_base_url
    row.dimensions = body.dimensions
    row.batch_size = body.batch_size
    row.query_prefix = body.query_prefix
    row.document_prefix = body.document_prefix
    row.updated_by = principal.user.id
    if supplied_key:
        row.api_key_enc = encrypt_secret(supplied_key)
    elif body.provider != "remote" or body.clear_api_key:
        row.api_key_enc = None
    await db.flush()
    await create_generation(db, organization_id)
    await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=organization_id,
        action="knowledge_embedding.configure",
        result="success",
        after={
            "provider": row.provider,
            "model": row.model,
            "base_url": row.base_url,
            "dimensions": row.dimensions,
            "batch_size": row.batch_size,
            "query_prefix": row.query_prefix,
            "document_prefix": row.document_prefix,
        },
    )
    return await _embedding_out(db, organization_id)


@router.post("/embedding/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex_embeddings(
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    organization_id = _org(principal)
    config = await get_embedding_config(db, organization_id)
    if not config.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Enable a local or remote embedding provider first")
    generation = await create_generation(db, organization_id)
    await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=organization_id,
        action="knowledge_embedding.reindex",
        result="success",
        after={"generation_id": str(generation.id)},
    )
    return {"queued": 1, "job_ids": [str(generation.id)], "generation_id": str(generation.id)}


@router.post("/embedding/generations/{generation_id}/activate")
async def activate_embedding_index(
    generation_id: UUID,
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    organization_id = _org(principal)
    try:
        await activate_generation(db, organization_id, generation_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except IndexNotReady as exc:
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=organization_id,
        action="knowledge_embedding.activate",
        result="success",
        after={"generation_id": str(generation_id)},
    )
    return await _embedding_out(db, organization_id)


@router.get("/ingestion-jobs")
async def ingestion_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    source_id: UUID | None = None,
    offset: int = Query(default=0, ge=0),
    source_name: str = Query(default="", max_length=300),
    job_status: str | None = Query(default=None, max_length=50),
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(
            select(CampusIngestionJob, CampusSource.name)
            .join(CampusSource, CampusSource.id == CampusIngestionJob.source_id)
            .where(CampusSource.organization_id == _org(principal))
            .where(CampusIngestionJob.source_id == source_id if source_id else True)
            .where(func.lower(CampusSource.name).contains(source_name.lower(), autoescape=True))
            .where(CampusIngestionJob.status == job_status if job_status else True)
            .order_by(CampusIngestionJob.created_at.desc(), CampusIngestionJob.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "id": str(job.id),
                "source_id": str(job.source_id),
                "source_name": name,
                "kind": job.kind,
                "status": job.status,
                "phase": job.phase,
                "attempt": job.attempt,
                "total_records": job.total_records,
                "processed_records": job.processed_records,
                "embedded_records": job.embedded_records,
                "embedding_provider": job.embedding_provider,
                "embedding_model": job.embedding_model,
                "error_code": job.error_code,
                "error_detail": job.error_detail,
                "created_at": job.created_at,
                "completed_at": job.completed_at,
                "progress_updated_at": job.progress_updated_at,
            }
            for job, name in rows
        ]
    }


@router.post("/planning/policies", status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: PolicyIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.planning_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org_id = _org(principal)
    revision = int(
        await db.scalar(
            select(func.coalesce(func.max(PlanningPolicy.revision), 0) + 1).where(
                PlanningPolicy.organization_id == org_id, PlanningPolicy.name == body.name
            )
        )
    )
    if body.activate:
        active = (await db.execute(select(PlanningPolicy).where(PlanningPolicy.organization_id == org_id))).scalars()
        for item in active:
            item.active = False
    policy = PlanningPolicy(
        organization_id=org_id,
        name=body.name,
        revision=revision,
        rules=body.rules,
        active=body.activate,
        created_by=principal.user.id,
    )
    db.add(policy)
    await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=org_id,
        action="planning_policy.create",
        result="success",
        reason=body.reason,
        after={"policy_id": str(policy.id), "revision": revision, "active": body.activate},
    )
    return {"id": str(policy.id), "name": policy.name, "revision": revision, "active": policy.active}


@router.put("/planning/catalog")
async def replace_academic_catalog(
    body: AcademicCatalogIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.planning_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.config import get_settings

    if get_settings().academic_catalog_reads_enabled or get_settings().academic_catalog_ingestion_enabled:
        return await _import_reviewed_catalog(body, principal, db)
    offerings_written = 0
    for item in body.offerings:
        code = "".join(item.course_code.upper().split())
        row = (
            await db.execute(
                select(CourseOffering).where(
                    CourseOffering.term == item.term,
                    CourseOffering.course_code == code,
                    CourseOffering.section == item.section,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = CourseOffering(
                term=item.term,
                course_code=code,
                section=item.section,
                title=item.title,
                credits=item.credits,
            )
            db.add(row)
        for key, value in item.model_dump(exclude={"course_code"}).items():
            setattr(row, key, value)
        row.fetched_at = datetime.now(UTC)
        offerings_written += 1
    for item in body.rules:
        code = "".join(item.course_code.upper().split())
        row = await db.get(CourseRule, code)
        if row is None:
            row = CourseRule(course_code=code, revision=1)
            db.add(row)
        else:
            row.revision += 1
        row.prerequisites = item.prerequisites
        row.exclusions = item.exclusions
        row.catalog_url = item.catalog_url
        row.fetched_at = datetime.now(UTC)
    await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=_org(principal),
        action="planning_catalog.import",
        result="success",
        reason=body.reason,
        after={"offerings": offerings_written, "rules": len(body.rules)},
    )
    return {"offerings": offerings_written, "rules": len(body.rules)}


async def _import_reviewed_catalog(body: AcademicCatalogIn, principal: AdminPrincipal, db: AsyncSession) -> dict:
    """Compatibility import: explicit term-scoped drafts, never planner-only writes."""
    from app.academic_catalog import service as catalog_service
    from app.planning.catalog import normalize_sections

    grouped: dict[tuple[str, str], dict] = {}
    days = {day: index for index, day in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))}
    for offering in body.offerings:
        code = "".join(offering.course_code.upper().split())
        row = grouped.setdefault((offering.term, code), {
            "title": offering.title, "local_credits": offering.credits,
            "department": offering.department or code[:3], "sections": [],
        })
        normalized = normalize_sections({"sections": [{"section": offering.section, "schedule": offering.schedule}]})
        meetings = normalized[0]["meetings"] if normalized else []
        row["sections"].append({
            "section_code": offering.section,
            "meetings_status": "unknown",  # Imported source evidence still needs review.
            "meetings": [{"weekday": days[meeting["day"]], "start_minute": meeting["start_minute"],
                          "end_minute": meeting["start_minute"] + meeting["duration_minutes"],
                          "room": meeting["room"], "status": "scheduled"} for meeting in meetings],
        })
    for rule in body.rules:
        code = "".join(rule.course_code.upper().split())
        keys = [key for key in grouped if key[1] == code]
        if not keys:
            if not body.term:
                raise HTTPException(422, "A term is required for rules without accompanying offerings")
            keys = [(body.term, code)]
        for key in keys:
            data = grouped.setdefault(key, {})
            # The legacy arbitrary boolean expression is retained for review.
            # It cannot be silently flattened into different prerequisite logic.
            data["legacy_prerequisites"] = rule.prerequisites
            data["replacements"] = [{"relationship_type": "exclusion", "related_course_code": excluded,
                                      "verified": False} for excluded in rule.exclusions]
            data["component_status"] = {"prerequisites": {"verified": False, "fresh": False,
                                                          "source_status": "legacy_import_needs_review"}}
    drafts = []
    for (term, code), data in grouped.items():
        try:
            draft = await catalog_service.create_draft(db, _org(principal), term, code, data=data,
                                                       reason=body.reason, created_by=principal.user.id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        drafts.append(str(draft.id))
    await db.commit()
    return {"offerings": len(body.offerings), "rules": len(body.rules), "draft_ids": drafts, "published": False}


@router.get("/course-groups")
async def list_groups(
    principal: AdminPrincipal = Depends(require(AdminPermission.knowledge_read)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(
            select(CourseGroupLink)
            .where(CourseGroupLink.organization_id == _org(principal))
            .order_by(CourseGroupLink.course_code, CourseGroupLink.section)
        )
    ).scalars()
    return {
        "items": [
            {
                "id": str(item.id),
                "course_code": item.course_code,
                "section": item.section or None,
                "eligibility": item.eligibility,
                "active": item.active,
                "valid_until": item.valid_until,
                "has_invite_url": True,
            }
            for item in rows
        ]
    }


@router.post("/course-groups", status_code=status.HTTP_201_CREATED)
async def create_group(
    body: GroupIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.groups_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    group = CourseGroupLink(
        organization_id=_org(principal),
        course_code="".join(body.course_code.upper().split()),
        section=body.section or "",
        invite_url_enc=encrypt_secret(body.invite_url),
        eligibility=body.eligibility,
        valid_until=body.valid_until,
        created_by=principal.user.id,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=_org(principal),
        action="course_group.create",
        result="success",
        after={"group_id": str(group.id), "course_code": group.course_code},
    )
    return {"id": str(group.id), "course_code": group.course_code, "section": group.section or None}


class GroupUpdateIn(BaseModel):
    course_code: str = Field(min_length=2, max_length=32)
    section: str | None = Field(default=None, max_length=16)
    invite_url: str | None = Field(default=None, min_length=10, max_length=2000)
    active: bool
    valid_until: datetime | None = None

    @field_validator("course_code")
    @classmethod
    def normalize_course(cls, value: str) -> str:
        value = "".join(value.upper().split())
        if len(value) < 2:
            raise ValueError("A course code is required")
        return value


@router.put("/course-groups/{group_id}")
async def update_group(
    group_id: UUID,
    body: GroupUpdateIn,
    principal: AdminPrincipal = Depends(require(AdminPermission.groups_write)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    group = (
        await db.execute(
            select(CourseGroupLink).where(
                CourseGroupLink.id == group_id,
                CourseGroupLink.organization_id == _org(principal),
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course group not found")
    group.course_code = body.course_code
    group.section = body.section or ""
    group.active = body.active
    group.valid_until = body.valid_until
    if body.invite_url is not None:
        group.invite_url_enc = encrypt_secret(body.invite_url)
    await db.commit()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        organization_id=_org(principal),
        action="course_group.update",
        result="success",
        after={"group_id": str(group.id), "active": group.active},
    )
    return {"id": str(group.id), "active": group.active}

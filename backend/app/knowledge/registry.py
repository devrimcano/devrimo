from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CampusIngestionJob, CampusSource, CampusSourceRevision
from app.knowledge.adapters import adapter_for
from app.knowledge.chunking import chunk_records, validate_chunk_config
from app.knowledge.fetcher import FetchPolicy, fetch_document
from app.knowledge.types import ParsedRecord

REMOTE_KINDS = {"drupal", "html_page", "html_table", "rss", "ical", "json", "pdf", "approved_social"}


def validate_source(source: CampusSource, config: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        adapter_for(source.kind)
    except ValueError as exc:
        errors.append(str(exc))
    if source.kind in REMOTE_KINDS:
        parsed = urlparse(source.url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            errors.append("A valid HTTP(S) URL is required for this adapter")
    elif source.url:
        warnings.append("This adapter ignores its URL and uses curated records")
    if not 0 <= source.authority <= 100:
        errors.append("authority must be between 0 and 100")
    if source.schedule_seconds < 300:
        errors.append("schedule_seconds must be at least 300")
    if source.kind in {"curated", "email_facts"} and not isinstance(config.get("records", []), list):
        errors.append("config.records must be a list")
    errors.extend(validate_chunk_config(config))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


async def create_source(
    db: AsyncSession,
    *,
    organization_id: UUID,
    actor_id: UUID,
    name: str,
    kind: str,
    url: str | None,
    language: str,
    authority: int,
    audience: dict,
    schedule_seconds: int,
    config: dict,
) -> tuple[CampusSource, CampusSourceRevision]:
    source = CampusSource(
        organization_id=organization_id,
        created_by=actor_id,
        name=name.strip(),
        kind=kind,
        url=url.strip() if url else None,
        language=language,
        authority=authority,
        audience=audience,
        schedule_seconds=schedule_seconds,
    )
    db.add(source)
    await db.flush()
    validation = validate_source(source, config)
    revision = CampusSourceRevision(
        source_id=source.id,
        revision=1,
        status="valid" if validation["ok"] else "draft",
        config=config,
        validation=validation,
        created_by=actor_id,
    )
    db.add(revision)
    await db.commit()
    await db.refresh(source)
    await db.refresh(revision)
    return source, revision


async def revise_source(
    db: AsyncSession, source: CampusSource, *, actor_id: UUID, config: dict
) -> CampusSourceRevision:
    next_revision = (
        await db.scalar(
            select(func.coalesce(func.max(CampusSourceRevision.revision), 0) + 1).where(
                CampusSourceRevision.source_id == source.id
            )
        )
    )
    validation = validate_source(source, config)
    revision = CampusSourceRevision(
        source_id=source.id,
        revision=next_revision,
        status="valid" if validation["ok"] else "draft",
        config=config,
        validation=validation,
        created_by=actor_id,
    )
    db.add(revision)
    await db.commit()
    await db.refresh(revision)
    return revision


async def preview_revision(source: CampusSource, revision: CampusSourceRevision) -> list[ParsedRecord]:
    validation = validate_source(source, revision.config)
    if not validation["ok"]:
        raise ValueError("; ".join(validation["errors"]))
    document = None
    if source.kind in REMOTE_KINDS:
        host = urlparse(source.url or "").hostname or ""
        extra_hosts = {str(value).lower() for value in revision.config.get("allowed_hosts", [])}
        document = await fetch_document(
            source.url or "",
            FetchPolicy(
                allowed_hosts=frozenset({host.lower(), *extra_hosts}),
                respect_robots=revision.config.get("respect_robots", True),
            ),
        )
    config = {
        **revision.config,
        "defaults": {
            "language": source.language,
            "audience": source.audience,
            **revision.config.get("defaults", {}),
        },
    }
    return chunk_records(adapter_for(source.kind).parse(document, config), config)


async def publish_revision(
    db: AsyncSession, source: CampusSource, revision: CampusSourceRevision, *, enabled: bool = True
) -> CampusIngestionJob:
    if revision.source_id != source.id or not revision.validation.get("ok"):
        raise ValueError("Only a valid revision for this source can be published")
    await db.refresh(source, with_for_update=True)
    now = datetime.now(UTC)
    old = (
        await db.execute(
            select(CampusSourceRevision).where(
                CampusSourceRevision.source_id == source.id,
                CampusSourceRevision.status == "published",
            )
        )
    ).scalars()
    for item in old:
        if item.id != revision.id:
            item.status = "valid"
    revision.status = "published"
    revision.published_at = now
    source.active_revision_id = revision.id
    source.status = "published"
    source.enabled = enabled
    source.last_error = None
    # HTTP validators describe the page bytes, not the parser configuration.
    # A new revision (including rollback) must parse the page again.
    source.etag = None
    source.last_modified = None
    job = CampusIngestionJob(source_id=source.id, revision_id=revision.id)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def rollback_source(db: AsyncSession, source: CampusSource, revision: CampusSourceRevision) -> CampusIngestionJob:
    if revision.source_id != source.id or not revision.validation.get("ok"):
        raise ValueError("Revision is not a valid rollback target")
    return await publish_revision(db, source, revision, enabled=source.enabled)

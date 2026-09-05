import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.db.models import CampusIngestionJob, CampusKnowledgeRecord, CampusSource, CampusSourceRevision
from app.knowledge.adapters import adapter_for
from app.knowledge.chunking import chunk_records, embedding_text
from app.knowledge.embeddings import EmbeddingConfig, assign_embedding, embed_texts, get_embedding_config
from app.knowledge.fetcher import FetchPolicy, fetch_document
from app.knowledge.registry import REMOTE_KINDS
from app.knowledge.types import ParsedRecord


class JobLeaseLost(RuntimeError):
    """This attempt may no longer change the job or its source records."""


class SourceRevisionChanged(ValueError):
    """The job's revision has been superseded and must not be retried."""


@dataclass(frozen=True)
class JobLease:
    job_id: UUID
    owner: str
    attempt: int
    # Carried so a worker can label its telemetry with the source it is
    # ingesting without re-reading the job row it no longer holds.
    source_id: UUID
    kind: str

    @classmethod
    def from_job(cls, job: CampusIngestionJob) -> "JobLease":
        if job.status != "leased" or not job.lease_owner:
            raise JobLeaseLost("Knowledge job must be claimed before processing")
        return cls(job.id, job.lease_owner, job.attempt, job.source_id, job.kind)


def _lease_conditions(lease: JobLease) -> tuple:
    return (
        CampusIngestionJob.id == lease.job_id,
        CampusIngestionJob.status == "leased",
        CampusIngestionJob.lease_owner == lease.owner,
        CampusIngestionJob.attempt == lease.attempt,
        CampusIngestionJob.leased_until > datetime.now(UTC),
    )


async def _lock_lease(db: AsyncSession, lease: JobLease) -> None:
    # Never autoflush progress or records from an attempt that lost its lease.
    with db.no_autoflush:
        owned = await db.scalar(select(CampusIngestionJob.id).where(*_lease_conditions(lease)).with_for_update())
    if owned is None:
        raise JobLeaseLost("Knowledge job lease expired or belongs to another attempt")


async def renew_lease(db: AsyncSession, lease: JobLease) -> None:
    # Serialize renewal with claiming another job for this source. Otherwise a
    # renewal committing at expiry could race the claimant's active-lease check.
    await db.scalar(
        select(CampusSource.id)
        .join(CampusIngestionJob, CampusIngestionJob.source_id == CampusSource.id)
        .where(CampusIngestionJob.id == lease.job_id)
        .with_for_update(of=CampusSource)
    )
    renewed = await db.scalar(
        update(CampusIngestionJob)
        .where(*_lease_conditions(lease))
        .values(leased_until=datetime.now(UTC) + timedelta(seconds=get_settings().knowledge_worker_lease_seconds))
        .returning(CampusIngestionJob.id)
    )
    await db.commit()
    if renewed is None:
        raise JobLeaseLost("Knowledge job lease expired or belongs to another attempt")


async def _lock_source_for_store(db: AsyncSession, job: CampusIngestionJob, lease: JobLease) -> CampusSource:
    # Source first, then job: claim, publication and failure use the same order.
    # These locks cover only database writes, never fetching or embedding I/O.
    source = await db.scalar(
        select(CampusSource)
        .where(CampusSource.id == job.source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    await _lock_lease(db, lease)
    if source is None or source.active_revision_id != job.revision_id:
        raise SourceRevisionChanged("Job points to a missing or inactive source revision")
    return source


def _complete_job(job: CampusIngestionJob, now: datetime) -> None:
    job.status = "completed"
    job.phase = "completed"
    job.completed_at = now
    job.progress_updated_at = now
    job.error_code = None
    job.error_detail = None
    job.lease_owner = None
    job.leased_until = None


def _content_hash(record: ParsedRecord) -> str:
    payload = {
        "type": record.record_type,
        "title": record.title,
        "summary": record.summary,
        "content": record.content,
        "url": record.url,
        "language": record.language,
        "campus": record.campus,
        "department": record.department,
        "degree_level": record.degree_level,
        "audience": record.audience,
        "starts_at": record.starts_at.isoformat() if record.starts_at else None,
        "ends_at": record.ends_at.isoformat() if record.ends_at else None,
        "published_at": record.published_at.isoformat() if record.published_at else None,
        "valid_until": record.valid_until.isoformat() if record.valid_until else None,
        "metadata": record.metadata,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def enqueue_due_sources(db: AsyncSession) -> int:
    now = datetime.now(UTC)
    sources = (
        await db.execute(select(CampusSource).where(CampusSource.enabled.is_(True), CampusSource.status == "published"))
    ).scalars()
    queued = 0
    for source in sources:
        due = source.last_fetched_at is None or source.last_fetched_at <= now - timedelta(
            seconds=source.schedule_seconds
        )
        if not due or source.active_revision_id is None:
            continue
        active_job = await db.scalar(
            select(CampusIngestionJob.id).where(
                CampusIngestionJob.source_id == source.id,
                CampusIngestionJob.status.in_(["queued", "leased", "failed"]),
            )
        )
        if active_job is None:
            db.add(CampusIngestionJob(source_id=source.id, revision_id=source.active_revision_id))
            queued += 1
    if queued:
        await db.commit()
    return queued


async def claim_job(db: AsyncSession, worker_id: str) -> CampusIngestionJob | None:
    now = datetime.now(UTC)
    running = aliased(CampusIngestionJob)
    statement = (
        select(CampusIngestionJob)
        .join(CampusSource, CampusSource.id == CampusIngestionJob.source_id)
        .where(
            or_(
                CampusIngestionJob.status == "queued",
                (CampusIngestionJob.status == "leased") & (CampusIngestionJob.leased_until < now),
                (CampusIngestionJob.status == "failed") & (CampusIngestionJob.available_at <= now),
            ),
            CampusIngestionJob.available_at <= now,
            ~select(running.id)
            .where(
                running.source_id == CampusIngestionJob.source_id,
                running.status == "leased",
                running.leased_until > now,
            )
            .exists(),
        )
        .order_by(CampusIngestionJob.available_at, CampusIngestionJob.created_at)
        .limit(1)
        .with_for_update(of=CampusSource, skip_locked=True)
    )
    job = (await db.execute(statement)).scalar_one_or_none()
    if job is None:
        await db.commit()
        return None
    # Recheck using a fresh READ COMMITTED snapshot after taking the source lock.
    active = await db.scalar(
        select(CampusIngestionJob.id).where(
            CampusIngestionJob.source_id == job.source_id,
            CampusIngestionJob.status == "leased",
            CampusIngestionJob.leased_until > datetime.now(UTC),
        )
    )
    if active is not None:
        await db.commit()
        return None
    await db.refresh(job, with_for_update=True)
    now = datetime.now(UTC)
    settings = get_settings()
    job.status = "leased"
    job.phase = "queued"
    job.attempt += 1
    job.lease_owner = worker_id
    job.leased_until = now + timedelta(seconds=settings.knowledge_worker_lease_seconds)
    job.started_at = job.started_at or now
    await db.commit()
    await db.refresh(job)
    return job


async def _set_progress(db: AsyncSession, job: CampusIngestionJob, phase: str, *, lease: JobLease) -> None:
    await _lock_lease(db, lease)
    job.phase = phase
    job.progress_updated_at = datetime.now(UTC)
    await db.commit()


async def _embed_batches(
    db: AsyncSession,
    job: CampusIngestionJob,
    source: CampusSource,
    texts: list[str],
    lease: JobLease,
) -> tuple[list[list[float] | None], EmbeddingConfig]:
    config = await get_embedding_config(db, source.organization_id)
    job.embedding_provider = config.provider
    job.embedding_model = config.model_label if config.enabled else None
    job.total_records = len(texts)
    job.processed_records = 0
    job.embedded_records = 0
    await _set_progress(db, job, "embedding", lease=lease)
    if not texts:
        return [], config
    if not config.enabled:
        job.processed_records = len(texts)
        await _set_progress(db, job, "embedding", lease=lease)
        return [None for _ in texts], config

    result: list[list[float] | None] = []
    for offset in range(0, len(texts), config.batch_size):
        batch = texts[offset : offset + config.batch_size]
        vectors = await embed_texts(db, source.organization_id, batch, config=config)
        result.extend(vectors)
        job.processed_records += len(batch)
        job.embedded_records += sum(vector is not None for vector in vectors)
        await _set_progress(db, job, "embedding", lease=lease)
    return result, config


async def _load_records(source: CampusSource, revision: CampusSourceRevision) -> tuple[list[ParsedRecord] | None, dict]:
    document = None
    headers: dict = {}
    if source.kind in REMOTE_KINDS:
        host = urlparse(source.url or "").hostname or ""
        allowed_hosts = {host.lower(), *(str(item).lower() for item in revision.config.get("allowed_hosts", []))}
        document = await fetch_document(
            source.url or "",
            FetchPolicy(
                allowed_hosts=frozenset(allowed_hosts),
                respect_robots=revision.config.get("respect_robots", True),
            ),
            etag=source.etag,
            last_modified=source.last_modified,
        )
        headers = {"etag": document.etag, "last_modified": document.last_modified}
        if document.not_modified:
            return None, headers
    config = {
        **revision.config,
        "defaults": {
            "language": source.language,
            "audience": source.audience,
            **revision.config.get("defaults", {}),
        },
    }
    parsed = adapter_for(source.kind).parse(document, config)
    # Pages can repeat the same item (for example, a Drupal views-row and its
    # nested article). Keep the last occurrence, matching updates to existing
    # rows, before chunking so differing copies cannot leave mixed chunk sets.
    unique_records = {record.external_id: record for record in parsed}
    records = chunk_records(list(unique_records.values()), config)
    if not records and not revision.config.get("allow_empty", False):
        raise ValueError("Adapter returned no records; existing records were preserved")
    return records, headers


async def process_job(db: AsyncSession, job: CampusIngestionJob, *, lease: JobLease | None = None) -> int:
    lease = lease or JobLease.from_job(job)
    await _lock_lease(db, lease)
    source = await db.get(CampusSource, job.source_id)
    revision = await db.get(CampusSourceRevision, job.revision_id)
    if source is None or revision is None or source.active_revision_id != revision.id:
        raise SourceRevisionChanged("Job points to a missing or inactive source revision")
    if job.kind == "reembed":
        rows = (
            (
                await db.execute(
                    select(CampusKnowledgeRecord).where(
                        CampusKnowledgeRecord.source_id == source.id,
                        CampusKnowledgeRecord.is_current.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        texts = [
            embedding_text(
                title=row.title,
                summary=row.summary,
                content=row.content,
                metadata=row.metadata_json,
            )
            for row in rows
        ]
        embeddings, config = await _embed_batches(db, job, source, texts, lease)
        await _set_progress(db, job, "storing", lease=lease)
        await _lock_source_for_store(db, job, lease)
        for row, embedding in zip(rows, embeddings, strict=True):
            assign_embedding(row, embedding, config.dimensions)
            row.embedding_model = config.model_label if embedding is not None else None
        now = datetime.now(UTC)
        _complete_job(job, now)
        await db.commit()
        return len(rows)

    # The source/revision reads above opened a transaction. Release it before
    # DNS, HTTP, parsing, and embedding I/O so the pool connection is not held
    # for the duration of a remote ingestion.
    await db.commit()
    await _set_progress(db, job, "fetching" if source.kind in REMOTE_KINDS else "parsing", lease=lease)
    records, headers = await _load_records(source, revision)
    if records is None:
        source = await _lock_source_for_store(db, job, lease)
        now = datetime.now(UTC)
        source.last_fetched_at = now
        source.last_success_at = now
        source.last_error = None
        _complete_job(job, now)
        await db.commit()
        return 0

    texts = [
        embedding_text(
            title=record.title,
            summary=record.summary,
            content=record.content,
            metadata=record.metadata,
        )
        for record in records
    ]
    embeddings, config = await _embed_batches(db, job, source, texts, lease)
    await _set_progress(db, job, "storing", lease=lease)
    source = await _lock_source_for_store(db, job, lease)
    now = datetime.now(UTC)
    existing = {
        row.external_id: row
        for row in (
            await db.execute(select(CampusKnowledgeRecord).where(CampusKnowledgeRecord.source_id == source.id))
        ).scalars()
    }
    seen: set[str] = set()
    for record, embedding in zip(records, embeddings, strict=True):
        seen.add(record.external_id)
        content_hash = _content_hash(record)
        row = existing.get(record.external_id)
        if row is None:
            row = CampusKnowledgeRecord(
                source_id=source.id,
                source_revision_id=revision.id,
                external_id=record.external_id,
                record_type=record.record_type,
                title=record.title,
                content=record.content,
                content_hash=content_hash,
            )
            db.add(row)
            existing[record.external_id] = row
        row.source_revision_id = revision.id
        row.record_type = record.record_type
        row.title = record.title
        row.summary = record.summary
        row.content = record.content
        row.url = record.url
        row.language = record.language
        row.campus = record.campus
        row.department = record.department
        row.degree_level = record.degree_level
        row.audience = record.audience
        row.starts_at = record.starts_at
        row.ends_at = record.ends_at
        row.published_at = record.published_at
        row.valid_until = record.valid_until
        row.authority = source.authority
        row.content_hash = content_hash
        row.metadata_json = record.metadata
        assign_embedding(row, embedding, config.dimensions)
        row.embedding_model = config.model_label if embedding is not None else None
        row.is_current = True
        row.last_seen_at = now
        row.removed_at = None
    for external_id, row in existing.items():
        if external_id not in seen and row.is_current:
            row.is_current = False
            row.removed_at = now
    source.etag = headers.get("etag")
    source.last_modified = headers.get("last_modified")
    source.last_fetched_at = now
    source.last_success_at = now
    source.last_error = None
    _complete_job(job, now)
    await db.commit()
    return len(records)


async def fail_job(db: AsyncSession, lease: JobLease, exc: Exception) -> str | None:
    """Record the failure and decide whether the job retries or is dead.

    Returns the resulting job status so the caller can report the difference:
    "this will be retried in 30 seconds" and "this source will never be
    ingested again" are the same log line today and very different problems.
    """
    await db.rollback()
    job = await db.get(CampusIngestionJob, lease.job_id)
    if job is None:
        return None
    source = await db.scalar(
        select(CampusSource)
        .where(CampusSource.id == job.source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    try:
        await _lock_lease(db, lease)
    except JobLeaseLost:
        await db.rollback()
        return None
    now = datetime.now(UTC)
    superseded = source is None or source.active_revision_id != job.revision_id
    dead = superseded or isinstance(exc, SourceRevisionChanged) or job.attempt >= job.max_attempts
    job.status = "dead" if dead else "failed"
    job.phase = "failed"
    job.progress_updated_at = now
    job.available_at = now + timedelta(seconds=min(3600, 2 ** max(job.attempt, 1) * 30))
    job.error_code = type(exc).__name__[:128]
    job.error_detail = str(exc)[:2000]
    job.leased_until = None
    job.lease_owner = None
    if source is not None and not superseded:
        source.last_fetched_at = now
        source.last_error = str(exc)[:2000]
    await db.commit()
    return job.status

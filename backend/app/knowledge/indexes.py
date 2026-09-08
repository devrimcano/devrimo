"""Generation publication and fenced, incremental semantic indexing."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.db.models import CampusKnowledgeRecord, CampusSource
from app.knowledge.chunking import embedding_text
from app.knowledge.embeddings import EmbeddingConfig, embed_texts, get_embedding_config
from app.knowledge.index_models import (
    KnowledgeIndexActivation,
    KnowledgeIndexGeneration,
    KnowledgeIndexJob,
    KnowledgeIndexVector,
)


class IndexNotReady(ValueError):
    pass


class IndexLeaseLost(RuntimeError):
    pass


def generation_config(row: KnowledgeIndexGeneration) -> EmbeddingConfig:
    return EmbeddingConfig(
        provider=row.provider,
        model=row.model,
        base_url=row.base_url,
        dimensions=row.dimensions,
        batch_size=row.batch_size,
        api_key=decrypt_secret(row.api_key_enc) if row.api_key_enc else None,
        query_prefix=row.query_prefix,
        document_prefix=row.document_prefix,
        database_override=True,
    )


async def active_generation(db: AsyncSession, organization_id: UUID) -> KnowledgeIndexGeneration | None:
    return await db.scalar(
        select(KnowledgeIndexGeneration)
        .join(KnowledgeIndexActivation, KnowledgeIndexActivation.generation_id == KnowledgeIndexGeneration.id)
        .where(KnowledgeIndexActivation.organization_id == organization_id)
    )


async def lock_index_publication(db: AsyncSession, organization_id: UUID) -> None:
    # Both source storage and activation take this transaction lock. The
    # coverage check and active-pointer change therefore see one corpus.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"knowledge-index:{organization_id}"},
    )


def _records(organization_id: UUID):
    return (
        select(CampusKnowledgeRecord)
        .join(CampusSource, CampusSource.id == CampusKnowledgeRecord.source_id)
        .where(
            CampusSource.organization_id == organization_id,
            CampusSource.status == "published",
            CampusSource.enabled.is_(True),
            CampusKnowledgeRecord.is_current.is_(True),
        )
    )


def vector_column(dimensions: int):
    if dimensions not in (384, 768, 1536):
        raise ValueError("Unsupported embedding dimension")
    return getattr(KnowledgeIndexVector, f"embedding_{dimensions}")


async def coverage(db: AsyncSession, generation: KnowledgeIndexGeneration) -> tuple[int, int]:
    records = _records(generation.organization_id).subquery()
    total = await db.scalar(select(func.count()).select_from(records)) or 0
    if generation.provider == "disabled":
        return total, total
    ready = (
        await db.scalar(
            select(func.count())
            .select_from(records)
            .join(
                KnowledgeIndexVector,
                (KnowledgeIndexVector.record_id == records.c.id)
                & (KnowledgeIndexVector.generation_id == generation.id)
                & (KnowledgeIndexVector.content_hash == records.c.content_hash),
            )
            .where(vector_column(generation.dimensions).is_not(None))
        )
        or 0
    )
    return total, ready


async def create_generation(db: AsyncSession, organization_id: UUID) -> KnowledgeIndexGeneration:
    """Admin application command; workers never create configuration rows."""
    config = await get_embedding_config(db, organization_id)
    row = KnowledgeIndexGeneration(
        organization_id=organization_id,
        provider=config.provider,
        model=config.model,
        base_url=config.base_url,
        dimensions=config.dimensions,
        batch_size=config.batch_size,
        api_key_enc=encrypt_secret(config.api_key) if config.api_key else None,
        query_prefix=config.query_prefix,
        document_prefix=config.document_prefix,
        model_label=config.model_label,
    )
    db.add(row)
    await db.flush()
    # Index jobs are discoverable from generations. The worker owns their
    # creation, so this transaction has no cross-owner job-table write.
    return row


async def activate_generation(db: AsyncSession, organization_id: UUID, generation_id: UUID) -> None:
    await lock_index_publication(db, organization_id)
    row = await db.get(KnowledgeIndexGeneration, generation_id)
    if row is None or row.organization_id != organization_id:
        raise LookupError("Index generation not found")
    total, ready = await coverage(db, row)
    if total != ready:
        raise IndexNotReady(f"Index coverage is {ready}/{total}; finish indexing before activation")
    await db.execute(
        insert(KnowledgeIndexActivation)
        .values(
            organization_id=organization_id,
            generation_id=row.id,
            activated_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=[KnowledgeIndexActivation.organization_id],
            set_={"generation_id": row.id, "activated_at": datetime.now(UTC)},
        )
    )


async def list_generations(db: AsyncSession, organization_id: UUID) -> list[dict]:
    active = await active_generation(db, organization_id)
    rows = (
        await db.scalars(
            select(KnowledgeIndexGeneration)
            .where(KnowledgeIndexGeneration.organization_id == organization_id)
            .order_by(KnowledgeIndexGeneration.created_at.desc())
        )
    ).all()
    result = []
    for row in rows:
        total, ready = await coverage(db, row)
        job = await db.get(KnowledgeIndexJob, row.id)
        result.append(
            {
                "id": str(row.id),
                "model": row.model,
                "model_label": row.model_label,
                "provider": row.provider,
                "dimensions": row.dimensions,
                "active": active is not None and active.id == row.id,
                "total": total,
                "ready": ready,
                "can_activate": total == ready,
                "status": job.status if job else "queued",
                "error": job.last_error if job else None,
                "created_at": row.created_at.isoformat(),
            }
        )
    return result


def input_fingerprint(config: EmbeddingConfig, passage: str) -> str:
    # Hash the actual provider input and its embedding space, including the
    # endpoint. Retrieval-only metadata changes can reuse the same vector.
    value = json.dumps(
        [config.provider, config.base_url, config.model, config.dimensions, config.document_prefix + passage],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


async def claim_index_job(db: AsyncSession, owner: str, *, lease_seconds: int = 180) -> tuple[UUID, int] | None:
    now = datetime.now(UTC)
    generations = (
        await db.scalars(
            select(KnowledgeIndexGeneration)
            .where(
                or_(
                    KnowledgeIndexGeneration.id.in_(select(KnowledgeIndexActivation.generation_id)),
                    KnowledgeIndexGeneration.id.in_(
                        select(KnowledgeIndexGeneration.id)
                        .distinct(KnowledgeIndexGeneration.organization_id)
                        .order_by(KnowledgeIndexGeneration.organization_id, KnowledgeIndexGeneration.created_at.desc())
                    ),
                )
            )
            .order_by(KnowledgeIndexGeneration.created_at)
        )
    ).all()
    for generation in generations:
        if generation.provider == "disabled":
            continue
        total, ready = await coverage(db, generation)
        if total == ready:
            continue
        await db.execute(insert(KnowledgeIndexJob).values(generation_id=generation.id).on_conflict_do_nothing())
        job = await db.scalar(
            select(KnowledgeIndexJob)
            .where(
                KnowledgeIndexJob.generation_id == generation.id,
                or_(KnowledgeIndexJob.leased_until.is_(None), KnowledgeIndexJob.leased_until <= now),
                # Backoff after a provider failure; active jobs can be renewed.
                or_(KnowledgeIndexJob.status != "failed", KnowledgeIndexJob.updated_at < now - timedelta(seconds=30)),
            )
            .with_for_update(skip_locked=True)
        )
        if job is None:
            continue
        job.status, job.lease_owner = "running", owner
        job.leased_until = now + timedelta(seconds=lease_seconds)
        job.attempt += 1
        job.total, job.completed, job.last_error = total, ready, None
        job.updated_at = now
        await db.commit()
        return generation.id, job.attempt
    await db.commit()
    return None


def _lease(generation_id: UUID, owner: str, attempt: int):
    return (
        KnowledgeIndexJob.generation_id == generation_id,
        KnowledgeIndexJob.lease_owner == owner,
        KnowledgeIndexJob.attempt == attempt,
        KnowledgeIndexJob.status == "running",
        KnowledgeIndexJob.leased_until > datetime.now(UTC),
    )


async def renew_index_lease(db: AsyncSession, generation_id: UUID, owner: str, attempt: int) -> None:
    found = await db.scalar(
        update(KnowledgeIndexJob)
        .where(*_lease(generation_id, owner, attempt))
        .values(
            leased_until=datetime.now(UTC) + timedelta(seconds=180),
            updated_at=datetime.now(UTC),
        )
        .returning(KnowledgeIndexJob.generation_id)
    )
    await db.commit()
    if found is None:
        raise IndexLeaseLost("Embedding job lease lost")


async def process_index_batch(db: AsyncSession, generation_id: UUID, owner: str, attempt: int) -> bool:
    """One bounded batch. Network work holds no database transaction."""
    generation = await db.get(KnowledgeIndexGeneration, generation_id)
    if generation is None:
        raise IndexLeaseLost("Generation removed")
    config = generation_config(generation)
    vectors = KnowledgeIndexVector
    rows = (
        await db.scalars(
            _records(generation.organization_id)
            .outerjoin(
                vectors,
                (vectors.record_id == CampusKnowledgeRecord.id) & (vectors.generation_id == generation_id),
            )
            .where(
                or_(
                    vectors.record_id.is_(None),
                    vectors.content_hash != CampusKnowledgeRecord.content_hash,
                    vector_column(config.dimensions).is_(None),
                )
            )
            .order_by(CampusKnowledgeRecord.id)
            .limit(config.batch_size)
        )
    ).all()
    pending = []
    for row in rows:
        passage = embedding_text(title=row.title, summary=row.summary, content=row.content, metadata=row.metadata_json)
        pending.append((row.id, row.content_hash, input_fingerprint(config, passage), passage))
    # Reuse exact embedding inputs across generations or metadata-only changes.
    reusable = {}
    if pending:
        found = (
            await db.scalars(
                select(vectors).where(
                    vectors.input_hash.in_([item[2] for item in pending]),
                    vector_column(config.dimensions).is_not(None),
                )
            )
        ).all()
        reusable = {row.input_hash: list(getattr(row, f"embedding_{config.dimensions}")) for row in found}
    await db.commit()
    missing = {item[2]: item[3] for item in pending if item[2] not in reusable}
    if missing:
        embedded = await embed_texts(db, generation.organization_id, list(missing.values()), config=config)
        reusable.update(zip(missing, embedded, strict=True))
    with db.no_autoflush:
        job = await db.scalar(select(KnowledgeIndexJob).where(*_lease(generation_id, owner, attempt)).with_for_update())
    if job is None:
        await db.rollback()
        raise IndexLeaseLost("Embedding result belongs to an expired attempt")
    for record_id, content_hash, fingerprint, _ in pending:
        vector = reusable[fingerprint]
        if vector is None:
            raise ValueError("Enabled embedding provider returned no vector")
        values = {
            "generation_id": generation_id,
            "record_id": record_id,
            "content_hash": content_hash,
            "input_hash": fingerprint,
            "embedding_384": vector if config.dimensions == 384 else None,
            "embedding_768": vector if config.dimensions == 768 else None,
            "embedding_1536": vector if config.dimensions == 1536 else None,
            "created_at": datetime.now(UTC),
        }
        # A concurrently edited record can keep this stale vector: retrieval
        # checks its content hash, and the next batch immediately replaces it.
        await db.execute(
            insert(vectors)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[vectors.generation_id, vectors.record_id],
                set_=values,
            )
        )
    total, ready = await coverage(db, generation)
    job.total, job.completed, job.updated_at = total, ready, datetime.now(UTC)
    done = total == ready
    if done:
        job.status, job.lease_owner, job.leased_until = "completed", None, None
    await db.commit()
    return done


async def fail_index_job(db: AsyncSession, generation_id: UUID, owner: str, attempt: int, exc: Exception) -> None:
    await db.rollback()
    await db.execute(
        update(KnowledgeIndexJob)
        .where(*_lease(generation_id, owner, attempt))
        .values(
            status="failed",
            lease_owner=None,
            leased_until=None,
            updated_at=datetime.now(UTC),
            # Do not persist provider URLs or response bodies that may contain keys.
            last_error=type(exc).__name__,
        )
    )
    await db.commit()

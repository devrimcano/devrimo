import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.api.v1.knowledge_admin import reindex_embeddings
from app.config import get_settings
from app.db.models import CampusIngestionJob, CampusKnowledgeRecord, CampusSource, CampusSourceRevision
from app.db.session import SessionLocal
from app.knowledge.embeddings import EmbeddingConfig
from app.knowledge.ingestion import (
    JobLease,
    JobLeaseLost,
    SourceRevisionChanged,
    _set_progress,
    claim_job,
    fail_job,
    process_job,
    renew_lease,
)
from app.knowledge.registry import publish_revision
from app.knowledge.types import FetchedDocument
from app.knowledge.worker import run_leased_job
from tests.test_knowledge_ingestion import _source_and_job

MENU = {"records": [{"external_id": "menu", "title": "Lunch", "content": "Soup"}]}
QWEN = EmbeddingConfig("remote", "qwen/qwen3-embedding-8b", None, 1536, 32)


@pytest.fixture
def embeddings(monkeypatch):
    monkeypatch.setattr("app.knowledge.ingestion.get_embedding_config", AsyncMock(return_value=QWEN))
    monkeypatch.setattr("app.api.v1.knowledge_admin.get_embedding_config", AsyncMock(return_value=QWEN))
    monkeypatch.setattr(
        "app.knowledge.ingestion.embed_texts",
        AsyncMock(side_effect=lambda _db, _org, texts, **kw: [[1.0] + [0.0] * 1535 for _ in texts]),
    )


async def test_claims_serialize_a_source_and_allow_other_sources(embeddings):
    async with SessionLocal() as db:
        source, revision, first = await _source_and_job(db, kind="curated", config=MENU)
        next_job = CampusIngestionJob(source_id=source.id, revision_id=revision.id)
        other = CampusSource(organization_id=source.organization_id, name="Other", kind="curated")
        db.add_all([next_job, other])
        await db.flush()
        other_revision = CampusSourceRevision(source_id=other.id, revision=1, config=MENU)
        db.add(other_revision)
        await db.flush()
        other.active_revision_id = other_revision.id
        db.add(CampusIngestionJob(source_id=other.id, revision_id=other_revision.id))
        await db.commit()
        async with SessionLocal() as second:
            claimed = await claim_job(second, "other-worker")
            assert claimed is not None and claimed.source_id == other.id
            assert await claim_job(second, "third-worker") is None
        await process_job(db, first)
        original_id = await db.scalar(
            select(CampusKnowledgeRecord.id).where(CampusKnowledgeRecord.source_id == source.id)
        )
        claimed = await claim_job(db, "next-worker")
        assert claimed is not None and claimed.id == next_job.id
        await process_job(db, claimed)
        ids = (
            await db.scalars(select(CampusKnowledgeRecord.id).where(CampusKnowledgeRecord.source_id == source.id))
        ).all()
        assert ids == [original_id]


async def test_simultaneous_claims_only_lease_one_job_per_source():
    async with SessionLocal() as db:
        source, revision, first = await _source_and_job(db, kind="curated", config=MENU)
        first.status, first.lease_owner, first.leased_until = "queued", None, None
        db.add(CampusIngestionJob(source_id=source.id, revision_id=revision.id))
        await db.commit()

    async def claim(worker):
        async with SessionLocal() as db:
            return await claim_job(db, worker)

    jobs = await asyncio.gather(claim("a"), claim("b"))
    assert sum(job is not None for job in jobs) == 1


async def test_storage_serializes_preexisting_overlapping_leases(monkeypatch, embeddings):
    # Cover leases created before deployment, when multiple source jobs could run.
    async with SessionLocal() as db:
        source, revision, first = await _source_and_job(db, kind="curated", config=MENU)
        second = CampusIngestionJob(
            source_id=source.id,
            revision_id=revision.id,
            status="leased",
            lease_owner="legacy-worker",
            attempt=1,
            leased_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        db.add(second)
        await db.commit()
        leases = [JobLease.from_job(first), JobLease.from_job(second)]
    ready = asyncio.Event()
    arrived = 0

    async def embed(_db, _org, texts, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 5)
        return [[1.0] + [0.0] * 1535 for _ in texts]

    monkeypatch.setattr("app.knowledge.ingestion.embed_texts", embed)
    assert await asyncio.wait_for(asyncio.gather(*(run_leased_job(lease) for lease in leases)), 10) == [1, 1]
    async with SessionLocal() as db:
        rows = (await db.scalars(select(CampusKnowledgeRecord))).all()
        assert len(rows) == 1 and rows[0].external_id == "menu"


async def test_reclaimed_attempt_cannot_progress_save_renew_or_fail(embeddings):
    async with SessionLocal() as stale:
        source, _, job = await _source_and_job(stale, kind="curated", config=MENU)
        lease = JobLease.from_job(job)
        async with SessionLocal() as current:
            await current.execute(
                update(CampusIngestionJob)
                .where(CampusIngestionJob.id == job.id)
                .values(
                    leased_until=datetime.now(UTC) - timedelta(seconds=1),
                )
            )
            await current.commit()
            # Same worker name verifies the attempt number is part of fencing.
            reclaimed = await claim_job(current, lease.owner)
            assert reclaimed is not None and reclaimed.attempt == lease.attempt + 1
            job.processed_records = 999
            with pytest.raises(JobLeaseLost):
                await _set_progress(stale, job, "embedding", lease=lease)
            await stale.rollback()
            with pytest.raises(JobLeaseLost):
                await process_job(stale, job, lease=lease)
            await stale.rollback()
            with pytest.raises(JobLeaseLost):
                await renew_lease(stale, lease)
            await fail_job(stale, lease, RuntimeError("stale failure"))
            await current.refresh(reclaimed)
            assert reclaimed.status == "leased" and reclaimed.processed_records == 0
            await process_job(current, reclaimed)
            await fail_job(stale, lease, RuntimeError("late failure"))
            await current.refresh(reclaimed)
            await stale.refresh(source)
            assert reclaimed.status == "completed" and reclaimed.error_detail is None
            assert source.last_error is None


async def test_heartbeat_renews_during_a_blocked_embedding_call(monkeypatch, embeddings):
    monkeypatch.setattr(get_settings(), "knowledge_worker_lease_seconds", 3)
    async with SessionLocal() as db:
        source, revision, job = await _source_and_job(db, kind="curated", config=MENU)
        lease, original_expiry = JobLease.from_job(job), job.leased_until
        db.add(CampusIngestionJob(source_id=source.id, revision_id=revision.id))
        await db.commit()
    renewed, release = asyncio.Event(), asyncio.Event()
    original_renew = renew_lease

    async def observe_renew(db, lease):
        await original_renew(db, lease)
        renewed.set()

    async def embed(_db, _org, texts, **kwargs):
        await release.wait()
        return [[1.0] + [0.0] * 1535 for _ in texts]

    monkeypatch.setattr("app.knowledge.worker.renew_lease", observe_renew)
    monkeypatch.setattr("app.knowledge.ingestion.embed_texts", embed)
    running = asyncio.create_task(run_leased_job(lease))
    try:
        await asyncio.wait_for(renewed.wait(), 5)
        async with SessionLocal() as db:
            saved = await db.get(CampusIngestionJob, lease.job_id)
            assert saved.leased_until > original_expiry
            assert await claim_job(db, "competitor") is None
        release.set()
        assert await asyncio.wait_for(running, 5) == 1
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_lost_heartbeat_cancels_processing_without_overwriting_new_owner(monkeypatch, embeddings):
    monkeypatch.setattr(get_settings(), "knowledge_worker_lease_seconds", 3)
    async with SessionLocal() as db:
        _, _, job = await _source_and_job(db, kind="curated", config=MENU)
        lease = JobLease.from_job(job)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def embed(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("app.knowledge.ingestion.embed_texts", embed)
    running = asyncio.create_task(run_leased_job(lease))
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with SessionLocal() as db:
            await db.execute(
                update(CampusIngestionJob)
                .where(CampusIngestionJob.id == lease.job_id)
                .values(
                    lease_owner="replacement",
                    attempt=lease.attempt + 1,
                    leased_until=datetime.now(UTC) + timedelta(minutes=5),
                )
            )
            await db.commit()
        with pytest.raises(JobLeaseLost):
            await asyncio.wait_for(running, 5)
        assert cancelled.is_set()
        async with SessionLocal() as db:
            await fail_job(db, lease, RuntimeError("late error"))
            saved = await db.get(CampusIngestionJob, lease.job_id)
            assert saved.lease_owner == "replacement" and saved.status == "leased"
            assert saved.error_detail is None
            assert (await db.scalars(select(CampusKnowledgeRecord))).all() == []
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_current_owner_failure_schedules_retry_and_clears_lease(monkeypatch, embeddings):
    monkeypatch.setattr("app.knowledge.ingestion.embed_texts", AsyncMock(side_effect=ValueError("provider error")))
    async with SessionLocal() as db:
        source, _, job = await _source_and_job(db, kind="curated", config=MENU)
        lease = JobLease.from_job(job)
        with pytest.raises(ValueError, match="provider error") as error:
            await run_leased_job(lease)
        await fail_job(db, lease, error.value)
        await db.refresh(job)
        await db.refresh(source)
        assert job.status == "failed" and job.available_at > datetime.now(UTC)
        assert job.lease_owner is None and job.leased_until is None
        assert source.last_error == "provider error"
        with pytest.raises(JobLeaseLost):
            await renew_lease(db, lease)


@pytest.mark.parametrize("kind", ["ingest", "reembed", "not_modified"])
async def test_revision_change_during_io_cannot_save_or_overwrite_source(monkeypatch, embeddings, kind):
    async with SessionLocal() as db:
        source, revision, job = await _source_and_job(db, kind="html_page", config={})
        job.kind = "reembed" if kind == "reembed" else "ingest"
        row = CampusKnowledgeRecord(
            source_id=source.id,
            source_revision_id=revision.id,
            external_id="menu",
            record_type="announcement",
            title="Before",
            content="Before",
            content_hash="a" * 64,
        )
        db.add(row)
        await db.commit()
        lease, source_id, row_id = JobLease.from_job(job), source.id, row.id

        async def publish_new():
            async with SessionLocal() as publisher:
                current = await publisher.get(CampusSource, source_id)
                newer = CampusSourceRevision(source_id=source_id, revision=2, config={}, validation={"ok": True})
                publisher.add(newer)
                await publisher.flush()
                await publish_revision(publisher, current, newer)
                record = await publisher.get(CampusKnowledgeRecord, row_id)
                record.content, record.title, record.source_revision_id = "New revision", "New revision", newer.id
                current.etag = "new-etag"
                await publisher.commit()

        async def embed(_db, _org, texts, **kwargs):
            await publish_new()
            return [[1.0] + [0.0] * 1535 for _ in texts]

        async def fetch(*args, **kwargs):
            if kind == "not_modified":
                await publish_new()
            return FetchedDocument(
                source.url, b"<main><h1>Old content</h1></main>", "text/html", not_modified=kind == "not_modified"
            )

        monkeypatch.setattr("app.knowledge.ingestion.embed_texts", embed)
        monkeypatch.setattr("app.knowledge.ingestion.fetch_document", fetch)
        with pytest.raises(SourceRevisionChanged):
            await process_job(db, job)
        await fail_job(db, lease, SourceRevisionChanged("superseded"))
        await db.refresh(source)
        await db.refresh(row)
        await db.refresh(job)
        assert row.content == "New revision" and row.is_current
        assert row.embedding_model is None
        assert source.etag == "new-etag" and source.last_error is None
        assert job.status == "dead"


async def test_publishing_reparses_unchanged_page_with_new_settings(monkeypatch, embeddings):
    async with SessionLocal() as db:
        source, _, first = await _source_and_job(db, kind="html_page", config={"content_selector": ".old"})
        page = b"<h1>Menu</h1><div class='old'>Old selector</div><div class='new'>New selector</div>"
        fetch = AsyncMock(return_value=FetchedDocument(source.url, page, "text/html", etag="same-bytes"))
        monkeypatch.setattr("app.knowledge.ingestion.fetch_document", fetch)
        await process_job(db, first)
        source.last_modified = "Fri, 04 Sep 2026 12:00:00 GMT"
        revision = CampusSourceRevision(
            source_id=source.id,
            revision=2,
            config={"content_selector": ".new"},
            validation={"ok": True},
        )
        db.add(revision)
        await db.commit()
        await publish_revision(db, source, revision)
        assert source.etag is None and source.last_modified is None
        job = await claim_job(db, "new-revision-worker")
        assert job is not None
        await process_job(db, job)
        assert fetch.call_args.kwargs["etag"] is None
        assert fetch.call_args.kwargs["last_modified"] is None
        row = (await db.scalars(select(CampusKnowledgeRecord))).one()
        assert "New selector" in row.content and "Old selector" not in row.content


@pytest.mark.parametrize("running_kind", ["ingest", "reembed"])
async def test_reindex_queues_new_model_after_running_job(monkeypatch, embeddings, running_kind):
    monkeypatch.setattr("app.api.v1.knowledge_admin.record_event", AsyncMock())
    async with SessionLocal() as db:
        source, revision, job = await _source_and_job(db, kind="curated", config=MENU)
        if running_kind == "reembed":
            await process_job(db, job)
            db.add(CampusIngestionJob(source_id=source.id, revision_id=revision.id, kind="reembed"))
            await db.commit()
            job = await claim_job(db, "old-model-worker")
        principal = SimpleNamespace(
            organization_id=source.organization_id, user=SimpleNamespace(id=source.organization_id)
        )
        old_config = EmbeddingConfig("remote", "gemini-embedding-001", None, 768, 32)
        monkeypatch.setattr("app.knowledge.ingestion.get_embedding_config", AsyncMock(return_value=old_config))

        async def switch_model(_db, _org, texts, **kwargs):
            async with SessionLocal() as admin:
                result = await reindex_embeddings(principal=principal, db=admin)
                assert result["queued"] == 1
                assert (await reindex_embeddings(principal=principal, db=admin))["queued"] == 0
                assert await claim_job(admin, "new-model-worker") is None
            return [[1.0] + [0.0] * 767 for _ in texts]

        monkeypatch.setattr("app.knowledge.ingestion.embed_texts", switch_model)
        await process_job(db, job)
        row = (await db.scalars(select(CampusKnowledgeRecord))).one()
        assert row.embedding_model == old_config.model_label
        monkeypatch.setattr("app.knowledge.ingestion.get_embedding_config", AsyncMock(return_value=QWEN))
        monkeypatch.setattr("app.knowledge.ingestion.embed_texts", AsyncMock(return_value=[[1.0] + [0.0] * 1535]))
        successor = await claim_job(db, "new-model-worker")
        assert successor is not None and successor.kind == "reembed"
        await process_job(db, successor)
        await db.refresh(row)
        assert row.embedding_model == QWEN.model_label
        assert row.embedding_768 is None and len(row.embedding_1536) == 1536

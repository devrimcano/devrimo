from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (
    CampusKnowledgeRecord,
    CampusSource,
    CampusSourceRevision,
    KnowledgeEmbeddingSettings,
    Organization,
)
from app.db.session import SessionLocal
from app.knowledge.index_models import KnowledgeIndexJob, KnowledgeIndexVector
from app.knowledge.indexes import (
    IndexLeaseLost,
    IndexNotReady,
    activate_generation,
    active_generation,
    claim_index_job,
    coverage,
    create_generation,
    process_index_batch,
)
from app.knowledge.retrieval import search_knowledge


async def corpus(db):
    org = Organization(slug="generation-test", name="Generation test")
    db.add(org)
    await db.flush()
    source = CampusSource(organization_id=org.id, name="Library", kind="curated", enabled=True, status="published")
    settings = KnowledgeEmbeddingSettings(
        organization_id=org.id,
        provider="local",
        model="model-a",
        base_url="http://embedding.test/v1",
        dimensions=384,
        batch_size=8,
    )
    db.add_all([source, settings])
    await db.flush()
    revision = CampusSourceRevision(source_id=source.id, revision=1, status="published", config={})
    db.add(revision)
    await db.flush()
    source.active_revision_id = revision.id
    record = CampusKnowledgeRecord(
        source_id=source.id,
        source_revision_id=revision.id,
        external_id="library-hours",
        record_type="guide",
        title="Library hours",
        content="Kütüphane hafta içi açıktır. Library opens on weekdays.",
        content_hash="a" * 64,
        metadata_json={},
        is_current=True,
    )
    db.add(record)
    await db.commit()
    return org, settings, record


async def test_generation_cutover_requires_complete_coverage_and_keeps_old_model(monkeypatch):
    calls = []

    async def embed(config, texts):
        calls.append((config.model, list(texts)))
        return [[1.0, *([0.0] * 383)] for _ in texts]

    monkeypatch.setattr("app.knowledge.embeddings._request_embeddings", embed)
    async with SessionLocal() as db:
        org, settings, _ = await corpus(db)
        first = await create_generation(db, org.id)
        org_id = org.id
        await db.commit()
        with pytest.raises(IndexNotReady):
            await activate_generation(db, org.id, first.id)
        await db.rollback()
        lease = await claim_index_job(db, "one")
        assert lease is not None
        assert await process_index_batch(db, lease[0], "one", lease[1])
        await activate_generation(db, org_id, lease[0])
        await db.commit()
        semantic = await search_knowledge(db, "zzzzzzzz", organization_id=org_id)
        assert len(semantic) == 1 and semantic[0]["title"] == "Library hours"
        org_id = (await active_generation(db, (await db.scalar(select(Organization))).id)).organization_id
        settings = await db.get(KnowledgeEmbeddingSettings, org_id)
        settings.model = "model-b"
        await db.flush()
        second = await create_generation(db, org_id)
        await db.commit()
        assert (await active_generation(db, org_id)).model == "model-a"
        assert await coverage(db, second) == (1, 0)
        lease = await claim_index_job(db, "two")
        assert lease and lease[0] == second.id
        assert await process_index_batch(db, lease[0], "two", lease[1])
        # Candidate preview uses its frozen model without changing live queries.
        preview = await search_knowledge(db, "previewzz", organization_id=org_id, generation_id=second.id)
        assert preview and calls[-1][0] == "model-b"
        await search_knowledge(db, "activezz", organization_id=org_id)
        assert calls[-1][0] == "model-a"
        await activate_generation(db, org_id, second.id)
        await db.commit()
        assert (await active_generation(db, org_id)).model == "model-b"
        assert [model for model, _ in calls] == ["model-a", "model-a", "model-b", "model-b", "model-a"]


async def test_exact_inputs_reused_and_stale_content_not_counted(monkeypatch):
    calls = []

    async def embed(config, texts):
        calls.append(list(texts))
        return [[1.0, *([0.0] * 383)] for _ in texts]

    monkeypatch.setattr("app.knowledge.embeddings._request_embeddings", embed)
    async with SessionLocal() as db:
        org, _, record = await corpus(db)
        first = await create_generation(db, org.id)
        await db.commit()
        lease = await claim_index_job(db, "one")
        assert await process_index_batch(db, lease[0], "one", lease[1])
        # A new generation with identical document inputs reuses the vector.
        second = await create_generation(db, org.id)
        await db.commit()
        lease = await claim_index_job(db, "two")
        assert await process_index_batch(db, lease[0], "two", lease[1])
        assert len(calls) == 1
        record.content, record.content_hash = "A new library policy", "b" * 64
        await db.commit()
        assert await coverage(db, first) == (1, 0)
        assert await coverage(db, second) == (1, 0)
        lease = await claim_index_job(db, "three")
        assert await process_index_batch(db, lease[0], "three", lease[1])
        assert len(calls) == 2


async def test_expired_worker_cannot_publish_vectors(monkeypatch):
    async def embed(config, texts):
        return [[1.0, *([0.0] * 383)] for _ in texts]

    monkeypatch.setattr("app.knowledge.embeddings._request_embeddings", embed)
    async with SessionLocal() as db:
        org, _, _ = await corpus(db)
        generation = await create_generation(db, org.id)
        await db.commit()
        lease = await claim_index_job(db, "dead-worker")
        job = await db.get(KnowledgeIndexJob, generation.id)
        job.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        with pytest.raises(IndexLeaseLost):
            await process_index_batch(db, lease[0], "dead-worker", lease[1])
        assert (await db.scalars(select(KnowledgeIndexVector))).all() == []

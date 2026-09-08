from sqlalchemy import select

from app.db.models import CampusIngestionJob, CampusKnowledgeRecord, CampusSource, CampusSourceRevision, Organization
from app.db.session import SessionLocal
from app.knowledge.adapters import adapter_for
from app.knowledge.embeddings import EmbeddingConfig
from app.knowledge.ingestion import claim_job, process_job
from app.knowledge.types import FetchedDocument


async def _source_and_job(db, *, kind, config):
    org = Organization(slug="ingestion-test", name="Ingestion test")
    db.add(org)
    await db.flush()
    source = CampusSource(
        organization_id=org.id,
        name="Cafeteria",
        kind=kind,
        url="https://kafeterya.metu.edu.tr",
        enabled=True,
        status="published",
    )
    db.add(source)
    await db.flush()
    revision = CampusSourceRevision(source_id=source.id, revision=1, status="published", config=config)
    db.add(revision)
    await db.flush()
    source.active_revision_id = revision.id
    job = CampusIngestionJob(source_id=source.id, revision_id=revision.id)
    db.add(job)
    await db.commit()
    job = await claim_job(db, "test-worker")
    assert job is not None
    return source, revision, job


async def test_ingest_repeated_drupal_ids_and_switch_embedding_model(monkeypatch):
    # Drupal selectors can match both a views-row wrapper and its article.
    document = FetchedDocument(
        "https://kafeterya.metu.edu.tr",
        '<div class="views-row"><article><h2><a href="/safak-corba">ŞAFAK ÇORBA</a></h2>'
        "<p>Lunch menu</p></article></div>".encode(),
        "text/html",
    )
    parsed = adapter_for("drupal").parse(document, {})
    assert [record.external_id for record in parsed] == ["/safak-corba", "/safak-corba"]

    async def fake_fetch(*args, **kwargs):
        return document

    config = EmbeddingConfig("remote", "gemini-embedding-001", None, 768, 32)
    embedded_inputs = []

    async def fake_config(*args):
        return config

    async def fake_embeddings(_db, _org, texts, *, config):
        embedded_inputs.extend(texts)
        return [[1.0] + [0.0] * (config.dimensions - 1) for _ in texts]

    monkeypatch.setattr("app.knowledge.ingestion.fetch_document", fake_fetch)
    monkeypatch.setattr("app.knowledge.ingestion.get_embedding_config", fake_config)
    monkeypatch.setattr("app.knowledge.ingestion.embed_texts", fake_embeddings)

    async with SessionLocal() as db:
        source, revision, job = await _source_and_job(db, kind="drupal", config={})
        assert await process_job(db, job) == 1
        row = (await db.execute(select(CampusKnowledgeRecord))).scalar_one()
        original_id, first_seen = row.id, row.first_seen_at
        assert row.embedding_model == "remote:gemini-embedding-001:768"
        assert job.total_records == job.processed_records == job.embedded_records == 1
        assert len(embedded_inputs) == 1

        config = EmbeddingConfig("remote", "qwen/qwen3-embedding-8b", None, 1536, 32)
        # Both ordinary ingestion and explicit re-embedding must reuse the row.
        for kind in ("ingest", "reembed"):
            next_job = CampusIngestionJob(source_id=source.id, revision_id=revision.id, kind=kind)
            db.add(next_job)
            await db.commit()
            next_job = await claim_job(db, "test-worker")
            assert next_job is not None
            assert await process_job(db, next_job) == 1
            row = (await db.execute(select(CampusKnowledgeRecord))).scalar_one()
            await db.refresh(row)
            assert row.id == original_id
            assert row.first_seen_at == first_seen
            assert row.embedding_768 is None
            assert row.embedding_384 is None
            assert len(row.embedding_1536) == 1536
            assert row.embedding_model == "remote:qwen/qwen3-embedding-8b:1536"
            assert row.is_current
            assert next_job.status == "completed"
        assert len(embedded_inputs) == 3


async def test_duplicate_parent_is_resolved_before_chunking(monkeypatch):
    async def disabled_config(*args):
        return EmbeddingConfig("disabled", "unused", None, 384, 32)

    monkeypatch.setattr("app.knowledge.ingestion.get_embedding_config", disabled_config)
    async with SessionLocal() as db:
        _, _, job = await _source_and_job(
            db,
            kind="curated",
            config={
                "chunk_max_chars": 600,
                "chunk_context_chars": 0,
                "records": [
                    {"external_id": "menu", "title": "Old menu", "content": "Old dish. " * 300},
                    {"external_id": "menu", "title": "New menu", "content": "New dish."},
                ],
            },
        )
        assert await process_job(db, job) == 1
        row = (await db.execute(select(CampusKnowledgeRecord))).scalar_one()
        assert row.external_id == "menu"
        assert row.title == "New menu"
        assert row.content == "New dish."
        assert row.metadata_json["chunk_count"] == 1

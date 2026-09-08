"""Hybrid campus retrieval.

Every ranking signal here is produced by an index inside PostgreSQL:

* stemmed full-text matching over a language-aware ``tsvector`` (GIN),
* trigram word-similarity for the agglutinative forms Snowball misses (GIN),
* approximate nearest neighbours over pgvector embeddings (HNSW).

The three ranked candidate lists are fused with Reciprocal Rank Fusion in the
same statement, so the database returns an already-ordered result set and the
application only picks one chunk per document.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Float, case, func, literal, literal_column, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CampusKnowledgeRecord, CampusSource
from app.knowledge.embeddings import embed_query
from app.knowledge.index_models import KnowledgeIndexGeneration, KnowledgeIndexVector
from app.knowledge.indexes import active_generation, generation_config, vector_column

# Reciprocal Rank Fusion. The constant damps the influence of the very top of
# each list so a single channel cannot dominate the fused ordering.
RRF_K = 60
LEXICAL_WEIGHT = 0.40
TRIGRAM_WEIGHT = 0.20
SEMANTIC_WEIGHT = 0.40

# Snowball has no configuration for every language in the corpus, and a query
# carries no language of its own, so both configurations are probed against the
# one GIN index. Turkish is the majority language and is listed first.
TEXT_SEARCH_CONFIGS = ("turkish", "english")

# Recent records get a small tie-breaking nudge over equally-ranked older ones.
FRESHNESS_WINDOW = timedelta(days=14)


@dataclass(slots=True)
class SearchFilters:
    source_id: UUID | None = None
    record_types: tuple[str, ...] = ()
    language: str | None = None
    campus: str | None = None
    department: str | None = None
    degree_level: str | None = None
    starts_after: datetime | None = None
    starts_before: datetime | None = None
    include_expired: bool = False


def _metadata(record: CampusKnowledgeRecord) -> dict:
    return record.metadata_json if isinstance(record.metadata_json, dict) else {}


def _document_id(record: CampusKnowledgeRecord) -> str:
    metadata = _metadata(record)
    return str(metadata.get("document_external_id") or metadata.get("parent_external_id") or record.external_id)


def _serialize(record: CampusKnowledgeRecord, source: CampusSource, score: float) -> dict:
    metadata = _metadata(record)
    return {
        "id": str(record.id),
        "document_id": _document_id(record),
        "type": record.record_type,
        "title": record.title,
        "summary": record.summary,
        "content": record.content,
        "section": metadata.get("section"),
        "chunk_index": metadata.get("chunk_index", 0),
        "chunk_count": metadata.get("chunk_count", 1),
        "page_number": metadata.get("page_number"),
        "url": record.url,
        "language": record.language,
        "starts_at": record.starts_at.isoformat() if record.starts_at else None,
        "ends_at": record.ends_at.isoformat() if record.ends_at else None,
        "published_at": record.published_at.isoformat() if record.published_at else None,
        "valid_until": record.valid_until.isoformat() if record.valid_until else None,
        "authority": record.authority,
        "source": source.name,
        "source_id": str(source.id),
        "source_url": source.url,
        "source_last_success_at": source.last_success_at.isoformat() if source.last_success_at else None,
        "score": round(float(score), 6),
    }


def _conditions(filters: SearchFilters, organization_id: UUID, now: datetime) -> list:
    conditions = [
        CampusKnowledgeRecord.is_current.is_(True),
        CampusSource.organization_id == organization_id,
        CampusSource.status == "published",
        CampusSource.enabled.is_(True),
    ]
    if filters.source_id:
        conditions.append(CampusSource.id == filters.source_id)
    if filters.record_types:
        conditions.append(CampusKnowledgeRecord.record_type.in_(filters.record_types))
    if filters.language:
        conditions.append(CampusKnowledgeRecord.language == filters.language)
    if filters.campus:
        conditions.append(or_(CampusKnowledgeRecord.campus.is_(None), CampusKnowledgeRecord.campus == filters.campus))
    if filters.department:
        conditions.append(
            or_(CampusKnowledgeRecord.department.is_(None), CampusKnowledgeRecord.department == filters.department)
        )
    if filters.degree_level:
        conditions.append(
            or_(
                CampusKnowledgeRecord.degree_level.is_(None),
                CampusKnowledgeRecord.degree_level == filters.degree_level,
            )
        )
    if filters.starts_after:
        conditions.append(
            or_(CampusKnowledgeRecord.starts_at.is_(None), CampusKnowledgeRecord.starts_at >= filters.starts_after)
        )
    if filters.starts_before:
        conditions.append(
            or_(CampusKnowledgeRecord.starts_at.is_(None), CampusKnowledgeRecord.starts_at <= filters.starts_before)
        )
    if not filters.include_expired:
        conditions.append(or_(CampusKnowledgeRecord.valid_until.is_(None), CampusKnowledgeRecord.valid_until >= now))
    return conditions


def _ranked_channel(
    conditions: list, signal, match, candidate_limit: int, *, descending: bool, name: str, generation_id=None
):
    """Rank one retrieval channel in the database and number its results.

    The inner statement is ordered and limited so the channel's index bounds the
    work; the outer statement only assigns the rank RRF consumes.
    """
    order = signal.desc() if descending else signal.asc()
    statement = select(CampusKnowledgeRecord.id.label("record_id"), signal.label("signal")).join(
        CampusSource, CampusSource.id == CampusKnowledgeRecord.source_id
    )
    if generation_id is not None:
        statement = statement.join(
            KnowledgeIndexVector,
            (KnowledgeIndexVector.record_id == CampusKnowledgeRecord.id)
            & (KnowledgeIndexVector.generation_id == generation_id)
            & (KnowledgeIndexVector.content_hash == CampusKnowledgeRecord.content_hash),
        )
    inner = statement.where(*conditions, match).order_by(order).limit(candidate_limit).subquery()
    inner_order = inner.c.signal.desc() if descending else inner.c.signal.asc()
    return select(
        inner.c.record_id,
        func.row_number().over(order_by=inner_order).label("rank"),
    ).cte(name)


def _weighted(cte, weight: float):
    return select(
        cte.c.record_id,
        (literal(weight) / (literal(RRF_K) + func.cast(cte.c.rank, Float))).label("contribution"),
    )


def _pick_per_document(
    rows: list[tuple[CampusKnowledgeRecord, CampusSource, float]],
    *,
    limit: int,
) -> list[dict]:
    """Return the best-ranked chunk per document, preserving fused order."""
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for record, source, score in rows:
        key = (str(source.id), _document_id(record))
        if key in seen:
            continue
        seen.add(key)
        results.append(_serialize(record, source, score))
        if len(results) >= limit:
            break
    return results


async def search_knowledge(
    db: AsyncSession,
    query: str,
    filters: SearchFilters | None = None,
    *,
    organization_id: UUID,
    limit: int = 10,
    generation_id: UUID | None = None,
) -> list[dict]:
    filters = filters or SearchFilters()
    now = datetime.now(UTC)
    conditions = _conditions(filters, organization_id, now)
    clean_query = query.strip()

    base = select(CampusKnowledgeRecord, CampusSource).join(
        CampusSource, CampusSource.id == CampusKnowledgeRecord.source_id
    )
    if not clean_query:
        rows = (
            await db.execute(
                base.where(*conditions)
                .order_by(CampusKnowledgeRecord.published_at.desc().nullslast(), CampusKnowledgeRecord.authority.desc())
                .limit(max(limit * 5, 50))
            )
        ).all()
        return _pick_per_document([(record, source, 1.0) for record, source in rows], limit=limit)

    if generation_id:
        generation = await db.scalar(
            select(KnowledgeIndexGeneration).where(
                KnowledgeIndexGeneration.id == generation_id,
                KnowledgeIndexGeneration.organization_id == organization_id,
            )
        )
        if generation is None:
            raise ValueError("Index generation not found")
    else:
        generation = await active_generation(db, organization_id)
    config = generation_config(generation) if generation else None
    query_embedding = (
        await embed_query(db, organization_id, clean_query, config=config) if config and config.enabled else None
    )
    candidate_limit = min(500, max(limit * 8, 50))

    search_vector = literal_column("campus_knowledge_records.search_vector")
    search_text = literal_column("campus_knowledge_records.search_text")
    tsqueries = [func.websearch_to_tsquery(name, clean_query) for name in TEXT_SEARCH_CONFIGS]

    channels = [
        (
            _ranked_channel(
                conditions,
                func.greatest(*[func.ts_rank_cd(search_vector, tsquery) for tsquery in tsqueries]),
                or_(*[search_vector.op("@@")(tsquery) for tsquery in tsqueries]),
                candidate_limit,
                descending=True,
                name="lexical",
            ),
            LEXICAL_WEIGHT,
        ),
        (
            _ranked_channel(
                conditions,
                func.word_similarity(clean_query, search_text),
                search_text.op("%>")(clean_query),
                candidate_limit,
                descending=True,
                name="trigram",
            ),
            TRIGRAM_WEIGHT,
        ),
    ]
    if query_embedding is not None and config is not None and generation is not None:
        column = vector_column(config.dimensions)
        channels.append(
            (
                _ranked_channel(
                    conditions,
                    column.cosine_distance(query_embedding),
                    column.is_not(None),
                    candidate_limit,
                    descending=False,
                    name="semantic",
                    generation_id=generation.id,
                ),
                SEMANTIC_WEIGHT,
            )
        )

    contributions = union_all(*[_weighted(cte, weight) for cte, weight in channels]).subquery()
    fused = (
        select(
            contributions.c.record_id,
            func.sum(contributions.c.contribution).label("score"),
        )
        .group_by(contributions.c.record_id)
        .cte("fused")
    )

    # Authority and freshness are deliberately small: they break ties between
    # comparable matches rather than promoting a weak match from a loud source.
    freshness = case((CampusKnowledgeRecord.published_at >= now - FRESHNESS_WINDOW, 0.001), else_=0.0)
    quality = (func.cast(CampusKnowledgeRecord.authority, Float) / 100) * 0.002 + freshness

    statement = (
        base.add_columns(((fused.c.score + quality) * 100).label("final_score"))
        .join(fused, fused.c.record_id == CampusKnowledgeRecord.id)
        .order_by(literal_column("final_score").desc())
        .limit(max(limit * 5, 50))
    )
    rows = (await db.execute(statement)).all()
    return _pick_per_document([(record, source, score) for record, source, score in rows], limit=limit)


async def read_campus_page(db: AsyncSession, url: str, *, organization_id: UUID) -> dict | None:
    """Reconstruct a full indexed document from its stored chunks."""
    lead_row = (
        await db.execute(
            select(CampusKnowledgeRecord, CampusSource)
            .join(CampusSource, CampusSource.id == CampusKnowledgeRecord.source_id)
            .where(
                CampusKnowledgeRecord.url == url,
                CampusKnowledgeRecord.is_current.is_(True),
                CampusSource.organization_id == organization_id,
                CampusSource.status == "published",
                CampusSource.enabled.is_(True),
            )
            .order_by(CampusKnowledgeRecord.last_seen_at.desc())
            .limit(1)
        )
    ).one_or_none()
    if lead_row is None:
        return None
    lead, source = lead_row
    document_id = _document_id(lead)

    # Chunk ids are derived from the document id, so the siblings are selected
    # by that key in the database rather than by scanning and filtering here.
    siblings = (
        (
            await db.execute(
                select(CampusKnowledgeRecord)
                .where(
                    CampusKnowledgeRecord.source_id == source.id,
                    CampusKnowledgeRecord.is_current.is_(True),
                    or_(
                        CampusKnowledgeRecord.external_id == document_id,
                        CampusKnowledgeRecord.external_id.startswith(f"{document_id}::"),
                    ),
                )
                .order_by(
                    CampusKnowledgeRecord.metadata_json["page_number"].as_integer().nullsfirst(),
                    CampusKnowledgeRecord.metadata_json["chunk_index"].as_integer().nullsfirst(),
                    CampusKnowledgeRecord.external_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not siblings:
        siblings = [lead]

    result = _serialize(siblings[0], source, 1.0)
    reconstructed: list[str] = []
    previous_section: str | None = None
    for record in siblings:
        section = _metadata(record).get("section")
        if section and section != previous_section:
            reconstructed.append(str(section))
        reconstructed.append(record.content)
        previous_section = str(section) if section else None
    result["content"] = "\n\n".join(reconstructed)
    result["summary"] = next((record.summary for record in siblings if record.summary), None)
    result["section"] = None
    result["chunk_index"] = 0
    result["chunk_count"] = len(siblings)
    return result

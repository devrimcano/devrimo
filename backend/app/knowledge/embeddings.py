from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto import decrypt_secret
from app.db.models import CampusKnowledgeRecord, KnowledgeEmbeddingSettings
from app.logging import get_logger
from app.observability.client import report_exception
from app.observability.llm import EVENT_AI_EMBEDDING, observed_ai_operation

SUPPORTED_VECTOR_DIMENSIONS = (384, 768, 1536)
logger = get_logger(__name__)


@lru_cache
def _embedding_client() -> httpx.AsyncClient:
    """One pooled client for all embedding batches in this process."""
    return httpx.AsyncClient(timeout=60)


async def close_embedding_client() -> None:
    if _embedding_client.cache_info().currsize:
        await _embedding_client().aclose()
        _embedding_client.cache_clear()


class EmbeddingResponseError(ValueError):
    """The provider returned a response that cannot be paired to inputs safely."""


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    provider: str
    model: str
    base_url: str | None
    dimensions: int
    batch_size: int
    api_key: str | None = None
    database_override: bool = False
    query_prefix: str = ""
    document_prefix: str = ""

    @property
    def enabled(self) -> bool:
        return self.provider in {"local", "remote"}

    @property
    def model_label(self) -> str:
        """Identify vectors by everything that changes their meaning.

        A stored vector is only comparable to a query vector produced the same
        way, so the document prefix belongs in the label: changing it retires
        the old vectors instead of silently mixing two embedding spaces. The
        label is left untouched when no prefix is configured, which keeps
        already-indexed corpora valid.
        """
        label = f"{self.provider}:{self.model}:{self.dimensions}"
        if not self.document_prefix:
            return label
        digest = hashlib.sha256(self.document_prefix.encode()).hexdigest()[:8]
        return f"{label}:{digest}"


async def get_embedding_config(db: AsyncSession, organization_id: UUID) -> EmbeddingConfig:
    row = await db.get(KnowledgeEmbeddingSettings, organization_id)
    if row is not None:
        return EmbeddingConfig(
            provider=row.provider,
            model=row.model,
            base_url=row.base_url,
            dimensions=row.dimensions,
            batch_size=row.batch_size,
            api_key=decrypt_secret(row.api_key_enc) if row.api_key_enc else None,
            database_override=True,
            query_prefix=row.query_prefix or "",
            document_prefix=row.document_prefix or "",
        )

    settings = get_settings()
    provider = "remote" if settings.knowledge_embedding_enabled else "disabled"
    return EmbeddingConfig(
        provider=provider,
        model=settings.knowledge_embedding_model,
        base_url=settings.knowledge_embedding_base_url if provider == "remote" else None,
        dimensions=settings.knowledge_embedding_dimensions,
        batch_size=32,
        api_key=settings.knowledge_embedding_api_key or None,
    )


def _reported_usage(payload: Any) -> tuple[int | None, int | None]:
    """Token counts as the provider reported them, or ``None`` when it did not.

    Absence is meaningful: a provider that omits ``usage`` has told us nothing
    about cost, which is not the same as having cost nothing.
    """
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None, None

    def _count(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return _count("prompt_tokens"), _count("total_tokens")


async def _request_embeddings(config: EmbeddingConfig, texts: Sequence[str]) -> list[list[float]]:
    if not config.base_url:
        raise ValueError("Embedding endpoint is not configured")
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    body: dict = {"model": config.model, "input": list(texts)}
    # Local OpenAI-compatible endpoints often expose models with a fixed
    # output width and reject the OpenAI-specific dimensions parameter.
    if config.provider == "remote":
        body["dimensions"] = config.dimensions
    # Agno's model client is instrumented, but embeddings never go through it —
    # they are a plain httpx call, and were the one model provider this service
    # talks to that produced no AI telemetry at all.
    with observed_ai_operation(
        "knowledge_embedding",
        provider=config.provider,
        model=config.model,
        event=EVENT_AI_EMBEDDING,
        batch_size=len(texts),
        dimensions=config.dimensions,
    ) as call:
        response = await _embedding_client().post(
            f"{config.base_url.rstrip('/')}/embeddings",
            headers=headers,
            json=body,
        )
        call.http_status = response.status_code
        response.raise_for_status()
        payload = response.json()
        prompt_tokens, total_tokens = _reported_usage(payload)
        call.usage(input_tokens=prompt_tokens, total_tokens=total_tokens)
        return _response_vectors(payload, len(texts))


def _response_vectors(payload: Any, expected_count: int) -> list[list[float]]:
    """Normalize OpenAI-compatible indexed and ordered embedding responses.

    OpenAI includes an ``index`` in every data item. Gemini's compatible
    endpoint preserves input order but omits that field. Accept both contracts,
    while rejecting mixed or incomplete batches rather than pairing a vector
    with the wrong source text.
    """
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise EmbeddingResponseError("Embedding provider returned an incomplete batch")
    if not all(isinstance(item, dict) and isinstance(item.get("embedding"), list) for item in rows):
        raise EmbeddingResponseError("Embedding provider returned a malformed batch")

    indexed_rows = ["index" in item for item in rows]
    if any(indexed_rows) and not all(indexed_rows):
        raise EmbeddingResponseError("Embedding provider returned inconsistent indexes")
    if not any(indexed_rows):
        return [item["embedding"] for item in rows]

    indexed: dict[int, list[float]] = {}
    for item in rows:
        index = item["index"]
        if not isinstance(index, int) or isinstance(index, bool) or index in indexed:
            raise EmbeddingResponseError("Embedding provider returned invalid indexes")
        indexed[index] = item["embedding"]
    if set(indexed) != set(range(expected_count)):
        raise EmbeddingResponseError("Embedding provider returned incomplete indexes")
    return [indexed[index] for index in range(expected_count)]


def _storage_vector(vector: Sequence[float], expected_dimensions: int) -> list[float]:
    if expected_dimensions not in SUPPORTED_VECTOR_DIMENSIONS:
        raise ValueError(f"Embedding dimensions must be one of {SUPPORTED_VECTOR_DIMENSIONS}")
    if len(vector) != expected_dimensions:
        raise ValueError(
            f"Embedding provider returned {len(vector)} dimensions; expected {expected_dimensions}"
        )
    return [float(value) for value in vector]


def embedding_column(dimensions: int):
    columns = {
        384: CampusKnowledgeRecord.embedding_384,
        768: CampusKnowledgeRecord.embedding_768,
        1536: CampusKnowledgeRecord.embedding_1536,
    }
    try:
        return columns[dimensions]
    except KeyError as exc:
        raise ValueError(f"Embedding dimensions must be one of {SUPPORTED_VECTOR_DIMENSIONS}") from exc


def assign_embedding(record: CampusKnowledgeRecord, vector: list[float] | None, dimensions: int) -> None:
    record.embedding_384 = vector if dimensions == 384 else None
    record.embedding_768 = vector if dimensions == 768 else None
    record.embedding_1536 = vector if dimensions == 1536 else None


async def embed_texts(
    db: AsyncSession,
    organization_id: UUID,
    texts: Sequence[str],
    *,
    config: EmbeddingConfig | None = None,
    prefix: str | None = None,
) -> list[list[float] | None]:
    """Embed only public canonical text using the organization's provider.

    ``prefix`` defaults to the configured document instruction; callers
    embedding a search query pass the query instruction instead.
    """
    resolved = config or await get_embedding_config(db, organization_id)
    if not texts or not resolved.enabled:
        return [None for _ in texts]
    if resolved.provider == "remote" and not resolved.api_key:
        raise ValueError("Remote embedding API key is not configured")
    lead = resolved.document_prefix if prefix is None else prefix
    texts = [f"{lead}{text}" for text in texts] if lead else list(texts)
    try:
        vectors = await _request_embeddings(resolved, texts)
    except EmbeddingResponseError as exc:
        if len(texts) == 1:
            raise
        # Some OpenAI-compatible endpoints return mixed or incomplete index
        # metadata for batch requests. Retry each item independently instead
        # of guessing the input/output pairing and corrupting the vector store.
        logger.warning(
            "knowledge_embedding_batch_fallback",
            provider=resolved.provider,
            batch_size=len(texts),
            error=str(exc),
        )
        # A provider that cannot pair its own outputs to our inputs is a real
        # defect on their side, and the per-item retry hides it completely.
        report_exception(
            exc,
            handler="knowledge_embedding_batch",
            provider=resolved.provider,
            model=resolved.model,
            batch_size=len(texts),
            fallback="per_item_retry",
        )
        vectors = []
        for text in texts:
            vectors.extend(await _request_embeddings(resolved, [text]))
    return [_storage_vector(vector, resolved.dimensions) for vector in vectors]


async def embed_query(
    db: AsyncSession,
    organization_id: UUID,
    text: str,
    *,
    config: EmbeddingConfig | None = None,
) -> list[float] | None:
    resolved = config or await get_embedding_config(db, organization_id)
    try:
        vectors = await embed_texts(
            db, organization_id, [text], config=resolved, prefix=resolved.query_prefix
        )
        return vectors[0]
    except (httpx.HTTPError, ValueError) as exc:
        # Search remains available through full-text/keyword ranking when an
        # embedding endpoint is temporarily unavailable — which is precisely
        # why this needs reporting: retrieval quietly degrades to lexical-only
        # and every result still looks like a successful search.
        logger.warning("knowledge_query_embedding_failed", provider=resolved.provider, error=str(exc))
        report_exception(
            exc,
            handler="knowledge_query_embedding",
            provider=resolved.provider,
            model=resolved.model,
            dependency="embedding_provider",
            degraded_to="lexical_only",
        )
        return None

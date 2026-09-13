"""Explicit memory owner commands with atomic revision and retry history."""

import asyncio
import hashlib
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.agents.store import get_agno_db
from app.core.digest import stable_digest
from app.db.session import get_session_factory
from app.student.service import SENSITIVE_TERMS
from app.workspace.models import WorkspaceMemoryMutation


class MemoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Optional: a model saving a new preference has no id to give, and refusing
    # the write over one is how "hatırla" ended with a validation error and an
    # answer that claimed the preference was saved anyway.
    id: str | None = Field(default=None, max_length=128)
    content: str = Field(min_length=1, max_length=500)


class MemoryChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memories: list[MemoryEntry] = Field(max_length=50)


def legacy_memories(user_id):
    learning = get_agno_db().get_learning(learning_type="user_memory", user_id=str(user_id))
    content = learning.get("content", {}) if learning else {}
    return [
        {"id": str(item["id"]), "content": str(item["content"])}
        for item in content.get("memories", [])
        if isinstance(item, dict) and item.get("id") and item.get("content")
    ]


async def _latest(db, user_id):
    return await db.scalar(
        select(WorkspaceMemoryMutation)
        .where(WorkspaceMemoryMutation.user_id == user_id)
        .order_by(WorkspaceMemoryMutation.revision.desc())
        .limit(1)
    )


async def read_memories(user_id: UUID):
    async with get_session_factory("assistant")() as db:
        row = await _latest(db, user_id)
        return {
            "revision": row.revision if row else 0,
            "memories": row.after_content if row else await asyncio.to_thread(legacy_memories, user_id),
        }


async def mutate_memories(
    user_id: UUID, changes: dict | None, expected_revision: int, idempotency_key: str, *, undo=False
):
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(422, "A request key of 1-128 characters is required")
    parsed = MemoryChanges.model_validate(changes) if not undo else None
    entries: list[dict] = []
    if parsed:
        entries = [
            {"id": (item.id or "").strip() or uuid4().hex[:12], "content": item.content}
            for item in parsed.memories
        ]
        if len({entry["id"] for entry in entries}) != len(entries):
            raise HTTPException(422, "Memory identifiers must be unique")
        if any(term in entry["content"].lower() for entry in entries for term in SENSITIVE_TERMS):
            raise HTTPException(422, "Sensitive information cannot be stored as memory")
    digest = stable_digest(
        {"changes": parsed.model_dump() if parsed else None, "revision": expected_revision, "undo": undo}
    )
    async with get_session_factory("assistant")() as db:
        lock = int.from_bytes(hashlib.sha256(f"memory:{user_id}".encode()).digest()[:8], "big", signed=True)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        prior = await db.scalar(
            select(WorkspaceMemoryMutation).where(
                WorkspaceMemoryMutation.user_id == user_id, WorkspaceMemoryMutation.idempotency_key == idempotency_key
            )
        )
        if prior:
            if prior.request_digest != digest:
                raise HTTPException(409, "Idempotency key was already used for different changes")
            return {"revision": prior.revision, "memories": prior.after_content}
        current = await _latest(db, user_id)
        revision = current.revision if current else 0
        if revision != expected_revision:
            raise HTTPException(409, "Memory revision changed; read before editing")
        if undo and current is None:
            raise HTTPException(409, "No memory change to undo")
        before = current.after_content if current else await asyncio.to_thread(legacy_memories, user_id)
        after = current.before_content if undo else entries
        db.add(
            WorkspaceMemoryMutation(
                user_id=user_id,
                revision=revision + 1,
                idempotency_key=idempotency_key,
                request_digest=digest,
                before_content=before,
                after_content=after,
            )
        )
        await db.commit()
        return {"revision": revision + 1, "memories": after}


async def clear_memories(user_id: UUID):
    """Privacy deletion removes history; an empty tombstone prevents legacy revival."""
    from uuid import uuid4

    from sqlalchemy import delete

    async with get_session_factory("assistant")() as db:
        lock = int.from_bytes(hashlib.sha256(f"memory:{user_id}".encode()).digest()[:8], "big", signed=True)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        current = await _latest(db, user_id)
        revision = (current.revision if current else 0) + 1
        # Delete legacy data first: failure leaves the current canonical view
        # untouched and the client receives an error rather than false success.
        await asyncio.to_thread(get_agno_db().delete_user_learnings, str(user_id), "user_memory")
        await db.execute(delete(WorkspaceMemoryMutation).where(WorkspaceMemoryMutation.user_id == user_id))
        db.add(
            WorkspaceMemoryMutation(
                user_id=user_id,
                revision=revision,
                idempotency_key=str(uuid4()),
                request_digest="0" * 64,
                before_content=[],
                after_content=[],
            )
        )
        await db.commit()
        return {"revision": revision, "memories": []}

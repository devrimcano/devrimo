"""Student-owned controls for Scholar's explicit long-term preferences."""

import asyncio

from fastapi import APIRouter, Depends

from app.agents.store import get_agno_db
from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.schemas import MemoryEntryOut, MemoryListOut

router = APIRouter()


def _memory_entries(user_id: str) -> list[MemoryEntryOut]:
    learning = get_agno_db().get_learning(learning_type="user_memory", user_id=user_id)
    content = learning.get("content", {}) if learning else {}
    entries = content.get("memories", []) if isinstance(content, dict) else []
    return [
        MemoryEntryOut(
            id=str(entry.get("id", "")),
            content=str(entry.get("content", "")),
        )
        for entry in entries
        if isinstance(entry, dict) and entry.get("id") and entry.get("content")
    ]


@router.get("", response_model=MemoryListOut)
async def list_memories(user: AuthenticatedUser = Depends(get_current_user)) -> MemoryListOut:
    memories = await asyncio.to_thread(_memory_entries, str(user.id))
    return MemoryListOut(memories=memories)


@router.delete("", response_model=MemoryListOut)
async def delete_memories(user: AuthenticatedUser = Depends(get_current_user)) -> MemoryListOut:
    await asyncio.to_thread(get_agno_db().delete_user_learnings, str(user.id), "user_memory")
    return MemoryListOut(memories=[])

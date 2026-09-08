"""Student-owned controls for Scholar's explicit long-term preferences."""

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.schemas import MemoryEntryOut, MemoryListOut

router = APIRouter()


@router.get("", response_model=MemoryListOut)
async def list_memories(user: AuthenticatedUser = Depends(get_current_user)) -> MemoryListOut:
    from app.agents.memory import read_memories

    result = await read_memories(user.id)
    return MemoryListOut(memories=[MemoryEntryOut(**entry) for entry in result["memories"]])


@router.delete("", response_model=MemoryListOut)
async def delete_memories(user: AuthenticatedUser = Depends(get_current_user)) -> MemoryListOut:
    from app.agents.memory import clear_memories

    await clear_memories(user.id)
    return MemoryListOut(memories=[])

"""Chat session listing and history.

The important change from the Hermes era: reading history no longer requires
the user's agent to be running. ``chat_sessions`` is the index the frontend
lists (title, ownership, soft-delete) and Agno's tables hold the messages, so
both are plain database reads — opening last month's conversation costs a
query, not an agent build.
"""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.store import get_agno_db
from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.db.models import ChatSession
from app.db.session import get_db
from app.logging import get_logger
from app.observability import capture_exception
from app.observability.client import report_exception
from app.schemas import ChatMessageOut, ChatSessionDetailOut, ChatSessionListOut, ChatSessionOut

router = APIRouter()
logger = get_logger(__name__)


async def _get_owned_session(db: AsyncSession, user_id, session_id: str) -> ChatSession:
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None),
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat session not found")
    return session


def _to_message_out(message) -> ChatMessageOut | None:
    """One Agno message as the frontend's shape, or ``None`` if not displayable.

    Agno's history includes the system prompt and tool-result messages. The
    thread view wants neither, and leaking the system prompt into a response
    would hand the persona to anyone with devtools open.
    """
    role = getattr(message, "role", None)
    if role not in ("user", "assistant"):
        return None
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content:
        return None

    created_at = getattr(message, "created_at", None)
    if isinstance(created_at, int | float):
        created_at = datetime.fromtimestamp(created_at, tz=UTC).isoformat()
    elif created_at is not None:
        created_at = str(created_at)

    return ChatMessageOut(role=role, content=content, created_at=created_at)


def _load_history(agno_session_id: str, user_id: str) -> list[ChatMessageOut]:
    db = get_agno_db()
    # user_id is passed to Agno as well as being checked against our own index
    # above: two independent ownership checks on the path that returns another
    # student's conversation if either is wrong.
    session = db.get_session(session_id=agno_session_id, user_id=user_id)
    if session is None:
        return []
    messages = session.get_chat_history()
    return [out for out in (_to_message_out(m) for m in messages) if out is not None]


@router.get("", response_model=ChatSessionListOut)
async def list_sessions(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionListOut:
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user.id, ChatSession.deleted_at.is_(None))
        .order_by(ChatSession.updated_at.desc())
    )
    sessions = result.scalars().all()
    return ChatSessionListOut(sessions=[ChatSessionOut.from_model(s) for s in sessions])


@router.get("/{session_id}", response_model=ChatSessionDetailOut)
async def get_session(
    session_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionDetailOut:
    session = await _get_owned_session(db, user.id, session_id)

    try:
        messages = await asyncio.to_thread(_load_history, session.agno_session_id or session.id, str(user.id))
    except Exception as exc:
        # A history read failing should not blank the thread list; log it and
        # return the session with no messages rather than a 502.
        logger.error("history_load_failed", user_id=str(user.id), session_id=session_id, error=str(exc))
        report_exception(
            exc,
            distinct_id=str(user.id),
            handler="session_history",
            chat_session_id=session_id,
        )
        # Degrading to an empty thread is invisible to the student and to us;
        # this is the only signal that their history stopped loading.
        capture_exception(
            exc,
            distinct_id=str(user.id),
            chat_session_id=session_id,
            **{"$exception_fingerprint": ["history_load_failed"]},
        )
        messages = []

    return ChatSessionDetailOut(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=messages,
    )


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    session = await _get_owned_session(db, user.id, session_id)

    try:
        await asyncio.to_thread(
            get_agno_db().delete_session,
            session_id=session.agno_session_id or session.id,
            user_id=str(user.id),
        )
    except Exception as exc:
        # The row is still soft-deleted below, so the student stops seeing it
        # either way; an orphaned Agno session is a cleanup problem, not a
        # reason to fail their delete.
        logger.warning("agno_session_delete_failed", user_id=str(user.id), error=str(exc))
        report_exception(exc, distinct_id=str(user.id), handler="session_delete", chat_session_id=session_id)
        capture_exception(
            exc,
            distinct_id=str(user.id),
            chat_session_id=session_id,
            **{"$exception_fingerprint": ["agno_session_delete_failed"]},
        )

    session.deleted_at = datetime.now(UTC)
    await db.commit()
    return {"ok": True}

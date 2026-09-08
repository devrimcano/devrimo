"""Per-call, user-scoped leases for single-tenant campus subprocesses."""

import hashlib
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text

from app.admin.directory import active_account
from app.campus import service
from app.db.session import SessionLocal


@asynccontextmanager
async def integration_session(user_id: UUID, tool_id: str):
    # Transaction-scoped advisory locks work through transaction poolers and
    # automatically release on cancellation/connection loss. No credentials
    # or session tokens ever become part of the lock key.
    async with SessionLocal() as db:
        if db.bind.dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(f"campus:{user_id}:{tool_id}".encode()).digest()[:8], "big", signed=True
            )
            await db.execute(text("SET LOCAL lock_timeout = '30s'"))
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        if await active_account(db, user_id) is None:
            raise HTTPException(403, "Account is inactive")
        specs = [spec for spec in await service.campus_server_specs(db, user_id) if spec.tool_id == tool_id]
        from app.campus import session_pool

        if not specs:
            await session_pool.invalidate(user_id, tool_id)
            raise HTTPException(403, "Integration is not enabled or credentials are unavailable")
        revision = await service.credential_revision(db, user_id)
        entry, connected = await session_pool.acquire(user_id, tool_id, specs, revision)
        try:
            yield connected
        finally:
            entry.active_leases -= 1
            if entry.retired and entry.active_leases == 0:
                await session_pool.invalidate(user_id, tool_id)

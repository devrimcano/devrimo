"""Authenticate the caller, then count the request against a named scope.

The switch lives in the runtime settings row and defaults to off; with it off
this dependency costs one primary-key read and returns. Scopes and their
windows belong to :mod:`app.core.rate_limit`.
"""

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runtime import get_rate_limit_config
from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthenticatedUser
from app.core import rate_limit as windows
from app.db.session import get_db

_SCOPE_FIELDS = {"chat": "chat_per_minute", "catalog": "catalog_per_minute"}


def rate_limited(scope: str):
    """A dependency for routes that need a rate limit once the switch is on."""
    if scope not in _SCOPE_FIELDS:
        raise ValueError(f"Unknown rate-limit scope: {scope!r}")

    async def dependency(
        user: AuthenticatedUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> AuthenticatedUser:
        config = await get_rate_limit_config(db)
        if not config.enabled:
            return user
        per_minute = getattr(config, _SCOPE_FIELDS[scope])
        retry_after = windows.retry_after_seconds(scope, str(user.id), per_minute)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please wait a moment and try again.",
                headers={"Retry-After": str(retry_after)},
            )
        return user

    return dependency

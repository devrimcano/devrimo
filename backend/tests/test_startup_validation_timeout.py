"""What happens when the database accepts a connection and then says nothing.

This is not hypothetical. On 2026-09-09 a deploy restarted the API and seven
workers within five seconds of each other; the API's connection to the pooler
reached ESTABLISHED and was never answered. The process stayed alive and never
listened, so systemd - which watches for a process that exits - kept reporting
the service as active for twelve minutes.
"""

import asyncio

import pytest

from app.db import session as db_session


class _SilentConnection:
    """A connection that is opened and then never answers."""

    async def __aenter__(self):
        await asyncio.Event().wait()

    async def __aexit__(self, *exc_info):
        return False


class _SilentEngine:
    def connect(self):
        return _SilentConnection()


async def test_a_silent_database_fails_startup_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(db_session, "engine", _SilentEngine())
    monkeypatch.setattr(db_session, "STARTUP_VALIDATION_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(RuntimeError, match="did not finish"):
        await db_session.validate_runtime_database(db_session.settings.database_runtime_role)


async def test_the_wait_is_bounded_by_the_configured_timeout(monkeypatch):
    monkeypatch.setattr(db_session, "engine", _SilentEngine())
    monkeypatch.setattr(db_session, "STARTUP_VALIDATION_TIMEOUT_SECONDS", 0.05)

    started = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeError):
        await db_session.validate_runtime_database(db_session.settings.database_runtime_role)
    # Generous, because CI runners are not quiet machines. The point is that it
    # returns at all rather than waiting for the deploy's health window.
    assert asyncio.get_running_loop().time() - started < 5

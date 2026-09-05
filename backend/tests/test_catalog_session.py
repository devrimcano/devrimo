"""One catalog connection per request, opened only when something misses.

Both properties were learned by measuring rather than reasoning. Connecting to
the Course Info server costs seconds; reading a cached page costs nothing. A
first attempt gave every read its own connection and made a five-section course
*slower* than the full-agent spawn it replaced — 10.6s to 15.1s. Holding one
connection brought it to 6.5s, and opening it lazily kept a warm page free.
"""

import app.campus.course_info as course_info
from app.campus.course_info import CatalogSession, catalog_session


class _Toolkit:
    name = course_info.TOOLKIT_NAME
    functions: dict = {}


def _count_connections(monkeypatch) -> list[int]:
    """Replace the connect step with a counter, leaving the session logic real."""
    opened = [0]

    class _Recorder:
        async def __aenter__(self):
            opened[0] += 1
            return _Toolkit()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(course_info, "_catalog_toolkit", lambda db, user_id: _Recorder())
    return opened


async def test_a_session_that_is_never_used_never_connects(monkeypatch):
    """The common case: every page already cached, so nothing should spawn."""
    opened = _count_connections(monkeypatch)
    async with catalog_session(None, "user"):
        pass
    assert opened == [0]


async def test_repeated_reads_share_one_connection(monkeypatch):
    """A five-section course needs six reads and must still connect once."""
    opened = _count_connections(monkeypatch)
    async with catalog_session(None, "user") as session:
        for _ in range(6):
            await session.toolkit()
    assert opened == [1]


async def test_the_connection_is_closed_even_when_the_caller_raises(monkeypatch):
    opened = _count_connections(monkeypatch)
    session_seen: list[CatalogSession] = []
    try:
        async with catalog_session(None, "user") as session:
            session_seen.append(session)
            await session.toolkit()
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert opened == [1]
    # Closed: the stack is released, so a later use would open a fresh one.
    assert session_seen[0]._stack is None

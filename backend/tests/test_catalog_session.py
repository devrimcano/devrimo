"""One catalog connection per request, opened only when something misses.

Both properties were learned by measuring rather than reasoning. Connecting to
the Course Info server costs seconds; reading a cached page costs nothing. A
first attempt gave every read its own connection and made a five-section course
*slower* than the full-agent spawn it replaced — 10.6s to 15.1s. Holding one
connection brought it to 6.5s, and opening it lazily kept a warm page free.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock

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


async def test_parallel_session_reads_authorize_once(monkeypatch):
    authorize = AsyncMock()
    monkeypatch.setattr(course_info, "require_catalog_access", authorize)
    session = CatalogSession(None, uuid.uuid4())

    await asyncio.gather(*(session.authorize() for _ in range(20)))

    authorize.assert_awaited_once()


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


async def test_two_catalog_reads_for_one_student_never_overlap(monkeypatch):
    """The Course Info server holds one stateful SAIS session per student.

    Its course-listing flow reads the tokens for the next request out of the
    previous response, and which page the session is "on" lives server-side. Two
    calls in flight at once therefore land on each other's pages — and a wrong
    token returns the *previous* page rather than an error, so the symptom is a
    plausible-looking wrong answer rather than a failure anyone would notice.
    Two browser tabs, or a chat turn overlapping a planner request, was enough.

    Four different cache keys, deliberately: the single flight already collapses
    two reads of the *same* key and would hide the absence of a lock.
    """
    _count_connections(monkeypatch)
    monkeypatch.setattr(course_info, "require_catalog_access", AsyncMock())
    course_info._catalog.purge(lambda key: True)
    depth = [0]
    peak = [0]

    async def call_toolkit(db, user_id, toolkit, tool_suffix, values):
        depth[0] += 1
        peak[0] = max(peak[0], depth[0])
        try:
            # A real call awaits the subprocess; this is the point at which two
            # of them would interleave if nothing held them apart.
            await asyncio.sleep(0)
            return [values["course"]]
        finally:
            depth[0] -= 1

    async def read_cached(key_hash):
        return None

    async def write_cached(*args, **kwargs):
        return None

    monkeypatch.setattr(course_info, "_call_toolkit", call_toolkit)
    monkeypatch.setattr(course_info, "read_cached", read_cached)
    monkeypatch.setattr(course_info, "write_cached", write_cached)

    user_id = uuid.uuid4()
    async with catalog_session(None, user_id) as session:
        await asyncio.gather(
            *[
                course_info.call_course_info(
                    None,
                    user_id,
                    "get_course_info",
                    {"department": "236", "semester": "20252", "course": code},
                    session=session,
                )
                for code in ("2360111", "2360122", "2360133", "2360144")
            ]
        )
    assert peak == [1], "two catalog calls for one student were in flight at once"


async def test_waiting_request_does_not_block_current_session_next_read(monkeypatch):
    """A held integration lease must be acquired before the process call lock."""
    from contextlib import asynccontextmanager

    lease_lock = asyncio.Lock()
    waiting = asyncio.Event()

    @asynccontextmanager
    async def toolkit(db, user_id):
        if lease_lock.locked():
            waiting.set()
        async with lease_lock:
            yield _Toolkit()

    async def call(db, user_id, toolkit, suffix, values):
        return values

    monkeypatch.setattr(course_info, "_catalog_toolkit", toolkit)
    monkeypatch.setattr(course_info, "_call_toolkit", call)
    user_id = uuid.uuid4()
    async with catalog_session(None, user_id) as first:
        await course_info._invoke(None, user_id, "get_course_info", {"course": "first"}, first)
        competing = asyncio.create_task(course_info._invoke(None, user_id, "get_course_info", {"course": "other"}))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            result = await asyncio.wait_for(
                course_info._invoke(None, user_id, "get_course_info", {"course": "second"}, first), 1
            )
            assert result == {"course": "second"}
        except BaseException:
            competing.cancel()
            await asyncio.gather(competing, return_exceptions=True)
            raise
    assert await asyncio.wait_for(competing, 1) == {"course": "other"}

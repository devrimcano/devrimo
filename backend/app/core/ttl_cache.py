"""A bounded cache with expiry and per-key single flight.

A module-level ``dict`` used as a cache leaks by construction: entries are only
ever reclaimed when the *same* key is looked up again after its deadline, so a
key that is never requested a second time stays resident for the life of the
process. A per-user catalog cache keyed by course code is exactly that shape —
a student browsing forty courses leaves forty entries nothing will ever read
again.

This keeps the entries bounded two ways (expired ones are dropped on every
write, and the oldest are evicted past ``max_entries``) and collapses
concurrent misses on one key into a single upstream call, which the SAIS-backed
callers need far more than they need a per-user mutex: two lookups for
different courses have no reason to serialise, and two lookups for the *same*
course have no reason to both run.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable

_MISSING = object()


class TTLCache:
    """Cache of at most ``max_entries`` values, each valid for ``ttl_seconds``."""

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[Hashable, tuple[float, object]] = OrderedDict()
        self._flights: dict[Hashable, asyncio.Future] = {}

    def get(self, key: Hashable, default: object = None) -> object:
        entry = self._entries.get(key)
        if entry is None:
            return default
        stored_at, value = entry
        if time.monotonic() - stored_at > self._ttl:
            del self._entries[key]
            return default
        self._entries.move_to_end(key)
        return value

    def set(self, key: Hashable, value: object) -> None:
        self._drop_expired()
        self._entries[key] = (time.monotonic(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def purge(self, matches: Callable[[Hashable], bool]) -> None:
        """Forget every entry whose key satisfies ``matches``.

        Deleting a student's stored academic data has to reach the cache too,
        or their department keeps being served from memory after they asked for
        it to be removed.
        """
        for key in [key for key in self._entries if matches(key)]:
            del self._entries[key]

    async def run(self, key: Hashable, factory: Callable[[], Awaitable]) -> object:
        """Return the value for ``key``, calling ``factory`` at most once.

        Callers that arrive while a fill is in flight wait on that fill rather
        than starting their own. The fill is shielded, so a client that gives up
        does not cancel the work the remaining waiters are still waiting for.
        """
        cached = self.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        flight = self._flights.get(key)
        if flight is None:
            flight = asyncio.ensure_future(self._fill(key, factory))
            self._flights[key] = flight
        return await asyncio.shield(flight)

    async def _fill(self, key: Hashable, factory: Callable[[], Awaitable]) -> object:
        try:
            value = await factory()
            self.set(key, value)
            return value
        finally:
            # Stored before the flight is forgotten, so the next caller sees a
            # hit rather than starting a second fill for the same key.
            self._flights.pop(key, None)

    def _drop_expired(self) -> None:
        now = time.monotonic()
        for key in [key for key, (stored_at, _) in self._entries.items() if now - stored_at > self._ttl]:
            del self._entries[key]

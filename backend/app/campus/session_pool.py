"""Leased campus sessions independent of model residency.

Each worker task owns its MCP connect/call/close lifecycle, including AnyIO
cancel scopes. Callers receive proxies, never credential-bearing toolkits.
"""

import asyncio
import inspect
from types import SimpleNamespace

from fastapi import HTTPException

from app.campus.toolkits import close_toolkits, connect_campus_toolkits
from app.config import get_settings


class SessionEntry:
    def __init__(self, specs, revision):
        self.revision = revision
        self.active_leases = 1
        self.retired = False
        self.queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.run(specs))

    async def run(self, specs):
        connected = []
        try:
            connected = await connect_campus_toolkits(specs, timeout_seconds=get_settings().campus_mcp_timeout_seconds)
            if not connected:
                raise HTTPException(502, "Integration is unavailable")
            proxies = []
            for toolkit in connected:
                functions = {}
                for name, function in (toolkit.functions or {}).items():

                    async def invoke(_name=name, _toolkit=toolkit, **arguments):
                        return await self.call(_toolkit, _name, arguments)

                    functions[name] = SimpleNamespace(parameters=function.parameters, entrypoint=invoke)
                proxies.append(SimpleNamespace(name=toolkit.name, functions=functions))
            self.ready.set_result(proxies)
            while True:
                try:
                    command = await asyncio.wait_for(
                        self.queue.get(), timeout=get_settings().campus_session_idle_seconds
                    )
                except TimeoutError:
                    if self.active_leases == 0:
                        break
                    continue
                if command is None:
                    break
                toolkit, name, arguments, future = command
                try:
                    result = toolkit.functions[name].entrypoint(**arguments)
                    if inspect.isawaitable(result):
                        result = await result
                    if not future.done():
                        future.set_result(result)
                except BaseException as exc:
                    if not future.done():
                        future.set_exception(exc)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            await close_toolkits(connected)
            while not self.queue.empty():
                command = self.queue.get_nowait()
                if command is not None and not command[-1].done():
                    command[-1].set_exception(HTTPException(502, "Integration session closed"))

    async def call(self, toolkit, name, arguments):
        if self.task.done():
            raise HTTPException(502, "Integration session expired")
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((toolkit, name, arguments, future))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Keep the lease until the in-flight operation stops, preventing a
            # second process from racing an operation after client cancellation.
            try:
                await asyncio.shield(future)
            finally:
                raise

    async def close(self):
        if not self.task.done():
            await self.queue.put(None)
            await self.task


_entries = {}


async def acquire(user_id, tool_id, specs, revision):
    key = (user_id, tool_id)
    # The caller holds a PostgreSQL user/integration lock across this lease.
    # No second acquisition for this key can reach here concurrently.
    entry = _entries.get(key)
    if entry is not None and (entry.task.done() or entry.retired or entry.revision != revision):
        await entry.close()
        _entries.pop(key, None)
        entry = None
    if entry is None:
        # Bound idle credential-bearing processes independently of AgentPool.
        limit = get_settings().campus_session_max_size
        if len(_entries) >= limit:
            for old_key, old in list(_entries.items()):
                if old.active_leases == 0:
                    await old.close()
                    _entries.pop(old_key, None)
                    break
            else:
                raise HTTPException(503, "Campus session capacity is busy")
        entry = SessionEntry(specs, revision)
        _entries[key] = entry

        def discard_finished(_task):
            if _entries.get(key) is entry:
                _entries.pop(key, None)

        entry.task.add_done_callback(discard_finished)
    else:
        entry.active_leases += 1
    try:
        return entry, await asyncio.shield(entry.ready)
    except BaseException:
        entry.active_leases -= 1
        await entry.close()
        _entries.pop(key, None)
        raise


async def invalidate(user_id, tool_id):
    entry = _entries.pop((user_id, tool_id), None)
    if entry is not None:
        await entry.close()


async def close_all():
    entries = list(_entries.values())
    _entries.clear()
    await asyncio.gather(*(entry.close() for entry in entries))


async def retire_user(user_id):
    """Credential changes retire leased sessions and immediately close idle ones."""
    for key, entry in list(_entries.items()):
        if key[0] != user_id:
            continue
        entry.retired = True
        if entry.active_leases == 0:
            await entry.close()
            if _entries.get(key) is entry:
                _entries.pop(key, None)


def session_count(user_id=None):
    """Live integration subprocess sessions, not resident model agents."""
    return sum(not entry.task.done() and (user_id is None or key[0] == user_id) for key, entry in _entries.items())


def session_capacity():
    return get_settings().campus_session_max_size

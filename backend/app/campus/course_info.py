"""Token-free reads of the Course Info MCP server.

The schedule builder needs the METU catalog, not an opinion about it: which
courses a department offers this term, which sections a course has, which
department a code belongs to. All of that is already exposed by the connected
Course Info server, so these helpers call its functions directly — no Agent
run, no model, no prompt, no tokens.

Owning the cache here rather than in the HTTP layer is what lets
``DELETE /student/academic-data`` actually reach it: a student who asks for
their stored data to be removed should not keep being served their department
from a dict inside a request handler.
"""

import asyncio
import inspect
import re
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pool import get_pool
from app.agents.toolset import close_toolkits, connect_campus_toolkits
from app.campus import departments as department_directory
from app.campus import service as campus_service
from app.campus.mcp_results import mcp_payload, parse_json_document
from app.config import get_settings
from app.core.digest import stable_digest
from app.core.persistent_cache import read_cached, read_many_cached, write_cached
from app.core.ttl_cache import TTLCache
from app.logging import get_logger

logger = get_logger(__name__)

TOOLKIT_NAME = "campus:course_info"

# Distinguishable from any value a cache could legitimately hold, including
# None -- a tool that answers null is not a miss.
_UNCACHED = object()

# One catalog call at a time per student.
#
# The Course Info server keeps a single SAIS client with a single cookie jar,
# and SAIS's course-listing flow is stateful on its side: the tokens for the
# next request are read out of the previous response, and which page you are
# "on" lives in the PHP session. Two calls in flight on one student therefore
# land on each other's pages — and the failure is silent, because a wrong token
# returns the *previous* page rather than an error. Two browser tabs, or a chat
# turn overlapping a planner request, was enough.
#
# Cache hits never reach here, so this serialises campus traffic only, and it
# bounds a student to one request against METU at a time by construction.
_campus_locks: dict[UUID, asyncio.Lock] = {}
_lock_registry = asyncio.Lock()


async def _campus_lock(user_id: UUID) -> asyncio.Lock:
    async with _lock_registry:
        return _campus_locks.setdefault(user_id, asyncio.Lock())

# Catalog data changes when the registrar publishes, not between page loads, so
# a quarter hour is generous; the bound exists so a long-lived worker's cache
# cannot grow with every course any student has ever opened.
_catalog = TTLCache(ttl_seconds=15 * 60, max_entries=4096)

CATALOG_NAMESPACE = "course-catalog"

# Tools whose answer depends only on the catalog, never on who is asking, and
# may therefore be cached once and served to every student.
#
# An allowlist rather than a denylist, and keyed by exact tool name: the two
# ``get_student_*`` tools return one student's curriculum, and a scheme where a
# newly added tool is shared until somebody remembers to exclude it is a
# cross-student data leak waiting for an upstream release. Anything not named
# here stays per-user and in-process.
#
# The lifetimes come from how the registrar actually behaves. Department and
# course names are fixed for a term once published. Section rows — times,
# rooms, instructors, the critical-info notes — do get edited during add-drop,
# so they are re-read weekly rather than held for the term.
_SHARED_TOOL_TTLS: dict[str, float] = {
    "get_departments_and_semesters": 30 * 24 * 3600,
    "search_departments": 30 * 24 * 3600,
    "list_program_courses": 30 * 24 * 3600,
    "get_course_prerequisites": 30 * 24 * 3600,
    "get_course_replacements": 30 * 24 * 3600,
    "get_thesis_courses": 30 * 24 * 3600,
    "get_course_info": 7 * 24 * 3600,
    # Same lifetime as the sections it describes, and shared for the same
    # reason: a section's eligibility table is a property of the section, not
    # of whoever asked. One student opening a course pays for everyone.
    "get_section_constraints": 7 * 24 * 3600,
}

_THREE_DIGITS = re.compile(r"^\d{3}$")
_ALPHA_PREFIX = re.compile(r"^[A-Z]{2,6}")

# Every Course Info tool names its arguments slightly differently across
# versions. Listed most specific first so a schema carrying both ``category``
# and a generic ``name`` binds the value to the one that means it.
_ARGUMENT_ALIASES = {
    "department": ("department", "department_code", "dept", "dept_code", "program", "program_code"),
    "semester": ("semester", "semester_code", "term", "term_code"),
    "course": ("course", "course_code", "code"),
    "section": ("section", "section_number", "section_no", "sec"),
    "query": ("query", "keyword", "search", "name"),
    "category": ("category", "category_id", "category_code", "code", "id", "name"),
}


def catalog_key(tool_suffix: str, values: dict[str, str]) -> tuple[tuple[str, ...], str]:
    """The identity and digest a shared catalog answer is stored under.

    One formula, in one place. It used to be spelled out independently in three
    modules — here, the overnight warmer, and the search endpoint — all of which
    have to agree exactly or the warmer fills a cache nobody reads.
    """
    identity = (tool_suffix, *(f"{key}={value}" for key, value in sorted(values.items())))
    return identity, stable_digest({"namespace": CATALOG_NAMESPACE, "identity": list(identity)})


async def prefetch(pairs: Iterable[tuple[str, dict[str, str]]]) -> None:
    """Seed the in-process cache for many shared answers from one database read.

    A batch endpoint otherwise probes the persistent layer once per course, and
    each probe opens its own session: forty courses meant forty connection
    checkouts before a single campus call. Answers already in memory are left
    alone, and a miss here is simply a miss — the caller fetches it as usual.

    Silent by design about anything not shared: a per-student tool has no
    business in a cache keyed without the student.
    """
    wanted: dict[str, tuple[str, ...]] = {}
    for tool_suffix, values in pairs:
        if tool_suffix not in _SHARED_TOOL_TTLS:
            continue
        identity, key_hash = catalog_key(tool_suffix, values)
        if _catalog.get(identity, _UNCACHED) is not _UNCACHED:
            continue
        wanted[key_hash] = identity
    if not wanted:
        return
    for key_hash, payload in (await read_many_cached(list(wanted))).items():
        _catalog.set(wanted[key_hash], payload)


def forget_user(user_id: UUID) -> None:
    """Drop every cached catalog answer belonging to one student."""
    prefix = str(user_id)
    _catalog.purge(lambda key: isinstance(key, tuple) and bool(key) and key[0] == prefix)
    # The registry holds one small lock per student who has ever read the
    # catalog, so it grows with the roll rather than with what they browsed.
    # Dropped here anyway: a student who asked to be forgotten should not leave
    # an object behind with their id as its key.
    _campus_locks.pop(user_id, None)


def json_value(value: Any) -> Any:
    """Coerce an unwrapped MCP payload into plain JSON-serialisable data."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, str):
        parsed = parse_json_document(value)
        return parsed if isinstance(parsed, str) else json_value(parsed)
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


class CatalogSession:
    """One Course Info connection, held open across a burst of related reads.

    Opening a page of the catalog is cheap; *connecting* to the server is not.
    A course with five sections needs six reads, and giving each its own
    connection spent more on spawning than the whole request had cost before.

    Opened lazily and at most once. A page whose data is already cached never
    connects at all, which is the common case and must stay free.
    """

    __slots__ = ("_db", "_user_id", "_stack", "_toolkit")

    def __init__(self, db: AsyncSession, user_id: UUID) -> None:
        self._db = db
        self._user_id = user_id
        self._stack: AsyncExitStack | None = None
        self._toolkit: Any = None

    async def toolkit(self) -> Any:
        if self._stack is None:
            self._stack = AsyncExitStack()
            self._toolkit = await self._stack.enter_async_context(_catalog_toolkit(self._db, self._user_id))
        return self._toolkit

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None


@asynccontextmanager
async def catalog_session(db: AsyncSession, user_id: UUID) -> AsyncIterator[CatalogSession]:
    """Share one catalog connection across several reads in the same request."""
    session = CatalogSession(db, user_id)
    try:
        yield session
    finally:
        await session.aclose()


async def call_course_info(
    db: AsyncSession,
    user_id: UUID,
    tool_suffix: str,
    values: dict[str, str],
    *,
    session: CatalogSession | None = None,
) -> Any:
    """Call one Course Info function and return its payload as plain data.

    Three layers, cheapest first: the in-process cache collapses a burst of
    identical lookups, the database cache survives restarts and is shared
    across students for catalog tools, and only a miss on both reaches the
    campus server — which costs that student's whole toolkit being spawned.

    Student-specific tools are absent from :data:`_SHARED_TOOL_TTLS`, so their
    keys stay scoped to the user and never touch the shared layer.

    Pass ``session`` when a request makes several of these calls, so they share
    one connection instead of spawning a campus server each.
    """
    shared_ttl = _SHARED_TOOL_TTLS.get(tool_suffix)
    identity, key_hash = catalog_key(tool_suffix, values)
    # A shared answer must not be keyed by the student who happened to ask for
    # it first, or every account would still pay for its own copy.
    memory_key = identity if shared_ttl else (str(user_id), *identity)

    # Which layer answered. Read here rather than set from inside ``load``,
    # because a caller that joins a fill already in flight never runs ``load``
    # at all and would otherwise be recorded as a memory hit when it in fact
    # waited on the campus.
    source = "memory" if _catalog.get(memory_key, _UNCACHED) is not _UNCACHED else "flight"

    async def load() -> Any:
        nonlocal source
        if shared_ttl is None:
            source = "campus"
            return await _invoke(db, user_id, tool_suffix, values, session)
        cached = await read_cached(key_hash)
        if cached is not None:
            source = "database"
            return cached
        source = "campus"
        value = await _invoke(db, user_id, tool_suffix, values, session)
        # owner_hash stays None: this row belongs to the catalog, not to the
        # student who triggered the fetch, so erasing their data must not
        # delete a course list every other student is reading.
        await write_cached(key_hash, value, namespace=CATALOG_NAMESPACE, ttl_seconds=shared_ttl)
        return value

    started = time.monotonic()
    try:
        return await _catalog.run(memory_key, load)
    finally:
        # ``tool`` and ``duration_ms`` deliberately carry the same names as
        # ``agent_tool_completed``: the catalog is being taken off the agent's
        # tool path, and one journald query has to span both sides of that
        # change for the before and after to be comparable at all. Argument
        # *values* are never logged -- a course code is fine, but these dicts
        # also carry the student's own programme and category ids.
        logger.info(
            "catalog_tool_completed",
            tool=tool_suffix,
            duration_ms=round((time.monotonic() - started) * 1000),
            source=source,
            shared=shared_ttl is not None,
        )


@asynccontextmanager
async def _catalog_toolkit(db: AsyncSession, user_id: UUID) -> AsyncIterator[Any]:
    """The Course Info toolkit, without paying for the student's whole agent.

    Reading a public course page used to go through the agent pool, which
    brings up *every* campus server the student has enabled — SAIS, ODTUClass,
    webmail — because that is what a chat turn needs. Measured cold, that was
    seven seconds to fetch one cached-for-a-week catalog page, and three of the
    four subprocesses were never called.

    So: reuse the resident agent when the student already has one up, since a
    chat turn has already paid for it and its connection is warm. Otherwise
    connect the catalog server alone and close it on the way out.

    The reuse path holds no lease, so a concurrent eviction could in principle
    close the toolkit mid-call. That is exactly the exposure the previous code
    had; leasing cannot fix it here because acquiring a lease builds the whole
    agent when one is not already resident, which is the cost being avoided.
    """
    resident = get_pool().get(user_id)
    if resident is not None:
        toolkit = next((item for item in resident.toolkits if item.name == TOOLKIT_NAME), None)
        if toolkit is not None:
            yield toolkit
            return

    specs = [spec for spec in await campus_service.campus_server_specs(db, user_id) if spec.tool_id == "course_info"]
    if not specs:
        yield None
        return
    settings = get_settings()
    connected = await connect_campus_toolkits(
        specs, timeout_seconds=settings.campus_catalog_timeout_seconds
    )
    try:
        yield next((item for item in connected if item.name == TOOLKIT_NAME), None)
    finally:
        await close_toolkits(connected)


async def _invoke(
    db: AsyncSession,
    user_id: UUID,
    tool_suffix: str,
    values: dict[str, str],
    session: CatalogSession | None = None,
) -> Any:
    """One call into the student's Course Info server, and never two at once.

    The lock is taken *here*, inside the single-flight factory, and never around
    ``_catalog.run``. The order is always flight-then-lock: callers waiting on
    one fill share it and only the leader ever holds the lock. Inverting that —
    taking the lock before entering the flight — deadlocks the whole catalog,
    and nothing in the type system says so, which is why it is written here.
    """
    async with await _campus_lock(user_id):
        if session is not None:
            return await _call_toolkit(db, user_id, await session.toolkit(), tool_suffix, values)
        async with _catalog_toolkit(db, user_id) as toolkit:
            return await _call_toolkit(db, user_id, toolkit, tool_suffix, values)


async def _call_toolkit(
    db: AsyncSession, user_id: UUID, toolkit: Any, tool_suffix: str, values: dict[str, str]
) -> Any:
    if toolkit is None:
        # Two different situations, and the old wording only described one of
        # them. A student who never enabled the catalog and a student whose
        # catalog server failed to start both landed on "not enabled", which
        # sends the second one to a settings page where everything looks fine.
        credential = await campus_service.get_credential(db, user_id)
        enabled = "course_info" in campus_service.enabled_tool_ids(credential)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The course catalog server did not start. This is usually temporary — try again in a moment."
            if enabled
            else "Course catalog access is not enabled. Turn it on in Settings to use the catalog.",
        )

    function_name = f"course_info_{tool_suffix}"
    function = (toolkit.functions or {}).get(function_name)
    if function is None or function.entrypoint is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Course Info tool is unavailable: {tool_suffix}")

    arguments = _tool_arguments(function, values)
    try:
        result = function.entrypoint(**arguments)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Course catalog request failed: {exc}") from exc
    return json_value(mcp_payload(result))


def _tool_arguments(function, values: dict[str, str]) -> dict[str, str]:
    properties = (function.parameters or {}).get("properties", {})
    arguments: dict[str, str] = {}
    for value_name, value in values.items():
        for candidate in _ARGUMENT_ALIASES[value_name]:
            if candidate in properties:
                arguments[candidate] = value
                break
    missing = set((function.parameters or {}).get("required", [])) - arguments.keys()
    if missing:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Course Info tool schema is unsupported; missing arguments: {', '.join(sorted(missing))}",
        )
    return arguments


# --- department identity ----------------------------------------------------


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _department_candidates(value: Any) -> list[tuple[str, str]]:
    """Every ``(code, name)`` pair a departments payload explicitly labels."""
    records: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        records.append(item)
        for child in item.values():
            visit(child)

    visit(value)

    candidates: list[tuple[str, str]] = []
    for record in records:
        code = ""
        name = ""
        for key, item in record.items():
            if not isinstance(item, (str, int)):
                continue
            normalized_key = _normalized(str(key))
            text = str(item).strip()
            if not code and "code" in normalized_key and _THREE_DIGITS.match(text):
                code = text
            if not name and "name" in normalized_key and text:
                name = text
        if code:
            candidates.append((code, name))
    return list(dict.fromkeys(candidates))


def department_options(value: Any) -> list[dict[str, str]]:
    """Every ``{code, name}`` a departments payload explicitly labels.

    The picker the schedule page shows when SAIS reports no department needs a
    list, not a single resolution. It goes through the same extraction
    :func:`department_code` uses, so the frontend never has to scan a payload
    for three-digit runs — which is how it used to end up offering a student id
    or a row count as a department.
    """
    return [{"code": code, "name": name or code} for code, name in _department_candidates(value)]


def department_code(value: Any, query: str) -> str | None:
    """The three-digit code of the department ``query`` names, or ``None``.

    Only a field explicitly labelled as a code is ever read, and only when it
    is exactly three digits. Scanning a payload for any three-digit run — which
    is what the frontend used to do — happily returns a student id, a year or a
    row count and then quietly serves someone else's course list.

    Ambiguity resolves to ``None`` rather than to a guess, for the same reason:
    the wrong department produces a completely plausible-looking answer, so the
    caller has to be able to tell that we did not know.
    """
    candidates = _department_candidates(value)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]
    wanted = _normalized(query)
    if not wanted:
        return None
    for match in (
        lambda name: name == wanted,
        lambda name: name.startswith(wanted),
        lambda name: wanted in name,
    ):
        hits = {code for code, name in candidates if match(_normalized(name))}
        if len(hits) == 1:
            return hits.pop()
    return None


async def resolve_department(
    db: AsyncSession, user_id: UUID, query: str, *, session: CatalogSession | None = None
) -> str | None:
    """Look a department name or abbreviation up in the catalog itself."""
    if not query.strip():
        return None
    departments = await call_course_info(
        db, user_id, "search_departments", {"query": query.strip()}, session=session
    )
    return department_code(departments, query)


async def department_for_course(
    db: AsyncSession,
    user_id: UUID,
    compact_course: str,
    home_department: str,
    *,
    session: CatalogSession | None = None,
) -> str:
    """The department that owns ``compact_course``.

    A full seven-digit METU code carries its owning department in the first
    three digits. An alphabetic code (``MATH260``) does not, and assuming the
    student's own department turns a service course into a lookup for a course
    that does not exist — a CENG student opening MATH 260 would be asking for
    5710260. The prefix is resolved through the catalog's own department search
    instead of guessed, and an unresolvable prefix is reported rather than
    silently answered from the wrong department.
    """
    if compact_course.isdigit() and len(compact_course) == 7:
        return compact_course[:3]
    prefix = _ALPHA_PREFIX.match(compact_course)
    if prefix is None:
        return home_department
    # Our own directory first. It is keyed by abbreviation, restricted to the
    # Ankara campus, and answers without a network call.
    #
    # The catalog's search matches department *names*, so an abbreviation
    # arrives as several departments at once: "HIST" returns History, History
    # of Architecture and History (Kuzey Kıbrıs Kampüsü), and "PHYS" returns
    # seven including Astrophysics and the Cyprus Physics department. Three
    # hits is ambiguous, ambiguous returns None, and None raises — which is
    # why every lettered course code failed to resolve.
    known = department_directory.resolve(prefix.group(0))
    if known is not None:
        return known.code
    # Still fall back to the catalog for anything our directory omits, such as
    # a Cyprus or joint programme a student legitimately asks for.
    resolved = await resolve_department(db, user_id, prefix.group(0), session=session)
    if resolved is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Could not identify which department owns {compact_course}. "
            "Use the course's full seven-digit METU code.",
        )
    return resolved

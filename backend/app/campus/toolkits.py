"""Turn a student's campus server specs into connected Agno toolkits.

This is the module that actually spawns campus MCP servers. Everything it
launches is a subprocess of the broker, so two properties matter and are
enforced here rather than left to a caller:

* **One env per server.** Each :class:`~agno.tools.mcp.MCPTools` is built from
  its own :class:`mcp.StdioServerParameters`, so a server sees only the
  credentials its catalog entry asked for. The MCP SDK merges that env over
  ``get_default_environment()`` — ``HOME``/``LOGNAME``/``PATH``/``SHELL``/
  ``TERM``/``USER`` only — so the broker's own ``SECRET_ENCRYPTION_KEY``,
  ``DATABASE_URL``, and model-provider key are never inherited by a scraper.

* **``server_params`` and never ``command``.** ``MCPTools.__init__`` rebuilds
  ``server_params`` from scratch when ``command`` is also passed, silently
  dropping ``cwd`` — which odtuclass needs, because it caches its Moodle
  session token relative to its working directory.
"""

import asyncio
from pathlib import Path

from agno.tools.mcp import MCPTools
from mcp import StdioServerParameters

from app.campus.mcp_config import CampusServerSpec
from app.logging import get_logger
from app.observability import capture, capture_exception
from app.observability.flags import enabled_campus_tool_ids

logger = get_logger(__name__)

TOOLKIT_INSTRUCTIONS = {
    "sais": "Use for the student's private schedule, transcript, CGPA, and SAIS announcements.",
    "course_info": (
        "Use for official catalog, prerequisite, replacement, and curriculum questions. "
        "Use the first-party plan_semester tool for offering eligibility and schedule optimization."
    ),
    "odtuclass": "Use for enrolled-course announcements, syllabi, labs, and assignment deadlines.",
    "webmail": "Use only for explicit mail requests; send and reply always require confirmation.",
}


def _prepare_working_dir(path: str) -> bool:
    """Create this server's private working directory. False if we cannot.

    0700: these directories hold cached campus session tokens, and on a
    single-tenant broker host the only thing standing between two students'
    caches is the directory mode.

    Unwritable is a deployment condition, not a bug to crash on:
    ``CAMPUS_STATE_ROOT`` defaults to a path the image creates, so a dev
    machine or a misconfigured volume hits this and must still get an agent.
    """
    try:
        Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        logger.warning("campus_state_dir_unavailable", path=path, error=str(exc))
        capture_exception(exc, path=path, **{"$exception_fingerprint": ["campus_state_dir_unavailable"]})
        return False
    return True


def build_toolkits(specs: list[CampusServerSpec], *, timeout_seconds: int) -> list[MCPTools]:
    """Construct (but do not connect) one toolkit per campus server.

    Skips any server whose working directory cannot be prepared, on the same
    reasoning as :func:`connect_toolkits`: one unavailable campus tool must not
    cost the student their whole agent.
    """
    # A remote kill switch for a campus server that has started misbehaving.
    # With no flag set this returns every spec unchanged, so the student's own
    # tool selection remains the only thing that decides.
    allowed = enabled_campus_tool_ids([spec.tool_id for spec in specs])
    if len(allowed) != len(specs):
        blocked = [spec.tool_id for spec in specs if spec.tool_id not in allowed]
        logger.warning("campus_servers_disabled_by_flag", servers=blocked)
        capture("campus_servers_disabled_by_flag", servers=blocked)
        specs = [spec for spec in specs if spec.tool_id in allowed]

    toolkits: list[MCPTools] = []
    for spec in specs:
        if spec.cwd and not _prepare_working_dir(spec.cwd):
            # Skipped rather than launched without a cwd. A server that caches
            # a session token relative to its working directory would otherwise
            # write it into the broker's own cwd — shared by every student.
            logger.warning("campus_server_skipped", server=spec.tool_id, reason="state_dir_unavailable")
            continue
        toolkits.append(build_toolkit(spec, timeout_seconds=timeout_seconds))
    return toolkits


def build_toolkit(spec: CampusServerSpec, *, timeout_seconds: int) -> MCPTools:
    """One unconnected toolkit for one server.

    Separate from :func:`build_toolkits` because a toolkit whose connect failed
    cannot be reused — the SDK leaves it half-initialised — so a retry has to
    construct a fresh one from the same spec.
    """
    return MCPTools(
        # Named for the tool so a failure is traceable to one server.
        name=f"campus:{spec.tool_id}",
        server_params=StdioServerParameters(
            command=spec.command,
            args=list(spec.args),
            env=dict(spec.env),
            cwd=spec.cwd,
        ),
        transport="stdio",
        timeout_seconds=timeout_seconds,
        # The four servers were written independently and several use
        # generic names (``search``, ``list_courses``); without this a
        # collision would shadow one server's tool with another's.
        tool_name_prefix=spec.tool_id,
        include_tools=list(spec.include_tools) or None,
        requires_confirmation_tools=list(spec.requires_confirmation_tools),
        instructions=TOOLKIT_INSTRUCTIONS.get(spec.tool_id),
        add_instructions=True,
    )


def _is_connected(toolkit: MCPTools) -> bool:
    """Whether a toolkit actually came up.

    ``MCPTools.connect()`` logs and swallows every failure rather than raising
    — a server whose interpreter is missing entirely returns normally from
    ``connect()`` — so the return value says nothing. The SDK's public
    ``initialized`` property is the honest signal, and an unconnected toolkit handed to an Agent
    advertises no tools while still being asked about on every run.
    """
    return toolkit.initialized


async def connect_toolkits(toolkits: list[MCPTools]) -> list[MCPTools]:
    """Connect each toolkit, dropping any that fails.

    One campus server being down is not a reason to deny the student their
    agent — the persona already tells the model to say so plainly when a tool
    it expected is missing. The rest still come up.
    """
    connected: list[MCPTools] = []
    for toolkit in toolkits:
        try:
            await toolkit.connect()
        except Exception as exc:  # defensive: connect() is documented not to raise
            logger.warning("campus_server_connect_failed", server=toolkit.name, error=str(exc))
            # Grouped by server, not by stack: every one of these is "this
            # campus server is down", and one issue per server is the useful
            # shape. Without this the student simply finds the agent quietly
            # unable to do something it could do yesterday.
            capture_exception(
                exc,
                server=toolkit.name,
                **{"$exception_fingerprint": ["campus_server_connect_failed", str(toolkit.name)]},
            )
            continue
        if not _is_connected(toolkit):
            # No exception to attach: MCPTools.connect() swallows its own
            # failures, so this silent case gets an event of its own.
            logger.warning("campus_server_unavailable", server=toolkit.name)
            capture("campus_server_unavailable", server=toolkit.name)
            await close_toolkits([toolkit])
            continue
        connected.append(toolkit)
        logger.info("campus_server_connected", server=toolkit.name, tools=len(toolkit.functions or {}))
    return connected


async def close_toolkits(toolkits: list[MCPTools]) -> None:
    """Close every toolkit, and never raise.

    Called from eviction and shutdown paths where the caller has nothing useful
    to do about a failure and the remaining toolkits still need closing — a
    leaked subprocess holds a student's credentials in memory.
    """
    for toolkit in toolkits:
        try:
            await toolkit.close()
        except (Exception, asyncio.CancelledError) as exc:
            # A subprocess that would not close is still holding a student's
            # METU credentials in its environment.
            logger.warning("campus_server_close_failed", server=toolkit.name, error=str(exc))
            capture_exception(
                exc,
                server=toolkit.name,
                **{"$exception_fingerprint": ["campus_server_close_failed", str(toolkit.name)]},
            )


# One extra attempt, not a loop. The observed failure is a server that
# handshakes in about a second normally and occasionally does not come up at
# all during a burst of agent rebuilds — three times in a hundred connects,
# with no stderr from the process and no exception the SDK will surrender.
# A single immediate retry costs one second in the rare bad case and turns a
# student's whole catalog off in none of them. More attempts would start
# hiding a server that is genuinely down behind a slow agent build.
CONNECT_ATTEMPTS = 2


async def connect_campus_toolkits(
    specs: list[CampusServerSpec], *, timeout_seconds: int, attempts: int = CONNECT_ATTEMPTS
) -> list[MCPTools]:
    """Connect every server, retrying one that fails to come up.

    Returns the toolkits that did connect, in the order their specs were given.
    A server that fails every attempt is dropped exactly as before: the student
    keeps the rest of their agent.
    """
    connected: list[MCPTools] = []
    for spec in specs:
        for attempt in range(1, attempts + 1):
            toolkits = build_toolkits([spec], timeout_seconds=timeout_seconds)
            if not toolkits:
                break  # working directory unavailable; already logged
            live = await connect_toolkits(toolkits)
            if live:
                if attempt > 1:
                    logger.info("campus_server_connected_on_retry", server=spec.tool_id, attempt=attempt)
                    capture("campus_server_connected_on_retry", server=spec.tool_id, attempt=attempt)
                connected.extend(live)
                break
            if attempt < attempts:
                logger.warning("campus_server_retrying", server=spec.tool_id, attempt=attempt)
    return connected

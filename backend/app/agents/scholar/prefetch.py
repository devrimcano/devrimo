"""The one read a question almost always needs, done inside the durable run.

A model step is the expensive unit of a turn, so the data for a prerequisites,
credits/sections or eligibility question is fetched before the model wakes up
and handed to it as ``dependencies.prefetched``. Three constraints shape where
and how this runs:

* In the worker, after the turn is durably queued and leased, through the same
  authenticated workspace path the tools use - never in the API request that is
  still assembling itself.
* Only against the published catalog, which is a local read. On the legacy path
  a cache miss can open Course Info, and a prefetch must not block a turn on
  campus latency.
* Projected and bounded exactly like a tool result, because it is re-sent on
  every later model step of the turn.

Failures are swallowed: a prefetch that fails must never fail the turn it was
meant to speed up.
"""

from app.agents.scholar.results import bound, project

# The read each intent almost always needs, and only when the message names
# exactly one course: a prefetch that guesses wrong costs more than the model
# step it saves.
_PREFETCH_BY_INTENT = {
    "prerequisites": ("catalog.prerequisites",),
    "credits": ("catalog.sections",),
    "sections": ("catalog.sections",),
    "eligibility": ("catalog.eligibility",),
}


def _available() -> bool:
    from app.config import get_settings
    from app.planning.catalog_service import published_catalog_reads_enabled

    settings = get_settings()
    if not getattr(settings, "scholar_prefetch_enabled", True):
        return False
    if not published_catalog_reads_enabled():
        return False
    return settings.database_runtime_role == "assistant" and bool(settings.workspace_gateway_url)


def _scope(dependencies: dict) -> dict | None:
    """What to read, or None when the question does not justify a prefetch.

    A term named in a form that cannot be resolved ("2026 güz") skips every
    kind. Eligibility is skipped unless the message names one section: the
    source aggregates all of them otherwise, and a prefetched aggregate answers
    a question the student did not ask.
    """
    intent = dependencies.get("intent")
    kinds = _PREFETCH_BY_INTENT.get(intent)
    courses = (dependencies.get("current_focus") or {}).get("courses") or []
    scope = dependencies.get("requested_scope") or {}
    if not kinds or len(courses) != 1 or scope.get("term_unresolved"):
        return None
    if intent == "eligibility" and not scope.get("section"):
        return None
    return {
        "kinds": kinds,
        "course": courses[0],
        "term": scope.get("term"),
        "section": scope.get("section"),
    }


async def prefetch_dependencies(dependencies: dict, *, client=None) -> dict:
    """Return the dependencies with a bounded ``prefetched`` list added.

    ``client`` is injectable so the decision and bounding logic can be tested
    without the workspace gateway or a database.
    """
    if not dependencies or not _available():
        return dependencies
    plan = _scope(dependencies)
    if plan is None:
        return dependencies
    if client is None:
        from app.config import get_settings
        from app.workspace.client import WorkspaceClient

        client = WorkspaceClient(get_settings().workspace_gateway_url)

    from fastapi import HTTPException

    from app.workspace.resources import ResourceRef

    entries: list[dict] = []
    for kind in plan["kinds"]:
        reference = ResourceRef(
            kind=kind,
            key=plan["course"],
            term=plan["term"],
            section=plan["section"] if kind == "catalog.eligibility" else None,
        )
        try:
            result = await client.read(reference)
        except HTTPException as exc:
            # A 404 is the answer: "not available in the published release" is
            # final, and handing it over up front ends the search before a tool
            # call spends a model step discovering it.
            if exc.status_code == 404:
                entries.append({"kind": kind, "key": plan["course"], "error": str(exc.detail)})
            continue
        except Exception:
            continue
        if not isinstance(result, dict):
            continue
        entries.append(
            {
                "kind": kind,
                "key": plan["course"],
                "data": bound(project(result.get("data"))),
                "provenance": project(result.get("provenance")),
            }
        )
    if not entries:
        return dependencies
    return {**dependencies, "prefetched": entries}

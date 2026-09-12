"""Prefetch correctness: the term and section the message named, and the bound.

A prefetch that reads the active term for a question about 20252 answers a
question the student did not ask; one that aggregates every section's
eligibility answers for the wrong section. It also runs inside the durable
worker and only against the published catalog, so a cache miss cannot block a
turn on campus latency, and every entry is bounded because it is re-sent on
each later model step.
"""

import asyncio

from app.agents.scholar import prefetch as prefetch_module
from app.agents.scholar.prefetch import prefetch_dependencies
from fastapi import HTTPException


class FakeClient:
    def __init__(self, results=None, errors=None):
        self.calls = []
        self.results = results or {}
        self.errors = errors or {}

    async def read(self, reference):
        self.calls.append(reference)
        error = self.errors.get(reference.kind)
        if error is not None:
            raise error
        return self.results.get(reference.kind, {"data": {}, "provenance": {}})


def _run(dependencies, client):
    original = prefetch_module._available
    prefetch_module._available = lambda: True
    try:
        return asyncio.run(prefetch_dependencies(dependencies, client=client))
    finally:
        prefetch_module._available = original


def _deps(intent, courses=("EE 201",), **scope):
    return {"intent": intent, "current_focus": {"courses": list(courses)}, "requested_scope": scope}


def test_a_focused_question_reads_the_term_it_names():
    client = FakeClient()
    out = _run(_deps("prerequisites", term="20252"), client)
    assert len(client.calls) == 1
    call = client.calls[0]
    assert (call.kind, call.key, call.term, call.section) == ("catalog.prerequisites", "EE 201", "20252", None)
    assert out["prefetched"][0]["kind"] == "catalog.prerequisites"
    assert out["prefetched"][0]["term"] == "20252"


def test_eligibility_reads_the_section_it_names():
    client = FakeClient()
    out = _run(_deps("eligibility", term=None, section="2"), client)
    call = client.calls[0]
    assert (call.kind, call.section) == ("catalog.eligibility", "2")
    assert out["prefetched"][0]["section"] == "2"


def test_eligibility_without_a_section_is_not_prefetched():
    client = FakeClient()
    out = _run(_deps("eligibility", term=None, section=None, section_unresolved=True), client)
    assert client.calls == []
    assert "prefetched" not in out


def test_an_unresolvable_term_skips_everything():
    client = FakeClient()
    out = _run(_deps("prerequisites", term=None, term_unresolved=True), client)
    assert client.calls == []
    assert "prefetched" not in out


def test_more_than_one_course_is_not_guessed():
    client = FakeClient()
    out = _run(_deps("prerequisites", courses=("EE 201", "MATH 260"), term=None), client)
    assert client.calls == []
    assert "prefetched" not in out


def test_a_404_is_the_answer_and_other_failures_are_skipped():
    not_found = FakeClient(
        errors={"catalog.prerequisites": HTTPException(404, "not available in the published release")}
    )
    out = _run(_deps("prerequisites", term=None), not_found)
    assert out["prefetched"][0]["error"] == "not available in the published release"

    broken = FakeClient(errors={"catalog.prerequisites": HTTPException(502, "gateway down")})
    out = _run(_deps("prerequisites", term=None), broken)
    assert "prefetched" not in out


def test_a_terminal_miss_reported_as_502_is_still_an_answer():
    """The real client reports every MCP error as 502, including the final miss."""
    terminal = FakeClient(
        errors={
            "catalog.prerequisites": HTTPException(
                502,
                "read failed: Course is not available in the published release This is final for the "
                "requested term: do not retry it.",
            )
        }
    )
    out = _run(_deps("prerequisites", term=None), terminal)
    assert out["prefetched"][0]["error"].startswith("read failed")


def test_prefetched_results_are_projected_and_bounded():
    client = FakeClient(
        results={
            "catalog.prerequisites": {
                "data": {"_catalog": {"components": {}}, "blob": "x" * 7000},
                "provenance": {"source": "academic_catalog", "freshness": None},
            }
        }
    )
    out = _run(_deps("prerequisites", term=None), client)
    data = out["prefetched"][0]["data"]
    assert data["truncated"] is True
    assert len(data["preview"]) == 6000
    assert "x" * 7000 not in data["preview"]
    assert out["prefetched"][0]["provenance"] == {"source": "academic_catalog"}

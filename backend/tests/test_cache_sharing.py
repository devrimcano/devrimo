"""What the shared cache may hold, and what it must never hold.

A catalog answer is a property of the catalog: the sections of a course and the
eligibility table of a section are the same facts whoever asks, so one student
fetching them pays for everyone. A student's own record is the opposite, and
the line between the two is a single allowlist. These tests exist because that
line is easy to cross by accident — adding a tool to the dict is a one-line
change, and the tools whose names begin "get_student_" return one person's
curriculum.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.campus import course_info

SHARED_VALUES = {"department": "236", "semester": "20252"}


@pytest.fixture(autouse=True)
def _empty_catalog():
    """The in-process cache is module state, so a test must not inherit one."""
    course_info._catalog.purge(lambda key: True)
    yield
    course_info._catalog.purge(lambda key: True)


class _Layers:
    """Stands in for the two cache layers and the campus, and records the traffic.

    These used to be assertions about the *text* of ``call_course_info``, which
    meant a refactor that preserved every property they care about still failed
    them. What matters is where an answer goes, so that is what is checked.
    """

    def __init__(self, answer):
        self.answer = answer
        self.reads: list[str] = []
        self.writes: list[tuple[str, dict]] = []
        self.campus_calls = 0

    def install(self, monkeypatch):
        monkeypatch.setattr(course_info, "require_catalog_access", AsyncMock())

        async def read_cached(key_hash):
            self.reads.append(key_hash)
            return None

        async def write_cached(key_hash, value, **kwargs):
            self.writes.append((key_hash, kwargs))

        async def invoke(db, user_id, tool_suffix, values, session=None):
            self.campus_calls += 1
            return self.answer

        monkeypatch.setattr(course_info, "read_cached", read_cached)
        monkeypatch.setattr(course_info, "write_cached", write_cached)
        monkeypatch.setattr(course_info, "_invoke", invoke)
        return self


def test_the_allowlist_is_exactly_what_is_reviewed():
    """A new upstream tool must be considered, not silently inherited.

    Pinned deliberately: an allowlist that grows without anyone deciding is the
    same failure as a denylist, which is what this design exists to avoid.
    """
    assert set(course_info._SHARED_TOOL_TTLS) == {
        "get_departments_and_semesters",
        "search_departments",
        "list_program_courses",
        "get_course_prerequisites",
        "get_course_replacements",
        "get_thesis_courses",
        "get_course_info",
        "get_section_constraints",
    }


def test_a_shared_key_does_not_depend_on_who_asked():
    """Two students must land on the same row, or nobody is sharing anything."""
    first = course_info.catalog_key("list_program_courses", {"department": "236", "semester": "20252"})
    # The same values, built the other way round. The identity is sorted, so the
    # order a caller happened to assemble its dict in cannot split the row in two.
    second = course_info.catalog_key("list_program_courses", {"semester": "20252", "department": "236"})
    assert first == second
    # And nothing in it names a student: the function is not even given one.
    identity, _ = first
    assert "user" not in " ".join(identity)


async def test_the_second_student_is_served_the_first_student_answer(monkeypatch):
    """The point of sharing: one fetch, and the row is keyed the same both times."""
    layers = _Layers(["a course"]).install(monkeypatch)

    answer = await course_info.call_course_info(None, uuid.uuid4(), "list_program_courses", dict(SHARED_VALUES))
    assert answer == ["a course"]
    assert layers.campus_calls == 1

    # In memory it is already shared, so a second student costs nothing at all.
    await course_info.call_course_info(None, uuid.uuid4(), "list_program_courses", dict(SHARED_VALUES))
    assert layers.campus_calls == 1

    # And past the process cache they still meet on one persistent row, which is
    # what survives a restart and what makes the sharing worth anything.
    course_info._catalog.purge(lambda key: True)
    await course_info.call_course_info(None, uuid.uuid4(), "list_program_courses", dict(SHARED_VALUES))
    assert len(set(layers.reads)) == 1
    assert len({key for key, _ in layers.writes}) == 1


@pytest.mark.parametrize("tool", ["get_student_course_categories", "get_student_curriculum"])
async def test_a_student_scoped_answer_never_reaches_the_persistent_layer(monkeypatch, tool):
    """No shared TTL means no persistent traffic at all, not a private row."""
    layers = _Layers({"course_categories": [{"id": "1-236"}]}).install(monkeypatch)

    await course_info.call_course_info(None, uuid.uuid4(), tool, {})
    assert layers.reads == []
    assert layers.writes == []

    # Nor may one student's curriculum be answered from another's memory entry.
    await course_info.call_course_info(None, uuid.uuid4(), tool, {})
    assert layers.campus_calls == 2


async def test_catalog_rows_are_written_unowned(monkeypatch):
    """owner_hash None is what stops one student's erasure deleting the catalog."""
    layers = _Layers(["a course"]).install(monkeypatch)

    await course_info.call_course_info(None, uuid.uuid4(), "list_program_courses", dict(SHARED_VALUES))
    assert layers.writes
    for _, kwargs in layers.writes:
        assert kwargs["namespace"] == course_info.CATALOG_NAMESPACE
        assert kwargs.get("owner_hash") is None

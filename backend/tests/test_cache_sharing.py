"""What the shared cache may hold, and what it must never hold.

A catalog answer is a property of the catalog: the sections of a course and the
eligibility table of a section are the same facts whoever asks, so one student
fetching them pays for everyone. A student's own record is the opposite, and
the line between the two is a single allowlist. These tests exist because that
line is easy to cross by accident — adding a tool to the dict is a one-line
change, and the tools whose names begin "get_student_" return one person's
curriculum.
"""

import inspect

from app.campus import course_info


def test_no_student_scoped_tool_is_ever_shared():
    """The allowlist must never grow to include a per-student tool."""
    shared = set(course_info._SHARED_TOOL_TTLS)
    assert not [name for name in shared if name.startswith("get_student")], (
        "a get_student_* tool returns one person's record and cannot be shared"
    )


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
    source = inspect.getsource(course_info.call_course_info)
    assert "memory_key = identity if shared_ttl else (str(user_id), *identity)" in source
    # And the persistent key is built from `identity` alone.
    assert 'stable_digest({"namespace": CATALOG_NAMESPACE, "identity": list(identity)})' in source


def test_a_student_scoped_answer_never_reaches_the_persistent_layer():
    """No shared TTL means no write_cached call at all, not a private row."""
    source = inspect.getsource(course_info.call_course_info)
    before, _, after = source.partition("if shared_ttl is None:")
    assert after, "the student-scoped branch disappeared"
    # The early return happens before any cache write in that branch.
    student_branch = after.split("key_hash =")[0]
    assert "write_cached" not in student_branch


def test_catalog_rows_are_written_unowned():
    """owner_hash None is what stops one student's erasure deleting the catalog."""
    source = inspect.getsource(course_info.call_course_info)
    assert "await write_cached(key_hash, value, namespace=CATALOG_NAMESPACE, ttl_seconds=shared_ttl)" in source
    assert "owner_hash" not in source.split("await write_cached")[1].split(")")[0]

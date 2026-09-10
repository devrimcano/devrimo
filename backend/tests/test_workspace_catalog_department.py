"""A course code is not a department code.

Every read of `catalog.prerequisites` on production failed, for every key, with
502 "Error executing tool get_course_prerequisites" - while the same course's
`catalog.sections` read succeeded and returned "Mathematics". Verified against
the source, one argument apart:

    department "236",     course "2360219"  ->  CALCULUS OF FUNCTIONS OF
                                                SEVERAL VARIABLES, min grade DD
    department "2360219", course "2360219"  ->  502

`ref.department or ref.key` is right for `catalog.department`, whose key really
is a department, and wrong for every catalog resource keyed by a course. So the
assistant could not answer "does MATH 219 have a prerequisite?" at all - and
because the message was being discarded further up, it retried instead of
saying so.
"""

from app.workspace.resources import ResourceRef
from app.workspace.service import WorkspaceService


def department_for(**values) -> str | None:
    return WorkspaceService._department(ResourceRef.model_validate(values))


def test_a_course_keyed_resource_uses_the_course_s_own_department():
    """A METU course code is seven digits beginning with its department."""
    assert department_for(kind="catalog.prerequisites", key="2360219", term="20261") == "236"
    assert department_for(kind="catalog.replacements", key="2400219") == "240"
    assert department_for(kind="catalog.theses", key="5710500") == "571"


def test_a_department_keyed_resource_still_uses_its_key():
    """catalog.department is the one whose key really is a department code."""
    assert department_for(kind="catalog.department", key="236") == "236"


def test_an_explicit_department_wins_over_anything_derived():
    """A model that names the department knows something the key does not carry.

    Cross-listed and service courses are exactly the case where the department a
    student is asking about is not the one that owns the code.
    """
    assert department_for(kind="catalog.prerequisites", key="2360219", department="571") == "571"


def test_a_key_that_is_not_a_course_code_is_passed_through_unchanged():
    """Truncation is only ever right for a seven-digit numeric code."""
    assert department_for(kind="catalog.prerequisites", key="MATH 219") == "MATH 219"
    assert department_for(kind="catalog.prerequisites", key="236") == "236"
    assert department_for(kind="catalog.courses", key="23602190") == "23602190"


def test_no_key_and_no_department_is_absent_rather_than_empty():
    """An empty string is an argument the source still has to reject."""
    assert department_for(kind="catalog.departments") is None
    assert department_for(kind="catalog.prerequisites", key="") is None

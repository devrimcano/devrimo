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


def test_a_lettered_course_resolves_to_the_numeric_pair_the_catalog_reads():
    """The model guessed the department prefix and reported a real course missing.

    "EE 201" is 5670201 in the catalog; the model sent 5710201 (Computer
    Engineering's prefix) and the read 404'd. Resolving the pair here removes the
    guess.
    """
    from app.workspace.service import WorkspaceService

    assert WorkspaceService._course_scope(ResourceRef(kind="catalog.prerequisites", key="EE 201")) == (
        "5670201",
        "567",
    )
    assert WorkspaceService._course_scope(ResourceRef(kind="catalog.sections", key="CENG 331")) == (
        "5710331",
        "571",
    )
    assert WorkspaceService._course_scope(ResourceRef(kind="catalog.prerequisites", key="2360219")) == (
        "2360219",
        "236",
    )
    # An explicit department still wins over the one the code carries.
    assert WorkspaceService._course_scope(
        ResourceRef(kind="catalog.prerequisites", key="2360219", department="CENG")
    ) == ("2360219", "571")


def test_a_key_that_is_not_a_course_code_is_passed_through_unchanged():
    """Truncation is only ever right for a seven-digit numeric code."""
    assert department_for(kind="catalog.prerequisites", key="MATH 219") == "MATH 219"
    assert department_for(kind="catalog.prerequisites", key="236") == "236"
    assert department_for(kind="catalog.courses", key="23602190") == "23602190"


def test_no_key_and_no_department_is_absent_rather_than_empty():
    """An empty string is an argument the source still has to reject.

    A kind that requires a key now refuses an empty one before the call is made
    (see test_workspace_gateway), so this is the kind that does not.
    """
    assert department_for(kind="catalog.departments") is None
    assert department_for(kind="catalog.courses", key="") is None


async def test_a_catalog_read_without_a_term_means_this_term(monkeypatch):
    """"Does MATH 219 have a prerequisite" is a question about this term.

    Verified live: reading catalog.prerequisites with the term returns MATH 120
    CALCULUS OF FUNCTIONS OF SEVERAL VARIABLES at DD, and reading it without
    one answered "Course Info tool schema is unsupported; missing arguments:
    semester_code" - which names an argument of the campus tool rather than the
    `term` field the caller controls, so there was nothing in it to act on. The
    assistant duly reported that it could not find the prerequisite.

    planning.timetable has resolved its term this way from the start; the
    catalog resources now do the same.
    """
    from uuid import uuid4

    from app.planning.service import current_term
    from app.workspace.service import WorkspaceService

    sent: dict = {}

    async def fake_call_course_info(_db, _user_id, method, values):
        sent["method"] = method
        sent.update(values)
        return []

    monkeypatch.setattr("app.campus.course_info.call_course_info", fake_call_course_info)

    service = WorkspaceService(uuid4())
    monkeypatch.setattr(service, "authorize", _noop)
    await service.upstream(ResourceRef.model_validate({"kind": "catalog.prerequisites", "key": "2360219"}))

    assert sent["method"] == "get_course_prerequisites"
    assert sent["semester"] == current_term(), "a term-less catalog read has to resolve a term"
    assert sent["department"] == "236", "the department still comes from the course code"
    assert sent["course"] == "2360219"


async def _noop(*_args, **_kwargs):
    return None

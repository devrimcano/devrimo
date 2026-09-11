"""The source does not answer in one shape, and one shape was being dropped.

Read live, `get_course_prerequisites` hands back a bare list. The same call
through the cache hands back `{"result": [...]}`. `_prerequisite_groups` looked
for `groups`, `prerequisite_groups`, `prerequisites`, `requirements` and `rows`
and not for `result`, so the second shape read as no table at all and was
recorded as `missing_prerequisites_table`.

Two things followed from that, measured on production:

  MATH 219 published with nine sections and `prerequisite_groups: []`, while
  the source lists MATH 120 at DD as its prerequisite. The planner does check
  prerequisites - plan_semester evaluates them and the thread shows blocked
  courses - so the check was running against nothing.

  513 courses were held out of publication entirely, because a draft carrying
  a blocking issue refuses to publish and takes its whole batch with it.

`_rows` has carried `result` all along, which is why every other tool survived
the same envelope.
"""

from app.academic_catalog.service import _prerequisite_groups, _replacement_rows

ROW = {
    "program_code": "1",
    "dept_version": "0",
    "prerequisite_course_code": "2360120",
    "name": "CALCULUS OF FUNCTIONS OF SEVERAL VARIABLES",
    "credit": "5(4.00,2.00,0.00)",
    "set_no": "1",
    "min_grade": "DD",
}


def test_a_bare_list_is_read():
    """What a live read returns."""
    groups, present = _prerequisite_groups([ROW])
    assert present
    assert groups, "a live prerequisite read produced no groups"


def test_a_result_envelope_is_read_the_same_way():
    """What a cached read returns, and what used to be discarded."""
    groups, present = _prerequisite_groups({"result": [ROW]})
    assert present, "the {'result': [...]} envelope read as no table at all"
    assert groups == _prerequisite_groups([ROW])[0], "the envelope changed the groups"


def test_an_empty_result_envelope_is_empty_rather_than_malformed():
    """A course with no prerequisites is a real answer, not a parse failure.

    Recording it as malformed is what blocks the course from being published.
    """
    groups, present = _prerequisite_groups({"result": []})
    assert present
    assert groups == []


def test_a_payload_with_no_table_at_all_is_still_reported_as_missing():
    """The check this exists to keep: an unreadable page must stay unreadable."""
    _groups, present = _prerequisite_groups({"unexpected": "shape"})
    assert not present


def test_replacements_carry_the_same_envelope():
    """The tool beside it answers the same two ways."""
    rows, present = _replacement_rows({"result": [{"replaced_course_code": "2360117"}]})
    assert present
    assert rows, "the replacement envelope read as no table"


# --- the field-name collision that was actually blocking publication --------

# One real row, copied from a stored observation. Every prerequisite row METU
# returns carries `position` as a course *status*, not an ordinal.
METU_ROW = {
    "name": "OCCUPATIONAL HEALTH AND SAFETY-I",
    "credit": "0(0.00,0.00,0.00)",
    "set_no": "1",
    "position": "Offered Course / Açık Ders",
    "min_grade": "S",
    "level_type": "Undergraduate / Lisans",
    "dept_version": "0",
    "program_code": "1",
    "prerequisite_course_code": "8770101",
}


def test_the_source_status_label_is_not_read_as_an_ordinal():
    """`position` means "course status" here and "index" to this parser.

    _int("Offered Course / Açık Ders") is None, which marked the row malformed
    - so every course that actually had a prerequisite was recorded as
    unreadable while every course without one parsed cleanly. 513 courses were
    held out of publication by it, and MATH 219 published with nine sections
    and no prerequisites while the source lists MATH 120 at DD for it.
    """
    groups, present = _prerequisite_groups([METU_ROW])
    assert present, "a real METU prerequisite row still reads as no table"
    assert groups, "the row produced no group"
    requirements = groups[0]["requirements"]
    assert [r["course_code"] for r in requirements] == ["8770101"]
    assert requirements[0]["minimum_grade"] == "S"


def test_rows_keep_the_order_the_source_sent_them_in():
    """With no ordinal given, the list order is the order."""
    second = dict(METU_ROW, prerequisite_course_code="5710230", name="INTRODUCTION TO C PROGRAMMING")
    groups, present = _prerequisite_groups([METU_ROW, second])
    assert present
    codes = [r["course_code"] for r in groups[0]["requirements"]]
    assert codes == ["8770101", "5710230"]


def test_an_ordinal_this_parser_does_name_is_still_checked():
    """`order` and `index` are its own spellings and still mean what they say."""
    _groups, present = _prerequisite_groups([dict(METU_ROW, order="not-a-number")])
    assert not present, "a genuinely unreadable ordinal stopped being reported"

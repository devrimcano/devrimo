"""A refresh must never be able to destroy what it failed to re-read.

Every rule here exists because the opposite shipped. A SAIS read that reached
the server but returned an unparseable transcript replaced sixteen completed
courses with none. An agent run that timed out cached its empty answer and
served it for hours. A campus page that came back blank would have overwritten
a department's whole course list for its full time-to-live.

The shape of the fix is the same in each: an empty answer is treated as a
failed read, not as news that the data is gone.
"""

from app.core.persistent_cache import _is_empty, _unwrap
from app.planning.service import merge_courses


def test_a_short_read_does_not_shorten_the_transcript():
    """A transcript accumulates; it does not lose courses between reads."""
    stored = [
        {"course_code": "PHYS105", "grade": "BA"},
        {"course_code": "MATH119", "grade": "CC"},
        {"course_code": "CENG240", "grade": "BA"},
    ]
    assert len(merge_courses(stored, [])) == 3
    assert len(merge_courses(stored, [{"course_code": "PHYS106", "grade": "BB"}])) == 4


def test_a_repeated_course_takes_its_new_grade():
    """The read that does carry a row wins — that is the point of refreshing."""
    stored = [{"course_code": "MATH119", "grade": "FF"}]
    merged = merge_courses(stored, [{"course_code": "MATH119", "grade": "CC"}])
    assert merged == [{"course_code": "MATH119", "grade": "CC"}]


def test_sections_of_one_course_are_distinct_rows():
    stored = [{"course_code": "PHYS105", "section": "1", "grade": "BA"}]
    merged = merge_courses(stored, [{"course_code": "PHYS105", "section": "2", "grade": "CC"}])
    assert len(merged) == 2


def test_merging_drops_junk_rather_than_raising():
    """Campus payloads are not schemas, and a stray value must not take the
    whole merge down — nor survive into the stored transcript."""
    merged = merge_courses([None, "x", {"course_code": "EE301"}], [42, {"course_code": "EE302"}])
    assert merged == [{"course_code": "EE301"}, {"course_code": "EE302"}]


class TestEmptiness:
    """What counts as "this read produced nothing"."""

    def test_empty_shapes(self):
        for value in ([], {}, "", None, {"value": []}, {"result": []}, {"a": [], "b": {}}):
            assert _is_empty(value), value

    def test_real_answers_are_not_empty(self):
        for value in ([{}], {"courses": [1]}, "x", 0, False, {"value": 0}):
            assert not _is_empty(value), value

    def test_zero_is_an_answer_not_an_absence(self):
        """Falsy is not the test: a stored 0 or False is a real cached value."""
        assert not _is_empty(0)
        assert not _is_empty(False)

    def test_the_json_wrapper_is_seen_through(self):
        assert _unwrap({"value": [1, 2]}) == [1, 2]
        assert _unwrap({"courses": [1]}) == {"courses": [1]}

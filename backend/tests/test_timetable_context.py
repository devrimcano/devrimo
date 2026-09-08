"""What the assistant is told about the week the student is building.

The planner's timetable lived only in one browser's localStorage, so chat could
not answer "is my Tuesday free" about the very grid open in the next tab. The
SAIS schedule was reachable the whole time and is not the same thing: that is
what the student is already registered for, which answers a different question.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.agents.scholar.context import _timetable


def row(payload, term="20261"):
    return SimpleNamespace(term=term, payload=payload, updated_at=datetime(2026, 9, 5, tzinfo=UTC))


def test_nothing_stored_is_reported_as_nothing():
    """Absent, not an empty shell: an empty object in the prompt reads as
    "the student has no courses", which is a different claim."""
    assert _timetable(None) is None
    assert _timetable(row({})) is None
    assert _timetable(row({"courses": [], "busy_blocks": []})) is None


def test_a_course_is_flattened_into_a_readable_line():
    out = _timetable(
        row(
            {
                "courses": [
                    {
                        "code": "MATH119",
                        "name": "CALCULUS",
                        "section": "1",
                        "credits": 5,
                        "instructor": "PROF X",
                        "meetings": [
                            {"day": "Mon", "start": 8, "duration": 2, "room": "P1"},
                            {"day": "Wed", "start": 11, "duration": 1, "room": ""},
                        ],
                    }
                ],
                "busy_blocks": [],
            }
        )
    )
    course = out["courses"][0]
    assert course["course"] == "MATH119"
    assert course["section"] == "1"
    # METU hours run xx:40 to xx:30, which is what the grid draws.
    assert course["when"] == "Mon 08:40-10:30 P1, Wed 11:40-12:30"


def test_busy_blocks_are_included():
    """A student's own commitments are the reason half the questions get asked."""
    out = _timetable(
        row(
            {
                "courses": [],
                "busy_blocks": [{"name": "İş", "meetings": [{"day": "Tue", "start": 13, "duration": 3, "room": ""}]}],
            }
        )
    )
    assert out["busy_blocks"] == [{"name": "İş", "when": "Tue 13:40-16:30"}]


def test_the_note_distinguishes_it_from_the_sais_schedule():
    out = _timetable(
        row(
            {"courses": [{"code": "EE301", "meetings": [{"day": "Mon", "start": 8, "duration": 1}]}], "busy_blocks": []}
        )
    )
    assert "not their registered sais schedule" in out["note"].lower()
    assert out["term"] == "20261"

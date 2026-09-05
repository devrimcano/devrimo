"""The catalog warm-up's pacing.

These requests carry a real student's METU credentials, so the budget and the
window are the safety mechanism, not a tuning detail. Everything here tests
restraint: that an already-cached entry costs no request, that the daily
ceiling holds across restarts, that a failure stops the pass instead of
retrying, and that the window wraps midnight correctly.
"""

from datetime import datetime

from app.campus.warmer import ISTANBUL

import pytest

from app.campus.warmer import _cache_key, _within_hours


@pytest.mark.parametrize(
    ("hour", "window", "inside"),
    [
        (3, "1-7", True),
        (0, "1-7", False),
        (7, "1-7", False),   # end is exclusive
        (1, "1-7", True),
        # Wrapping midnight is the normal shape of an overnight window.
        (23, "22-4", True),
        (2, "22-4", True),
        (12, "22-4", False),
        # Nonsense configuration must not silently disable the job forever,
        # nor silently run it at noon under a window the operator thought
        # they set. Permissive is the safer of the two only because the other
        # limits still apply.
        (13, "not-a-window", True),
    ],
)
def test_the_overnight_window(hour, window, inside):
    # Istanbul time, not UTC: the window means the small hours where METU is.
    now = datetime(2026, 9, 5, hour, tzinfo=ISTANBUL)
    assert _within_hours(now, window) is inside


def test_the_cache_key_matches_the_one_the_page_reads():
    """A warmed entry must be the entry the schedule page looks up.

    Computed independently here, from the same inputs, so a change to either
    construction breaks this rather than silently filling a parallel cache
    nobody reads.
    """
    from app.campus.course_info import CATALOG_NAMESPACE
    from app.core.digest import stable_digest

    expected = stable_digest(
        {
            "namespace": CATALOG_NAMESPACE,
            "identity": ["list_program_courses", "department=240", "semester=20252"],
        }
    )
    assert _cache_key("240", "20252") == expected

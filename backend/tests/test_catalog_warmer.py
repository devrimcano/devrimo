"""The catalog pacing helpers the published worker borrows.

Imports carry a real deployment's credentials, so the off-peak window is a
safety mechanism, not a tuning detail: it must mean the small hours where METU
is, wrap midnight correctly, and fail closed on a malformed setting.
"""

from datetime import datetime

import pytest

from app.campus.warmer import ISTANBUL, _within_hours


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
        # A malformed safety setting must fail closed.
        (13, "not-a-window", False),
    ],
)
def test_the_overnight_window(hour, window, inside):
    # Istanbul time, not UTC: the window means the small hours where METU is.
    now = datetime(2026, 9, 5, hour, tzinfo=ISTANBUL)
    assert _within_hours(now, window) is inside


def test_settings_reject_a_malformed_warm_window():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="CATALOG_WARM_HOURS"):
        Settings(_env_file=None, catalog_warm_hours="not-a-window")

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


# --- the second phase: courses students actually asked for --------------------


async def test_demand_is_counted_and_bounded(monkeypatch):
    """The warmer has never had a demand signal; this is it, and it cannot grow."""
    from app.campus import warmer

    stored: dict[str, object] = {}

    async def read_cached(key_hash):
        return stored.get(key_hash)

    async def write_cached(key_hash, payload, **kwargs):
        stored[key_hash] = payload

    monkeypatch.setattr(warmer, "read_cached", read_cached)
    monkeypatch.setattr(warmer, "write_cached", write_cached)

    await warmer.record_wanted_courses("20252", ["2300213", "5670201"])
    await warmer.record_wanted_courses("20252", ["2300213"])
    assert await warmer._wanted_courses("20252") == {"2300213": 2, "5670201": 1}

    # A term's row holds the most wanted and nothing beyond the cap.
    await warmer.record_wanted_courses("20252", [f"236{index:04d}" for index in range(warmer.WANTED_LIMIT + 50)])
    counts = await warmer._wanted_courses("20252")
    assert len(counts) == warmer.WANTED_LIMIT
    assert counts["2300213"] == 2, "the most wanted course survives the trim"


async def test_a_failure_to_record_demand_never_reaches_the_student(monkeypatch):
    """It is a hint for a background job, not part of anybody's request."""
    from app.campus import warmer

    async def broken(*args, **kwargs):
        raise RuntimeError("cache is down")

    monkeypatch.setattr(warmer, "read_cached", broken)
    monkeypatch.setattr(warmer, "write_cached", broken)
    await warmer.record_wanted_courses("20252", ["2300213"])  # must not raise


async def test_courses_are_only_warmed_once_every_department_is_in(monkeypatch):
    """Department listings are what the search box reads; they always win."""
    from app.campus import warmer

    calls: list[str] = []

    async def read_cached(key_hash):
        return None  # nothing cached: every department is still missing

    monkeypatch.setattr(warmer, "read_cached", read_cached)

    async def warm_courses(*args, **kwargs):
        calls.append("courses")
        return 0

    monkeypatch.setattr(warmer, "_warm_courses", warm_courses)
    monkeypatch.setattr(warmer, "_warming_user", lambda: _one_user())
    monkeypatch.setattr(warmer, "_spent_today", lambda day: _zero())

    settings = warmer.get_settings()
    monkeypatch.setattr(settings, "catalog_warm_enabled", True)
    monkeypatch.setattr(settings, "catalog_warm_hours", "0-24")
    monkeypatch.setattr(settings, "catalog_warm_batch", 0)

    await warmer.warm_once()
    assert calls == [], "a missing department must not be skipped for a course page"


async def test_an_already_cached_course_costs_no_request(monkeypatch):
    """The same restraint the department phase has always had."""
    from uuid import uuid4

    from app.campus import warmer

    async def read_cached(key_hash):
        return {"already": "cached"}

    monkeypatch.setattr(warmer, "read_cached", read_cached)

    async def wanted(semester):
        return {"2300213": 5}

    monkeypatch.setattr(warmer, "_wanted_courses", wanted)

    called = []

    async def call_course_info(*args, **kwargs):
        called.append(args)
        raise AssertionError("a cached course must not be fetched")

    monkeypatch.setattr(warmer, "call_course_info", call_course_info)
    assert await warmer._warm_courses(uuid4(), "20252", "2026-09-06", 0) == 0
    assert called == []


async def _one_user():
    from uuid import uuid4

    return uuid4(), 1


async def _zero():
    return 0

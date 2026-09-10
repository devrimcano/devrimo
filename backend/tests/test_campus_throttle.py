"""How often one account may ask METU whether a password is correct.

The endpoint being limited answers "was that password right" for any username
the caller supplies, from this host's IP. These tests pin the two properties
that matter: honest use is never blocked, and walking a list is.
"""

import pytest

from app.campus import throttle


@pytest.fixture(autouse=True)
def _clean_slate():
    throttle.reset()
    yield
    throttle.reset()


def test_a_student_connecting_their_account_is_never_stopped():
    """Type it wrong, correct it, change the password later: still fine."""
    for _ in range(throttle.PER_USER_ATTEMPTS):
        assert throttle.retry_after_seconds("student-1") is None


def test_walking_a_list_is_stopped():
    for _ in range(throttle.PER_USER_ATTEMPTS):
        throttle.retry_after_seconds("student-1")
    wait = throttle.retry_after_seconds("student-1")
    assert wait is not None
    assert 0 < wait <= throttle.WINDOW_SECONDS


def test_one_account_running_out_does_not_stop_anyone_else():
    for _ in range(throttle.PER_USER_ATTEMPTS + 3):
        throttle.retry_after_seconds("student-1")
    assert throttle.retry_after_seconds("student-2") is None


def test_more_accounts_do_not_buy_more_attempts():
    """Registering is cheap, so the budget cannot be per-account alone."""
    spent = 0
    for index in range(throttle.GLOBAL_ATTEMPTS * 2):
        # A fresh account for every attempt: the per-user limit never bites.
        if throttle.retry_after_seconds(f"throwaway-{index}") is None:
            spent += 1
    assert spent == throttle.GLOBAL_ATTEMPTS


def test_a_refused_attempt_is_not_charged():
    """Otherwise being throttled would extend the block for asking."""
    for _ in range(throttle.PER_USER_ATTEMPTS):
        throttle.retry_after_seconds("student-1")
    first = throttle.retry_after_seconds("student-1")
    second = throttle.retry_after_seconds("student-1")
    assert first is not None and second is not None
    # The wait counts down from the oldest attempt, so it never grows.
    assert second <= first


def test_attempts_are_forgotten_once_the_window_passes(monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(throttle.time, "monotonic", lambda: clock["now"])

    for _ in range(throttle.PER_USER_ATTEMPTS):
        assert throttle.retry_after_seconds("student-1") is None
    assert throttle.retry_after_seconds("student-1") is not None

    clock["now"] += throttle.WINDOW_SECONDS + 1
    assert throttle.retry_after_seconds("student-1") is None


def test_tracking_does_not_grow_without_bound(monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(throttle.time, "monotonic", lambda: clock["now"])
    # Past the global cap on purpose: refused attempts must not be recorded
    # either, or a stream of new ids is still a slow leak.
    for index in range(throttle.MAX_TRACKED_USERS + 500):
        clock["now"] += 0.001
        throttle.retry_after_seconds(f"visitor-{index}")
    assert len(throttle._state.attempts) <= throttle.MAX_TRACKED_USERS

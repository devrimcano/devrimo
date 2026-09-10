"""How often one account may ask METU whether a password is correct.

``POST /api/v1/campus/connection/verify`` exists so the connection form can tell
a student their password is wrong before storing it. It takes a username and a
password, asks METU's real login, and answers ``ok: true`` or ``ok: false``.
Nothing about it required the username to be the caller's own, and nothing
limited how often it could be asked.

That is a credential-testing oracle against METU's central login, and it runs
from this host's IP. One signed-up account could walk a list of usernames and
passwords at whatever rate it liked; METU would see the traffic as Devrimo's,
which risks this host being blocked and — if METU locks accounts on repeated
failure — risks locking real students out of their own university account. The
same call is made by ``PUT /connection`` whenever a password is supplied, so
both doors are counted through here.

The shape of the limit follows the shape of honest use: a student verifies a
password when they connect their account and again if they change it. A handful
of attempts in a quarter of an hour covers that with room to spare, and does not
cover walking a list. The service-wide ceiling is there because a handful of
accounts is not much harder to create than one.

State is per-process and that is sufficient here: the API runs as a single
uvicorn process with no ``--workers``. If that ever changes this becomes a
per-worker limit, which is weaker than intended rather than absent — and the
right answer then is a shared store, not a bigger number.
"""

import time
from collections import deque
from dataclasses import dataclass, field

# Per account: enough for a student who mistypes, then corrects, then changes
# their password later the same afternoon.
PER_USER_ATTEMPTS = 6
# Across every account, so registering more accounts does not multiply the
# budget. Sized well above what the whole user base does in a quarter hour.
GLOBAL_ATTEMPTS = 90
WINDOW_SECONDS = 15 * 60

# Above this many tracked accounts, the oldest idle ones are dropped. Without a
# bound, a stream of new user ids is itself a slow memory leak.
MAX_TRACKED_USERS = 5_000


@dataclass
class _Window:
    attempts: dict[str, deque[float]] = field(default_factory=dict)
    everyone: deque[float] = field(default_factory=deque)


_state = _Window()


def _prune(now: float) -> None:
    cutoff = now - WINDOW_SECONDS
    while _state.everyone and _state.everyone[0] <= cutoff:
        _state.everyone.popleft()
    for key in list(_state.attempts):
        stamps = _state.attempts[key]
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        if not stamps:
            del _state.attempts[key]
    if len(_state.attempts) > MAX_TRACKED_USERS:
        # Oldest last-attempt first. Dropping an entry only forgives attempts
        # already made, and the global counter still holds the line.
        for key in sorted(_state.attempts, key=lambda k: _state.attempts[k][-1])[
            : len(_state.attempts) - MAX_TRACKED_USERS
        ]:
            del _state.attempts[key]


def retry_after_seconds(user_id: str) -> int | None:
    """Seconds to wait, or ``None`` when this attempt is allowed.

    Counts the attempt when it allows it, so a caller cannot ask permission and
    then not use it.
    """
    now = time.monotonic()
    _prune(now)

    # Looked up rather than created: an attempt that is about to be refused
    # must not leave a record behind, or a stream of unknown ids is a slow leak
    # that pruning only catches on the following call.
    stamps = _state.attempts.get(user_id)
    if stamps is not None and len(stamps) >= PER_USER_ATTEMPTS:
        return max(1, int(WINDOW_SECONDS - (now - stamps[0])))
    if len(_state.everyone) >= GLOBAL_ATTEMPTS:
        return max(1, int(WINDOW_SECONDS - (now - _state.everyone[0])))

    if stamps is None:
        stamps = _state.attempts[user_id] = deque()
    stamps.append(now)
    _state.everyone.append(now)
    return None


def reset() -> None:
    """Forget every recorded attempt. For tests; nothing in the app calls it."""
    _state.attempts.clear()
    _state.everyone.clear()

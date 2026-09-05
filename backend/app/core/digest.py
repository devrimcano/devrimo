"""One definition of the hashes that identify a student's cached rows.

``schedule_data_cache`` is keyed by SHA-256 digests rather than by user id, and
``DELETE /student/academic-data`` has to reproduce the *owner* digest exactly to
find those rows again. When the two formulas lived in separate modules they
agreed only by coincidence — ``json.dumps`` kwargs that happen to be no-ops on a
bare string — and a change to either one would have made the deletion path miss
silently rather than fail. It lives here so there is only one formula to change.
"""

import hashlib
import json
from typing import Any
from uuid import UUID


def stable_digest(value: Any) -> str:
    """A SHA-256 over a canonical JSON rendering of ``value``."""
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def owner_digest(user_id: UUID | str) -> str:
    """The ``owner_hash`` every cache row belonging to ``user_id`` carries."""
    return stable_digest(str(user_id))

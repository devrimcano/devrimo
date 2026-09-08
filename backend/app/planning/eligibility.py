"""Signed evidence for normal-plan section eligibility.

The browser and assistant need to carry a section verdict into the revisioned
timetable command, but a client supplied ``eligible: true`` value is not
authoritative by itself. These short-lived signatures bind the verdict to the
authenticated student, term and the SAIS context versions used to calculate
it. The timetable owner verifies the signature before accepting a normal
write; what-if state deliberately does not need one.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.config import get_settings

TOKEN_VERSION = "v3"
# Section constraints are served from the shared Course Info cache for seven
# days. Evidence cannot outlive the source answer it certifies, even when the
# student's retained context and transcript rows have not changed.
MAX_EVIDENCE_AGE = timedelta(days=7)
_MAX_CLOCK_SKEW = timedelta(minutes=5)


def academic_evidence_fresh(
    context_verified_at: datetime | str | None,
    snapshot_fetched_at: datetime | str | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether the SAIS inputs are recent enough for a normal plan.

    A token's seven-day lifetime must not turn an old transcript or profile
    row into fresh evidence.  A missing snapshot is allowed here because some
    sections have no transcript-dependent dimensions; callers still require a
    snapshot when their specific rows contain grade/prerequisite/CGPA rules.
    """

    if context_verified_at is None:
        return False
    current = (now or datetime.now(UTC)).astimezone(UTC)
    for value in (context_verified_at, snapshot_fetched_at):
        if value is None:
            continue
        try:
            age = current - _utc(value)
        except (TypeError, ValueError, OverflowError):
            return False
        if age < -_MAX_CLOCK_SKEW or age > MAX_EVIDENCE_AGE:
            return False
    return True


def _stamp(value: datetime | str | None) -> str:
    if value is None:
        return ""
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def planning_context_fingerprint(values: dict[str, Any] | None) -> str:
    """Stable, opaque binding for the current verified academic context."""

    if not isinstance(values, dict):
        return ""
    fields = (
        "department",
        "program_code",
        "degree_level",
        "year_of_study",
        "surname_prefix",
        "campus",
        "verified_at",
        "confirmed_at",
    )
    raw = "\x1f".join(_stamp(values.get(field)) for field in fields)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def meeting_fingerprint(meetings: Any) -> str:
    """Canonical identity for the published meeting data of one section."""

    if not isinstance(meetings, (list, tuple)):
        return ""
    normalized: list[str] = []
    for item in meetings:
        if not isinstance(item, dict):
            continue
        day = str(item.get("day") or "").strip()
        start = item.get("start_minute", item.get("startMinute"))
        duration = item.get("duration_minutes", item.get("durationMinutes"))
        if start is None and item.get("start") is not None:
            try:
                start = int(item.get("start")) * 60 + 40
            except (TypeError, ValueError):
                start = ""
        if duration is None and item.get("duration") is not None:
            try:
                duration = max(1, int(item.get("duration")) * 60 - 10)
            except (TypeError, ValueError):
                duration = ""
        try:
            start_value = str(int(start))
        except (TypeError, ValueError):
            start_value = ""
        try:
            duration_value = str(int(duration))
        except (TypeError, ValueError):
            duration_value = ""
        normalized.append("|".join((day, start_value, duration_value, str(item.get("room") or "").strip())))
    return ";".join(normalized)


def _message(
    user_id: UUID | str,
    term: str,
    course_code: str,
    section: str,
    eligibility_status: str,
    eligible: bool,
    context_verified_at: datetime | str | None,
    snapshot_fetched_at: datetime | str | None,
    issued_at: datetime | str,
    source_expires_at: datetime | str | None,
    context_fingerprint: str | None,
    meetings: Any,
) -> bytes:
    values = (
        str(user_id),
        str(term),
        str(course_code),
        str(section),
        str(eligibility_status),
        "1" if eligible else "0",
        _stamp(context_verified_at),
        _stamp(snapshot_fetched_at),
        _stamp(issued_at),
        _stamp(source_expires_at),
        str(context_fingerprint or ""),
        meeting_fingerprint(meetings),
    )
    return "\x1f".join(values).encode("utf-8")


def issue_eligibility_token(
    user_id: UUID | str,
    term: str,
    course_code: str,
    section: str,
    *,
    eligibility_status: str,
    eligible: bool,
    context_verified_at: datetime | str | None,
    snapshot_fetched_at: datetime | str | None,
    meetings: Any = None,
    issued_at: datetime | str | None = None,
    source_expires_at: datetime | str | None = None,
    context_fingerprint: str | None = None,
) -> str:
    """Create an opaque HMAC proof for one positive server verdict."""

    secret = get_settings().secret_encryption_key
    if not secret:
        raise RuntimeError("SECRET_ENCRYPTION_KEY must be set before issuing eligibility evidence")
    if not academic_evidence_fresh(context_verified_at, snapshot_fetched_at):
        raise ValueError("SAIS academic evidence is stale")
    issued = issued_at or datetime.now(UTC)
    if isinstance(issued, datetime):
        issued = issued.astimezone(UTC).replace(microsecond=0)
    source_expiry = _utc(source_expires_at) if source_expires_at is not None else _utc(issued) + MAX_EVIDENCE_AGE
    source_expiry = source_expiry.replace(microsecond=0)
    issued_stamp = _stamp(issued)
    source_expiry_stamp = _stamp(source_expiry)
    digest = hmac.new(
        secret.encode("utf-8"),
        _message(
            user_id,
            term,
            course_code,
            section,
            eligibility_status,
            eligible,
            context_verified_at,
            snapshot_fetched_at,
            issued_stamp,
            source_expiry_stamp,
            context_fingerprint,
            meetings,
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"{TOKEN_VERSION}.{issued_stamp}.{source_expiry_stamp}.{digest}"


def verify_eligibility_token(
    token: str | None,
    user_id: UUID | str,
    term: str,
    course_code: str,
    section: str,
    *,
    eligibility_status: str,
    eligible: bool,
    context_verified_at: datetime | str | None,
    snapshot_fetched_at: datetime | str | None,
    meetings: Any = None,
    context_fingerprint: str | None = None,
) -> bool:
    """Verify evidence against the current student context versions."""

    if not token or not token.startswith(f"{TOKEN_VERSION}."):
        return False
    try:
        _, issued_raw, source_expiry_raw, digest = token.split(".", 3)
        issued_at = _utc(issued_raw)
        source_expiry = _utc(source_expiry_raw)
        now = datetime.now(UTC)
        age = now - issued_at
        if age < timedelta(minutes=-5) or age > MAX_EVIDENCE_AGE:
            return False
        # The proof cannot be kept alive by re-signing a cached answer. The
        # expiry is signed into the token and is capped at the same seven-day
        # lifetime as the source Course Info cache.
        if source_expiry <= issued_at or source_expiry > issued_at + MAX_EVIDENCE_AGE or now > source_expiry:
            return False
        if not academic_evidence_fresh(context_verified_at, snapshot_fetched_at, now=now):
            return False
        expected = issue_eligibility_token(
            user_id,
            term,
            course_code,
            section,
            eligibility_status=eligibility_status,
            eligible=eligible,
            context_verified_at=context_verified_at,
            snapshot_fetched_at=snapshot_fetched_at,
            meetings=meetings,
            issued_at=issued_at,
            source_expires_at=source_expiry,
            context_fingerprint=context_fingerprint,
        )
    except (RuntimeError, TypeError, ValueError, OverflowError):
        return False
    return hmac.compare_digest(digest, expected.rsplit(".", 1)[-1])

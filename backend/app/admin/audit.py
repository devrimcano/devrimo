from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminAuditEvent
from app.observability.client import capture
from app.observability.context import (
    OUTCOME_EXPECTED_FAILURE,
    OUTCOME_SUCCESS,
    OUTCOME_UNEXPECTED_FAILURE,
)

# Audit results map onto the outcome vocabulary the rest of the system reports
# in, so "which admin actions are failing" is the same query shape as "which
# requests are failing". "partial" is an unexpected failure on purpose: the
# local half committed and the remote half did not, which is the state most
# worth investigating and the one that produced no signal at all before.
_OUTCOMES = {
    "success": OUTCOME_SUCCESS,
    "failed": OUTCOME_UNEXPECTED_FAILURE,
    "partial": OUTCOME_UNEXPECTED_FAILURE,
    "denied": OUTCOME_EXPECTED_FAILURE,
    "blocked": OUTCOME_EXPECTED_FAILURE,
}


async def record_event(
    db: AsyncSession,
    *,
    actor_user_id: UUID,
    action: str,
    result: str,
    target_user_id: UUID | None = None,
    organization_id: UUID | None = None,
    reason: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> AdminAuditEvent:
    """Record allowlisted state only; callers must never pass secrets or content.

    Every admin mutation already funnels through here to write its audit row,
    which makes this the one place an admin action can be reported to PostHog
    without scattering a capture call across two dozen routes — and without any
    risk of the same action being counted twice.

    ``reason``, ``before`` and ``after`` stay in the database. They are the
    audit trail, they can name a student, and the product question PostHog
    answers is "which actions run and which of them fail".
    """
    event = AdminAuditEvent(
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        organization_id=organization_id,
        action=action[:100],
        result=result[:32],
        reason=reason[:1000] if reason else None,
        before_state=before,
        after_state=after,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    capture(
        "admin_action",
        distinct_id=str(actor_user_id),
        action=event.action,
        result=event.result,
        outcome=_OUTCOMES.get(event.result, OUTCOME_UNEXPECTED_FAILURE),
        has_target=target_user_id is not None,
        organization_id=str(organization_id) if organization_id else None,
    )
    return event

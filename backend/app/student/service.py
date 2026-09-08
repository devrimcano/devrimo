from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StudentContext, UserPreference

ALLOWED_PREFERENCE_KEYS = {
    "interests",
    "event_categories",
    "device_platform",
    "accessibility_display",
    "digest_preferences",
}
SENSITIVE_TERMS = {
    "health",
    "medical",
    "disability",
    "religion",
    "political",
    "ethnicity",
    "sexual",
    "disciplinary",
    "password",
    "grade",
    "transcript",
}


def validate_preference(key: str, value: dict) -> None:
    if key not in ALLOWED_PREFERENCE_KEYS:
        raise ValueError("Preference key is not in the benign preference allowlist")
    serialized = str(value).lower()
    if any(term in serialized for term in SENSITIVE_TERMS):
        raise ValueError("Sensitive traits and academic records cannot be stored as preferences")
    if len(serialized) > 4000:
        raise ValueError("Preference value is too large")


async def get_context(db: AsyncSession, user_id: UUID) -> StudentContext:
    context = await db.get(StudentContext, user_id)
    if context is None:
        context = StudentContext(user_id=user_id)
        db.add(context)
        await db.commit()
        await db.refresh(context)
    return context


async def apply_verified_context(
    db: AsyncSession,
    user_id: UUID,
    *,
    department: str | None,
    degree_level: str | None,
    program_code: str | None,
    campus: str | None,
    surname_prefix: str | None = None,
    year_of_study: int | None = None,
    source: str = "sais",
) -> StudentContext:
    context = await get_context(db, user_id)
    # Every field is overwritten only when SAIS actually reported it. A refresh
    # that reaches SAIS but cannot parse one field must not erase a value the
    # student typed by hand — that turns a partial read into data loss, and the
    # student has no way to tell it happened.
    incoming = (
        ("department", department),
        ("degree_level", degree_level),
        ("program_code", program_code),
        ("campus", campus),
        ("surname_prefix", surname_prefix),
        ("year_of_study", year_of_study),
    )
    was_verified = context.verified_at
    retained_verified_fields = any(
        value is None and getattr(context, field, None) not in (None, "")
        for field, value in incoming
    )
    found = 0
    for field, value in incoming:
        if value:
            setattr(context, field, value)
            found += 1
    # Only claim SAIS verification when SAIS actually told us something.
    if found and (was_verified is None or not retained_verified_fields):
        now = datetime.now(UTC)
        context.source = source
        context.verified_at = now
        # Confirmed on arrival, deliberately. SAIS *is* the registrar's record,
        # so a prompt asking the student to confirm their own department back
        # to us is a dialog with no decision in it: they have nothing to check
        # it against and no reason to answer no. All it reliably did was leave
        # the context unconfirmed for everyone who ignored it. A student who
        # disagrees edits the field, which marks it manual — that is the real
        # correction path, and it always existed.
        context.confirmed_at = now
    await db.commit()
    await db.refresh(context)
    return context


async def list_preferences(db: AsyncSession, user_id: UUID) -> list[UserPreference]:
    return (
        await db.execute(
            select(UserPreference).where(UserPreference.user_id == user_id).order_by(UserPreference.updated_at.desc())
        )
    ).scalars().all()

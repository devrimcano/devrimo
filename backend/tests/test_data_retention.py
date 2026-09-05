"""Erasure and expiry of stored student data.

Both cases here cover something whose failure mode is silence. A deletion that
misses a table returns 200. A cache with no sweep serves correct answers while
it grows without bound. Neither breaks a request, which is exactly why they
need tests rather than monitoring.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.digest import owner_digest, stable_digest
from app.db.models import (
    ScheduleDataCache,
    StudentAcademicSnapshot,
    StudentContext,
    UserMailFact,
    UserPreference,
)
from app.db.session import SessionLocal
from app.knowledge.retention import sweep_expired_schedule_cache
from app.student.purge import purge_academic_data, purge_student_data


# --- the owner digest --------------------------------------------------------


def test_owner_digest_matches_the_key_builder_formula():
    """The cache is keyed by digest, so deletion has to reproduce it exactly.

    These two lived in separate modules with different ``json.dumps`` keyword
    arguments and agreed only because those arguments happen to be no-ops on a
    bare string. This asserts the property the deletion path depends on.
    """
    user_id = uuid.uuid4()
    assert owner_digest(user_id) == stable_digest(str(user_id))
    assert owner_digest(user_id) == owner_digest(str(user_id))
    assert owner_digest(user_id) != owner_digest(uuid.uuid4())


# --- erasure -----------------------------------------------------------------


async def _seed_student(db, user_id: uuid.UUID) -> None:
    db.add(StudentContext(user_id=user_id, department="Computer Engineering", program_code="567"))
    db.add(
        StudentAcademicSnapshot(
            user_id=user_id,
            term="20261",
            completed_courses=[{"course_code": "5670111", "grade": "AA"}],
            enrolled_courses=[],
            current_credits=30,
            current_grade_points=110,
        )
    )
    db.add(
        ScheduleDataCache(
            key_hash=stable_digest({"owner": owner_digest(user_id), "n": 1}),
            owner_hash=owner_digest(user_id),
            namespace="schedule-plan",
            payload={"courses": []},
            expires_at=datetime.now(UTC) + timedelta(hours=6),
        )
    )
    db.add(UserPreference(user_id=user_id, key="interests", value={"topics": ["robotics"]}, provenance="explicit"))
    db.add(
        UserMailFact(
            user_id=user_id,
            external_id="m-1",
            fact_type="deadline",
            title="Add-drop closes",
            summary="Add-drop closes on Friday",
            message_digest="0" * 64,
        )
    )
    await db.commit()


async def _counts(db, user_id: uuid.UUID) -> dict[str, int]:
    async def count(model, column):
        return len((await db.execute(select(model).where(column == user_id))).scalars().all())

    cached = (
        await db.execute(select(ScheduleDataCache).where(ScheduleDataCache.owner_hash == owner_digest(user_id)))
    ).scalars().all()
    return {
        "context": await count(StudentContext, StudentContext.user_id),
        "snapshot": await count(StudentAcademicSnapshot, StudentAcademicSnapshot.user_id),
        "preference": await count(UserPreference, UserPreference.user_id),
        "mail_fact": await count(UserMailFact, UserMailFact.user_id),
        "cache": len(cached),
    }


async def test_academic_purge_clears_the_transcript_and_its_cache():
    user_id = uuid.uuid4()
    async with SessionLocal() as db:
        await _seed_student(db, user_id)
        await purge_academic_data(db, user_id)
        await db.commit()
        counts = await _counts(db, user_id)
    assert counts["context"] == 0
    assert counts["snapshot"] == 0
    assert counts["cache"] == 0
    # Deliberately narrow: asking to delete academic data is not asking to
    # forget benign preferences or opted-in mail facts.
    assert counts["preference"] == 1
    assert counts["mail_fact"] == 1


async def test_account_purge_leaves_nothing_keyed_to_the_user():
    """What permanent account deletion has to mean.

    The admin path used to delete sessions, credentials, the profile and the
    agent row — so a deleted account's transcript, with its completed courses
    and grade points, outlived it.
    """
    user_id = uuid.uuid4()
    async with SessionLocal() as db:
        await _seed_student(db, user_id)
        await purge_student_data(db, user_id)
        await db.commit()
        counts = await _counts(db, user_id)
    assert counts == {"context": 0, "snapshot": 0, "preference": 0, "mail_fact": 0, "cache": 0}


async def test_purge_does_not_reach_another_student():
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    async with SessionLocal() as db:
        await _seed_student(db, mine)
        await _seed_student(db, theirs)
        await purge_student_data(db, mine)
        await db.commit()
        assert (await _counts(db, mine))["snapshot"] == 0
        assert await _counts(db, theirs) == {
            "context": 1,
            "snapshot": 1,
            "preference": 1,
            "mail_fact": 1,
            "cache": 1,
        }


# --- expiry ------------------------------------------------------------------


def _cache_row(key: str, *, expires_in: timedelta) -> ScheduleDataCache:
    return ScheduleDataCache(
        key_hash=stable_digest(key),
        owner_hash=owner_digest(uuid.uuid4()),
        namespace="schedule-plan",
        payload={"courses": []},
        expires_at=datetime.now(UTC) + expires_in,
    )


async def test_sweep_removes_only_expired_rows():
    """The rows nothing else reclaims.

    A plan key includes the requested course pool, so a student who tries five
    pools leaves four rows that no read will return to — the read path can only
    drop a row somebody asks for a second time.
    """
    async with SessionLocal() as db:
        db.add(_cache_row("stale-1", expires_in=-timedelta(hours=1)))
        db.add(_cache_row("stale-2", expires_in=-timedelta(days=3)))
        db.add(_cache_row("live", expires_in=timedelta(hours=6)))
        await db.commit()

        assert await sweep_expired_schedule_cache(db) == 2
        remaining = (await db.execute(select(ScheduleDataCache))).scalars().all()
    assert [row.key_hash for row in remaining] == [stable_digest("live")]


async def test_sweep_is_bounded_and_repeatable():
    """Batched so one pass cannot hold a long lock on a table the page reads."""
    async with SessionLocal() as db:
        for index in range(5):
            db.add(_cache_row(f"stale-{index}", expires_in=-timedelta(hours=1)))
        await db.commit()

        assert await sweep_expired_schedule_cache(db, batch_size=2) == 2
        assert await sweep_expired_schedule_cache(db, batch_size=2) == 2
        assert await sweep_expired_schedule_cache(db, batch_size=2) == 1
        assert await sweep_expired_schedule_cache(db, batch_size=2) == 0

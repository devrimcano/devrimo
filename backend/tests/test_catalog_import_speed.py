"""What a whole-term import spends its hours on, and the shape that fixed it.

Measured on production from 4,855 real source reads before this change: a plan
step cost about 2.7 seconds and the whole-term plan is 10,048 steps, so roughly
eight and a half hours. Two of those hours were this worker's own bookkeeping -
four to five separate transactions per step, every one of them taking a row lock
on the same job - and another hour was a fifteen-step batch handing control back
every five seconds.

Nothing here is about what gets written. These tests pin how often, because that
is the whole of the change, and because "correct but a day long" is how the
import failed three times in a row without anyone being able to point at a bug.
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.academic_catalog import worker
from app.academic_catalog.models import CatalogImportJob, CatalogSourceObservation
from app.admin.directory import METU_ID, ensure_metu
from app.db.session import SessionLocal

# --- A. the lease timestamp ------------------------------------------------


async def test_the_lease_is_touched_on_a_timer_not_once_per_request(monkeypatch):
    """The gate is an httpx request hook, so "per request" meant per redirect.

    Four round trips to Frankfurt to move a timestamp with fifteen minutes of
    headroom, four or five times per course, ten thousand courses.
    """
    touches = []

    async def touch(organization_id, job_id, lease_token):
        touches.append(job_id)

    monkeypatch.setattr(worker, "admit_request", touch)
    settings = worker.get_settings()
    monkeypatch.setattr(settings, "catalog_import_lease_touch_seconds", 180.0)

    job_id = uuid4()
    keeper = worker.LeaseKeeper(METU_ID, job_id, uuid4())
    for _ in range(40):
        await keeper()
    assert touches == [job_id], "forty requests must cost one lease write, not forty"


async def test_the_lease_is_touched_again_once_the_interval_has_passed(monkeypatch):
    """It is a rate limit, not a once-per-pass write: the lease must not lapse."""
    touches = []

    async def touch(organization_id, job_id, lease_token):
        touches.append(job_id)

    monkeypatch.setattr(worker, "admit_request", touch)
    monkeypatch.setattr(worker.get_settings(), "catalog_import_lease_touch_seconds", 1.0)

    keeper = worker.LeaseKeeper(METU_ID, uuid4(), uuid4())
    await keeper()
    await keeper()
    assert len(touches) == 1
    # Rather than sleeping a second in a test: move the keeper's own clock back.
    keeper._next_touch = 0.0
    await keeper()
    assert len(touches) == 2


async def test_the_legacy_warmer_gate_is_still_admitted_per_request(monkeypatch):
    """Without a job there is no lease - there is a daily HTTP budget, and that
    one really does have to be consulted before every single request."""
    calls = []

    async def touch(organization_id, job_id, lease_token):
        calls.append(job_id)

    monkeypatch.setattr(worker, "admit_request", touch)
    keeper = worker.LeaseKeeper(METU_ID, None, None)
    await keeper()
    await keeper()
    await keeper()
    assert calls == [None, None, None]


# --- B/C. the cursor and the plan --------------------------------------------


async def test_the_persisted_cursor_never_runs_ahead_of_the_persisted_plan(monkeypatch):
    """The one invariant the checkpointing has to hold.

    Between checkpoints neither the cursor nor the plan is written, so an
    interrupted pass re-reads a few pages. If the cursor were written alone it
    would instead point past steps whose expansions were never stored, and the
    courses those pages discovered would be skipped for good.
    """
    from app.academic_catalog.service import enqueue_import

    settings = worker.get_settings()
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", True)
    monkeypatch.setattr(settings, "catalog_warm_enabled", False)
    monkeypatch.setattr(worker, "_within_hours", lambda *_: True)
    monkeypatch.setattr(worker, "_account", AsyncMockAccount())
    # Large enough that the pass finishes without ever hitting a step count.
    monkeypatch.setattr(settings, "catalog_import_checkpoint_steps", 1000)

    class Source:
        def __init__(self, _secret, _gate):
            pass

        async def read(self, tool, values):
            if tool == "get_course_info":
                return {"sections": [{"section": "1"}, {"section": "2"}]}
            return {}

        async def aclose(self):
            pass

    monkeypatch.setattr(worker, "CatalogSource", Source)
    async with SessionLocal() as db:
        await ensure_metu(db)
        job = await enqueue_import(db, METU_ID, "20261", course_codes=["2400219"], requested_by=uuid4())
        await db.commit()
        job_id = job.id

    result = await worker.run_once()
    assert result.outcome == "completed"

    async with SessionLocal() as db:
        stored = await db.get(CatalogImportJob, job_id)
        plan = (stored.checkpoint or {}).get("steps") or []
        assert stored.checkpoint_offset <= len(plan), (
            "the cursor points past the end of the plan that was stored with it"
        )
        # The sections the course listing discovered are in the stored plan, not
        # only in the pass's memory.
        assert any(step["tool"] == "get_section_constraints" for step in plan)


async def test_a_pass_stops_at_the_batch_it_is_given(monkeypatch):
    """The batch is the import's own, deliberately not the legacy warmer's.

    Raising catalog_warm_batch would have raised the warmer's daily HTTP
    allowance with it, which is a different thing bounded for a different reason.
    """
    from app.academic_catalog.service import enqueue_import

    settings = worker.get_settings()
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", True)
    monkeypatch.setattr(settings, "catalog_warm_enabled", False)
    monkeypatch.setattr(worker, "_within_hours", lambda *_: True)
    monkeypatch.setattr(worker, "_account", AsyncMockAccount())
    monkeypatch.setattr(settings, "catalog_import_batch", 2)
    monkeypatch.setattr(settings, "catalog_import_concurrency", 4)

    class Source:
        def __init__(self, _secret, _gate):
            pass

        async def read(self, _tool, _values):
            return {}

        async def aclose(self):
            pass

    monkeypatch.setattr(worker, "CatalogSource", Source)
    async with SessionLocal() as db:
        await ensure_metu(db)
        # Three codes is nine steps; a batch of two must stop at two.
        job = await enqueue_import(
            db, METU_ID, "20261", course_codes=["2400219", "2400119", "2400213"], requested_by=uuid4()
        )
        await db.commit()
        job_id = job.id

    result = await worker.run_once()
    assert result.outcome == "progress"
    assert int(result) == 2, "a window wider than the remaining batch must not overshoot it"
    async with SessionLocal() as db:
        stored = await db.get(CatalogImportJob, job_id)
        assert stored.checkpoint_offset == 2


# --- E. more than one page at a time ----------------------------------------


async def test_concurrency_reads_the_same_plan_and_keeps_it_in_order(monkeypatch):
    """A page is fetched on its own client, because a portal session serves one.

    Concurrency must not change which pages are read or the order they are
    recorded in - only how many are in flight. The observations are what a
    reviewer reads, so their order is part of the contract.
    """
    from app.academic_catalog.service import enqueue_import

    settings = worker.get_settings()
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", True)
    monkeypatch.setattr(settings, "catalog_warm_enabled", False)
    monkeypatch.setattr(worker, "_within_hours", lambda *_: True)
    monkeypatch.setattr(worker, "_account", AsyncMockAccount())
    monkeypatch.setattr(settings, "catalog_import_concurrency", 3)

    live = {"now": 0, "peak": 0}

    class Source:
        def __init__(self, _secret, _gate):
            pass

        async def read(self, tool, values):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            try:
                # Yield, so overlapping reads really do overlap.
                await asyncio.sleep(0)
                return {}
            finally:
                live["now"] -= 1

        async def aclose(self):
            pass

    monkeypatch.setattr(worker, "CatalogSource", Source)
    async with SessionLocal() as db:
        await ensure_metu(db)
        await enqueue_import(db, METU_ID, "20261", course_codes=["2400219", "2400119"], requested_by=uuid4())
        await db.commit()

    result = await worker.run_once()
    assert result.outcome == "completed"
    assert live["peak"] > 1, "three clients were built and the pass still read one page at a time"

    async with SessionLocal() as db:
        rows = (await db.scalars(
            select(CatalogSourceObservation)
            .where(CatalogSourceObservation.organization_id == METU_ID)
            .order_by(CatalogSourceObservation.created_at, CatalogSourceObservation.id)
        )).all()
    tools = [row.tool for row in rows]
    # Six steps: three tools for each of two courses, recorded in plan order.
    assert tools == [
        "get_course_info", "get_course_prerequisites", "get_course_replacements",
        "get_course_info", "get_course_prerequisites", "get_course_replacements",
    ], tools


async def test_a_failure_beside_other_pages_keeps_the_pages_that_answered(monkeypatch):
    """One client failing must not discard what the others just fetched.

    A window is read together, so a failure that ends the pass - a lost lease,
    an authentication failure - arrives with two or three good pages beside it.
    Those were paid for; they are kept, and the cursor stops at the page that
    did not answer so the retry starts there.
    """
    from app.academic_catalog.service import enqueue_import

    settings = worker.get_settings()
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", True)
    monkeypatch.setattr(settings, "catalog_warm_enabled", False)
    monkeypatch.setattr(worker, "_within_hours", lambda *_: True)
    monkeypatch.setattr(worker, "_account", AsyncMockAccount())
    monkeypatch.setattr(settings, "catalog_import_concurrency", 3)

    class Source:
        def __init__(self, _secret, _gate):
            pass

        async def read(self, tool, _values):
            if tool == "get_course_prerequisites":
                raise RuntimeError("the source went away")
            return {}

        async def aclose(self):
            pass

    monkeypatch.setattr(worker, "CatalogSource", Source)
    async with SessionLocal() as db:
        await ensure_metu(db)
        job = await enqueue_import(db, METU_ID, "20261", course_codes=["2400219"], requested_by=uuid4())
        await db.commit()
        job_id = job.id

    result = await worker.run_once()
    assert result.outcome == "failed"

    async with SessionLocal() as db:
        stored = await db.get(CatalogImportJob, job_id)
        # Step 0 answered and was kept; step 1 is where the retry resumes.
        assert stored.checkpoint_offset == 1
        answered = (await db.scalars(select(CatalogSourceObservation.tool).where(
            CatalogSourceObservation.organization_id == METU_ID,
            CatalogSourceObservation.payload.is_not(None),
        ))).all()
        assert "get_course_info" in answered, "a page that answered was thrown away"


class AsyncMockAccount:
    """An eligible source account, without a credential to decrypt."""

    def __init__(self):
        self.user_id = uuid4()

    async def __call__(self, organization_id):
        return self.user_id, object()


@pytest.fixture(autouse=True)
def _no_scheduling(monkeypatch):
    """These tests are about one job's pass, not about what else is due."""
    async def nothing(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(worker, "schedule_due", nothing)

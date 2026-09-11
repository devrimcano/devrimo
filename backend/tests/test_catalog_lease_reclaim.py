"""A job a killed worker was holding is handed back, not waited out.

A pass that is cancelled - which is every deploy, because systemd restarts the
unit - unwinds without releasing the job's lease, and `_claim` will not touch a
`running` job until that lease expires. The lease is fifteen minutes.

Measured across one afternoon on production: three deploys, and each left the
whole-term catalog import stopped for the remainder of its lease while the new
worker polled beside it with nothing to do. Each time it took a manual
`update ... set status='queued', lease_until=null` to get it moving again.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.academic_catalog import worker
from app.academic_catalog.models import CatalogImportJob
from app.admin.directory import METU_ID, ensure_metu
from app.db.session import SessionLocal


async def _running_job(lease_minutes: int) -> CatalogImportJob:
    async with SessionLocal() as db:
        await ensure_metu(db)
        job = CatalogImportJob(
            organization_id=METU_ID,
            term="20261",
            status="running",
            lease_token=uuid4(),
            lease_until=datetime.now(UTC) + timedelta(minutes=lease_minutes),
            dedup_key=f"reclaim-{uuid4()}",
            checkpoint={"steps": [], "offset": 7},
            checkpoint_offset=7,
        )
        db.add(job)
        await db.commit()
        return job


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    """Each test is a worker that has just started."""
    monkeypatch.setattr(worker, "_reclaimed_on_boot", False, raising=False)


async def test_a_live_lease_from_a_dead_worker_is_released():
    """The whole point: the new process does not wait out the old one's lease."""
    job = await _running_job(lease_minutes=14)
    await worker._reclaim_leases_from_a_previous_process()

    async with SessionLocal() as db:
        reclaimed = await db.get(CatalogImportJob, job.id)
        assert reclaimed.status == "queued"
        assert reclaimed.lease_until is None
        assert reclaimed.lease_token is None


async def test_the_cursor_survives_being_reclaimed():
    """It is handed back to be continued, not restarted.

    Losing the offset would turn a deploy into a re-walk of everything already
    fetched - on a whole-term import, hours of requests through a student's
    account.
    """
    job = await _running_job(lease_minutes=14)
    await worker._reclaim_leases_from_a_previous_process()

    async with SessionLocal() as db:
        reclaimed = await db.get(CatalogImportJob, job.id)
        assert reclaimed.checkpoint_offset == 7
        assert reclaimed.checkpoint["offset"] == 7


async def test_it_runs_once_per_process_and_before_the_first_claim(monkeypatch):
    """A job this process is running cannot be caught by it.

    That holds because the reclaim runs before there is one - so it must not
    run again later, when there might be.
    """
    calls: list[int] = []

    async def counting():
        calls.append(1)

    monkeypatch.setattr(worker, "_reclaim_leases_from_a_previous_process", counting)
    monkeypatch.setattr(worker.get_settings(), "academic_catalog_ingestion_enabled", True)
    monkeypatch.setattr(worker, "schedule_due", _none)
    monkeypatch.setattr(worker, "_claim", _none)

    await worker.run_once()
    await worker.run_once()
    await worker.run_once()
    assert calls == [1], "the reclaim ran more than once in one process"


async def _none(*_args, **_kwargs):
    return None

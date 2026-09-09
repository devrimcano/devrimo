"""Source admission, durable budgets, and resumable catalog traversal."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.academic_catalog import worker
from app.academic_catalog.models import CatalogHttpBudget, CatalogImportJob
from app.academic_catalog.source import client_type
from app.admin.directory import METU_ID, ensure_metu
from app.db.session import SessionLocal


async def test_full_school_import_does_not_reuse_directory_only_job():
    from app.academic_catalog.service import enqueue_import

    async with SessionLocal() as db:
        await ensure_metu(db)
        discovery = await enqueue_import(
            db, METU_ID, "20261", reason="Scheduled discovery",
            payload={"scheduled": True, "discovery_only": True},
        )
        full = await enqueue_import(db, METU_ID, "20261", reason="Import all departments")
        repeated = await enqueue_import(db, METU_ID, "20261", reason="Import all departments again")
        assert full.id != discovery.id
        assert repeated.id == full.id
        assert not full.payload.get("discovery_only")
        assert worker.initial_steps(full) == [{"tool": "get_departments_and_semesters", "values": {}}]


async def test_source_gate_counts_redirects_and_refuses_before_network():
    attempts = []
    sent = []

    async def gate():
        if len(attempts) >= 2:
            raise worker.ImportDeferred("daily_http_budget_exhausted")
        attempts.append(1)

    def serve(request):
        sent.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/finish"})
        return httpx.Response(200)

    client = client_type()(username="fixture", password="fixture", request_gate=gate)
    hooks = client._client.event_hooks
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(serve), event_hooks=hooks, follow_redirects=True)
    try:
        await client._client.get("https://example.invalid/start")
        with pytest.raises(worker.ImportDeferred):
            await client._client.get("https://example.invalid/denied")
        assert sent == ["/start", "/finish"]
        assert client._requests == 2
    finally:
        await client.aclose()


async def test_http_budget_cannot_be_overspent_by_concurrent_workers(monkeypatch):
    settings = worker.get_settings()
    monkeypatch.setattr(settings, "catalog_warm_hours", "0-24")
    monkeypatch.setattr(settings, "catalog_warm_daily_limit", 2)
    monkeypatch.setattr(settings, "catalog_warm_interval_seconds", 0)
    monkeypatch.setattr(settings, "catalog_warm_jitter_seconds", 0)
    async with SessionLocal() as db:
        await ensure_metu(db)
        await db.commit()
    results = await asyncio.gather(*(worker.admit_request(METU_ID, uuid4()) for _ in range(4)), return_exceptions=True)
    assert sum(value is None for value in results) == 2
    assert sum(isinstance(value, worker.ImportDeferred) for value in results) == 2
    async with SessionLocal() as db:
        budget = await db.scalar(select(CatalogHttpBudget).where(
            CatalogHttpBudget.organization_id == METU_ID,
            CatalogHttpBudget.budget_date == datetime.now(worker.ISTANBUL).date(),
        ))
        assert budget.attempted_count == 2


@pytest.mark.parametrize("listing_tool", ["list_program_courses", "get_thesis_courses"])
def test_job_expands_only_the_official_term_and_keeps_four_digit_codes(listing_tool):
    initial = worker.initial_steps(SimpleNamespace(term="20261", department=None, course_codes=[], payload={}))
    with pytest.raises(ValueError, match="official term"):
        worker.expand_steps(initial[0], {"semesters": [{"code": "20252"}], "departments": [{"code": "240"}]}, "20261")
    listings = worker.expand_steps(initial[0], {
        "semesters": [{"code": "20261"}], "departments": [{"code": "240"}],
    }, "20261")
    listing = next(step for step in listings if step["tool"] == listing_tool)
    steps = worker.expand_steps(listing, [{"course_code": "2402201"}], "20261")
    assert {step["values"]["course"] for step in steps} == {"2402201"}
    assert [step["tool"] for step in steps] == [
        "get_course_info", "get_course_prerequisites", "get_course_replacements",
    ]


async def test_expired_job_lease_can_resume_without_discarding_checkpoint():
    from datetime import UTC, timedelta

    async with SessionLocal() as db:
        await ensure_metu(db)
        job = CatalogImportJob(
            organization_id=METU_ID, term="20261", status="running",
            checkpoint={"steps": [{"tool": "get_course_info", "values": {"course": "2402201"}}], "offset": 0},
            lease_until=datetime.now(UTC) - timedelta(seconds=1),
            dedup_key="lease-test", reason="fixture",
        )
        db.add(job)
        await db.commit()
        job_id = job.id
    claimed = await worker._claim()
    assert claimed.id == job_id
    assert claimed.checkpoint["offset"] == 0
    assert claimed.checkpoint["steps"][0]["values"]["course"] == "2402201"
    assert claimed.lease_token is not None


async def test_reclaimed_lease_fences_previous_worker(monkeypatch):
    from datetime import UTC, timedelta

    settings = worker.get_settings()
    monkeypatch.setattr(settings, "catalog_warm_hours", "0-24")
    async with SessionLocal() as db:
        await ensure_metu(db)
        old_token = uuid4()
        job = CatalogImportJob(
            organization_id=METU_ID, term="20261", status="running",
            lease_token=old_token, lease_until=datetime.now(UTC) - timedelta(seconds=1),
            checkpoint={"offset": 3}, dedup_key="fence-test",
        )
        db.add(job)
        await db.commit()
        job_id = job.id
    claimed = await worker._claim()
    assert claimed.lease_token != old_token
    with pytest.raises(worker.LeaseLost):
        await worker._save(job_id, old_token, checkpoint={"offset": 99})
    with pytest.raises(worker.LeaseLost):
        await worker.admit_request(METU_ID, job_id, old_token)
    async with SessionLocal() as db:
        current = await db.get(CatalogImportJob, job_id)
        assert current.checkpoint == {"offset": 3}
        assert await db.scalar(select(CatalogHttpBudget)) is None

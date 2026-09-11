"""Source admission, durable budgets, and resumable catalog traversal."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.academic_catalog import worker
from app.academic_catalog.models import (
    CatalogCourse,
    CatalogDraft,
    CatalogHttpBudget,
    CatalogImportJob,
    CatalogTerm,
)
from app.academic_catalog.source import client_type
from app.admin.directory import METU_ID, ensure_metu
from app.db.session import SessionLocal


async def test_manual_import_completes_outside_hours_without_pacing_or_budget(monkeypatch):
    from app.academic_catalog.service import enqueue_import

    settings = worker.get_settings()
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", True)
    monkeypatch.setattr(settings, "catalog_warm_enabled", False)
    monkeypatch.setattr(settings, "catalog_warm_daily_limit", 0)
    monkeypatch.setattr(settings, "catalog_warm_interval_seconds", 3600)
    monkeypatch.setattr(worker, "_within_hours", lambda *_: False)
    schedule = AsyncMock()
    monkeypatch.setattr(worker, "schedule_due", schedule)
    monkeypatch.setattr(worker, "_account", AsyncMock(return_value=(uuid4(), object())))
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock(side_effect=AssertionError("Artificial delay")))

    class Source:
        def __init__(self, _secret, admission):
            self.admission = admission

        async def read(self, _tool, _values):
            await self.admission()
            return {"semesters": [{"code": "20261"}], "departments": []}

        async def aclose(self):
            pass

    monkeypatch.setattr(worker, "CatalogSource", Source)
    async with SessionLocal() as db:
        await ensure_metu(db)
        scheduled = await enqueue_import(db, METU_ID, "20261", department="240", payload={"scheduled": True})
        manual = await enqueue_import(db, METU_ID, "20261", requested_by=uuid4())
        await db.commit()
        manual_id, scheduled_id = manual.id, scheduled.id
    result = await worker.run_once()
    assert result.outcome == "completed"
    assert result.job_id == str(manual_id)
    schedule.assert_not_awaited()
    async with SessionLocal() as db:
        assert (await db.get(CatalogImportJob, scheduled_id)).status == "queued"
        assert (await db.get(CatalogImportJob, manual_id)).checkpoint_offset == 1
        assert await db.scalar(select(CatalogHttpBudget)) is None


async def test_manual_request_promotes_existing_scheduled_job():
    from app.academic_catalog.service import enqueue_import

    async with SessionLocal() as db:
        await ensure_metu(db)
        original = await enqueue_import(db, METU_ID, "20261", department="240", payload={"scheduled": True})
        original.checkpoint_offset = 2
        requested = await enqueue_import(db, METU_ID, "20261", department="240", requested_by=uuid4())
        await db.commit()
        assert requested.id == original.id
        assert requested.checkpoint_offset == 2
    claimed = await worker._claim(manual_only=True)
    assert claimed.id == original.id


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


async def test_import_dedup_race_preserves_callers_transaction(monkeypatch):
    from app.academic_catalog import service

    term = f"txn-{uuid4().hex[:12]}"
    async with SessionLocal() as competitor:
        await ensure_metu(competitor)
        winner = await service.enqueue_import(competitor, METU_ID, term, reason="Winning request")
        await competitor.commit()

    async with SessionLocal() as db:
        await ensure_metu(db)
        prior = CatalogImportJob(
            organization_id=METU_ID,
            term=term,
            status="queued",
            dedup_key=f"unrelated-{uuid4().hex}",
            reason="Must survive a dedup race",
        )
        db.add(prior)
        await db.flush()

        original_scalar = db.scalar
        calls = 0

        async def hide_precheck(statement, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return await original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(db, "scalar", hide_precheck)
        reused = await service.enqueue_import(db, METU_ID, term, reason="Losing request")
        assert reused.id == winner.id
        await db.commit()
        assert await db.get(CatalogImportJob, prior.id) is not None


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
    results = await asyncio.gather(*(worker.admit_request(METU_ID, None) for _ in range(4)), return_exceptions=True)
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
    if listing_tool == "get_thesis_courses":
        # Thesis rows are recorded by the listing observation itself; they are
        # deliberately not expanded into detail and rule reads.
        assert steps == []
        return
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


def test_catalog_pass_result_keeps_the_legacy_integer_contract():
    result = worker.CatalogPassResult(0, outcome="deferred", error_code="daily_http_budget_exhausted")
    assert isinstance(result, int)
    assert result == 0
    assert result.outcome == "deferred"
    assert result.error_code == "daily_http_budget_exhausted"


async def test_catalog_import_failed_job_stores_value_error_detail(monkeypatch):
    from app.academic_catalog.service import enqueue_import

    settings = worker.get_settings()
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", True)
    monkeypatch.setattr(settings, "catalog_warm_enabled", False)
    monkeypatch.setattr(worker, "_within_hours", lambda *_: True)
    monkeypatch.setattr(worker, "_account", AsyncMock(return_value=(uuid4(), object())))

    class Source:
        def __init__(self, _secret, _admission):
            pass

        async def read(self, _tool, _values):
            raise ValueError("Bad response payload from source")

        async def aclose(self):
            pass

    monkeypatch.setattr(worker, "CatalogSource", Source)

    async with SessionLocal() as db:
        await ensure_metu(db)
        job = await enqueue_import(db, METU_ID, "20261", department="240", requested_by=uuid4())
        await db.commit()
        job_id = job.id

    result = await worker.run_once()

    assert result.outcome == "failed"
    assert result.error_code == "ValueError"
    assert result.error_detail == "Bad response payload from source"

    async with SessionLocal() as db:
        updated = await db.get(CatalogImportJob, job_id)
        assert updated.error_code == "ValueError"
        assert updated.error_detail == "Bad response payload from source"


async def test_a_failed_import_with_progress_is_resumed_rather_than_restarted():
    """Twenty-four hours of answered pages are not thrown away by asking again.

    Only queued and running jobs were reused, so a failed one took its progress
    with it: the checkpoint recorded exactly which of 10048 steps it reached,
    and nothing could reach it. Asking for the same scope again built a fresh
    job that would re-walk every step METU had already answered.
    """
    from app.academic_catalog.service import enqueue_import

    async with SessionLocal() as db:
        await ensure_metu(db)
        first = await enqueue_import(db, METU_ID, "20261", department="240", requested_by=uuid4())
        await db.commit()
        first_id = first.id

    async with SessionLocal() as db:
        job = await db.get(CatalogImportJob, first_id)
        job.status = "failed"
        job.attempts = 3
        job.checkpoint_offset = 4817
        job.error_detail = "SAIS section restriction table could not be read"
        await db.commit()

    async with SessionLocal() as db:
        again = await enqueue_import(db, METU_ID, "20261", department="240", requested_by=uuid4())
        await db.commit()
        assert again.id == first_id, "a new job was created and 4817 answered steps were abandoned"
        assert again.status == "queued"
        assert again.checkpoint_offset == 4817, "the resumed job lost its place"
        assert again.attempts == 0, "the failed run's attempts were carried into the new one"
        assert again.error_detail is None


async def test_a_failed_import_that_never_started_is_built_again():
    """Nothing to resume, and a scope that is simply broken must not loop."""
    from app.academic_catalog.service import enqueue_import

    async with SessionLocal() as db:
        await ensure_metu(db)
        first = await enqueue_import(db, METU_ID, "20261", department="241", requested_by=uuid4())
        await db.commit()
        first_id = first.id

    async with SessionLocal() as db:
        job = await db.get(CatalogImportJob, first_id)
        job.status = "failed"
        job.attempts = 3
        job.checkpoint_offset = 0
        await db.commit()

    async with SessionLocal() as db:
        again = await enqueue_import(db, METU_ID, "20261", department="241", requested_by=uuid4())
        await db.commit()
        assert again.id != first_id, "a job that never got anywhere was resumed instead of rebuilt"


async def test_refusals_do_not_discard_an_import_that_has_already_answered(monkeypatch):
    """Fifteen unreadable pages must not throw away a day of work.

    The guard that fires when a pass answers nothing used to look only at the
    pass, and a pass is fifteen steps. A run of fifteen unreadable restriction
    tables is an ordinary thing for METU to have, and it killed the job: a
    whole-term import walked 4817 of 10048 steps over about twenty-four hours,
    hit such a run, exhausted its three attempts and was marked failed.

    A source that has answered 4817 times is not a source that is down, so a
    parse refusal after real progress is now recorded against its own step and
    the pass moves on.
    """
    from app.academic_catalog.service import enqueue_import

    settings = worker.get_settings()
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", True)
    monkeypatch.setattr(settings, "catalog_warm_enabled", False)
    monkeypatch.setattr(worker, "_within_hours", lambda *_: True)
    monkeypatch.setattr(worker, "_account", AsyncMock(return_value=(uuid4(), object())))

    class Refusing:
        def __init__(self, _secret, _admission):
            pass

        async def read(self, _tool, _values):
            raise ValueError("SAIS section restriction table could not be read")

        async def aclose(self):
            pass

    monkeypatch.setattr(worker, "CatalogSource", Refusing)

    async with SessionLocal() as db:
        await ensure_metu(db)
        job = await enqueue_import(
            db,
            METU_ID,
            "20261",
            course_codes=["2400201", "2400213"],
            requested_by=uuid4(),
        )
        await db.commit()
        job_id = job.id

    # Stand the job up mid-plan: this one has already answered for earlier
    # steps, which is the whole difference from the test above.
    async with SessionLocal() as db:
        started = await db.get(CatalogImportJob, job_id)
        assert len(worker.initial_steps(started)) > 3, "this test needs a plan it can start partway through"
        started.checkpoint_offset = 3
        await db.commit()

    result = await worker.run_once()

    assert result.outcome != "failed", f"a refusal after real progress ended the job: {result.error_detail}"

    async with SessionLocal() as db:
        updated = await db.get(CatalogImportJob, job_id)
        assert updated.status != "failed"
        # The cursor moved past the pages that would not be read, rather than
        # the job stopping on them.
        assert updated.checkpoint_offset > 3


async def test_a_failure_with_no_message_still_says_where_it_happened(monkeypatch):
    """What every catalog import on production actually did.

    Three jobs, all failed, error_code "ValueError" and error_detail null,
    because the exception carried no arguments. The admin panel showed a job
    that failed and no reason, while the reviewed catalog stayed empty.
    """
    from app.academic_catalog.service import enqueue_import

    settings = worker.get_settings()
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", True)
    monkeypatch.setattr(settings, "catalog_warm_enabled", False)
    monkeypatch.setattr(worker, "_within_hours", lambda *_: True)
    monkeypatch.setattr(worker, "_account", AsyncMock(return_value=(uuid4(), object())))

    class Source:
        def __init__(self, _secret, _admission):
            pass

        async def read(self, _tool, _values):
            raise ValueError

        async def aclose(self):
            pass

    monkeypatch.setattr(worker, "CatalogSource", Source)

    async with SessionLocal() as db:
        await ensure_metu(db)
        job = await enqueue_import(db, METU_ID, "20261", department="240", requested_by=uuid4())
        await db.commit()
        job_id = job.id

    result = await worker.run_once()

    assert result.outcome == "failed"
    assert result.error_code == "ValueError"
    assert result.error_detail
    assert "no message" in result.error_detail
    # The step it died on, which is the part someone can act on.
    assert "list_program_courses" in result.error_detail
    assert "department=240" in result.error_detail
    assert "semester=20261" in result.error_detail

    async with SessionLocal() as db:
        updated = await db.get(CatalogImportJob, job_id)
        assert updated.error_detail == result.error_detail


def test_serialize_import_job_uses_scalar_cursor_and_compact_checkpoint():
    from app.academic_catalog.service import serialize_import_job

    job = CatalogImportJob(
        organization_id=METU_ID,
        term="20261",
        status="running",
        checkpoint={
            "steps": [{"tool": "get_course_info", "values": {"course": "2402201"}}],
            "offset": 9,
            "retry_at": "2026-09-09T10:00:00Z",
            "conflicts": ["one"],
        },
        checkpoint_offset=0,
        dedup_key="serialize-cursor-test",
    )

    payload = serialize_import_job(job)

    assert payload["checkpoint"] == {
        "offset": 0,
        "total": 1,
        "phase": "get_course_info",
        "retry_at": "2026-09-09T10:00:00Z",
        "conflicts": ["one"],
    }
    assert "steps" not in payload["checkpoint"]


async def test_cursor_progress_does_not_rewrite_the_durable_step_plan():
    from datetime import UTC, timedelta

    async with SessionLocal() as db:
        await ensure_metu(db)
        token = uuid4()
        job = CatalogImportJob(
            organization_id=METU_ID,
            term="20261",
            status="running",
            lease_token=token,
            lease_until=datetime.now(UTC) + timedelta(minutes=5),
            checkpoint={"steps": [{"tool": "get_course_info", "values": {"course": "2402201"}}], "offset": 0},
            dedup_key="cursor-test",
        )
        db.add(job)
        await db.commit()
        job_id = job.id

    await worker._save_offset(job_id, token, 1, error_code=None)

    async with SessionLocal() as db:
        current = await db.get(CatalogImportJob, job_id)
        assert current.checkpoint["offset"] == 0
        assert current.checkpoint_offset == 1


async def test_schedule_due_caps_courses_globally_but_allows_one_discovery(monkeypatch):
    from app.academic_catalog import worker as catalog_worker

    async with SessionLocal() as db:
        await ensure_metu(db)
        terms = [
            CatalogTerm(organization_id=METU_ID, term_code="20261", is_current=True),
            CatalogTerm(organization_id=METU_ID, term_code="20252", is_current=True),
        ]
        db.add_all(terms)
        await db.flush()
        courses = []
        drafts = []
        for term, code in zip(terms, ("2402201", "2402202")):
            course = CatalogCourse(organization_id=METU_ID, course_code=code, department="240")
            courses.append(course)
            db.add(course)
            await db.flush()
            drafts.append(CatalogDraft(organization_id=METU_ID, term_id=term.id, course_id=course.id, data={}))
        db.add_all(drafts)
        await db.commit()

    settings = catalog_worker.get_settings()
    monkeypatch.setattr(settings, "catalog_warm_courses_per_pass", 1)
    monkeypatch.setattr(settings, "catalog_warm_discoveries_per_pass", 1)
    monkeypatch.setattr(catalog_worker, "_wanted_courses", lambda term: _empty_demand(term))
    scheduled = []

    async def enqueue(*args, **kwargs):
        scheduled.append((kwargs.get("payload") or {}).get("discovery_only", False))
        return None

    monkeypatch.setattr(catalog_worker.catalog_service, "enqueue_import", enqueue)
    assert await catalog_worker.schedule_due() == 2
    assert sum(not discovery for discovery in scheduled) == 1
    assert sum(bool(discovery) for discovery in scheduled) == 1


async def _empty_demand(_term):
    return {}


async def test_a_department_that_cannot_be_listed_does_not_end_the_import(monkeypatch):
    """One unreadable page is one page, not a failed import.

    METU's directory lists Actuarial Science; its course app answers
    "Information about the department could not be found." The parser refuses a
    page it cannot identify, which is right - but that refusal used to end the
    pass, and that programme sorts first, so the other 206 departments were
    never fetched and the reviewed catalog stayed empty. The same thing then
    happened 429 steps into the next attempt, on one section's restriction
    table, with 2467 courses already read.
    """
    from app.academic_catalog.service import enqueue_import

    settings = worker.get_settings()
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", True)
    monkeypatch.setattr(settings, "catalog_warm_enabled", False)
    monkeypatch.setattr(worker, "_within_hours", lambda *_: True)
    monkeypatch.setattr(worker, "_account", AsyncMock(return_value=(uuid4(), object())))

    seen = []

    class Source:
        def __init__(self, _secret, _admission):
            pass

        async def read(self, tool, values):
            seen.append((tool, values.get("department")))
            if tool == "list_program_courses":
                raise ValueError("SAIS course information page department identity is missing")
            return []

        async def aclose(self):
            pass

    monkeypatch.setattr(worker, "CatalogSource", Source)

    async with SessionLocal() as db:
        await ensure_metu(db)
        job = await enqueue_import(db, METU_ID, "20261", department="240", requested_by=uuid4())
        await db.commit()
        job_id = job.id

    result = await worker.run_once()

    # The listing was refused and the pass carried on to the next step.
    assert ("list_program_courses", "240") in seen
    assert ("get_thesis_courses", "240") in seen
    assert result.outcome != "failed"

    async with SessionLocal() as db:
        updated = await db.get(CatalogImportJob, job_id)
        assert updated.status in {"queued", "completed"}
        assert updated.checkpoint_offset >= 2

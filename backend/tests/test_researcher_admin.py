from uuid import UUID

from sqlalchemy import text

from app.config import get_settings
from app.db.session import SessionLocal, engine
from app.researchers.models import Researcher, ResearcherImportItem, ResearcherImportRun
from app.researchers.service import LOCK_ID
from tests.conftest import auth_header, new_user_id

BASE = "/api/v1/admin/researchers"


async def test_queue_resume_and_overlap(client, monkeypatch):
    uid = new_user_id()
    headers = auth_header(uid)
    assert (await client.get(BASE + "/dashboard", headers=headers)).status_code == 403
    assert (await client.post(BASE + "/runs", headers=headers, json={})).status_code == 403
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(uid))
    response = await client.post(BASE + "/runs", headers=headers, json={"limit": 10})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert response.json()["status"] == "queued"
    assert (await client.post(BASE + "/runs", headers=headers, json={})).status_code == 409
    async with SessionLocal() as db:
        run = await db.get(ResearcherImportRun, UUID(run_id))
        run.status = "running"
        run.discovery = {"complete": True, "reported_total": 2}
        db.add(
            ResearcherImportItem(
                run_id=run.id,
                source_id=1,
                status="completed",
                identity={"display_name": "Ada", "source_url": "https://avesis.metu.edu.tr/ada"},
                completed_sections=["general", "publications"],
            )
        )
        db.add(
            ResearcherImportItem(
                run_id=run.id,
                source_id=2,
                status="incomplete",
                identity={"display_name": "Bob", "source_url": "https://avesis.metu.edu.tr/bob"},
                errors=[{"section": "general", "error": "Timeout"}],
            )
        )
        await db.commit()
    overview = (await client.get(BASE + "/dashboard", headers=headers)).json()
    assert not overview["busy"]
    assert overview["runs"][0]["status"] == "interrupted"
    assert overview["runs"][0]["completed"] == 1
    items = (await client.get(BASE + f"/runs/{run_id}/items?errors_only=true", headers=headers)).json()
    assert items["total"] == 1 and items["items"][0]["name"] == "Bob"
    async with engine.connect() as lock:
        await lock.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_ID})
        try:
            assert (await client.post(BASE + f"/runs/{run_id}/resume", headers=headers, json={})).status_code == 409
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})
    resumed = await client.post(BASE + f"/runs/{run_id}/resume", headers=headers, json={})
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["completed"] == 1
    async with SessionLocal() as db:
        item = await db.get(ResearcherImportItem, (UUID(run_id), 1))
        assert item.completed_sections == ["general", "publications"]
        run = await db.get(ResearcherImportRun, UUID(run_id))
        assert run.discovery["complete"] and run.options["limit"] == 10
        run.status = "completed"
        await db.commit()
    assert (await client.post(BASE + f"/runs/{run_id}/resume", headers=headers, json={})).status_code == 409


async def test_directory_search_and_pagination(client, monkeypatch):
    uid = new_user_id()
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(uid))
    async with SessionLocal() as db:
        db.add_all(
            [
                Researcher(
                    id=i,
                    alias=f"p{i}",
                    name=f"Person {i:02}",
                    affiliation="100% Research" if i == 0 else "Science",
                    source_url=f"https://avesis.metu.edu.tr/p{i}",
                )
                for i in range(30)
            ]
        )
        await db.commit()
    headers = auth_header(uid)
    page = (await client.get(BASE + "?offset=25", headers=headers)).json()
    assert page["total"] == 30 and len(page["items"]) == 5
    search = (await client.get(BASE, params={"q": "%"}, headers=headers)).json()
    assert search["total"] == 1


async def test_worker_only_consumes_queued_runs(monkeypatch):
    from app.researchers import worker

    called = []

    async def fake_sync(*args, **kwargs):
        called.append(kwargs)

    monkeypatch.setattr(worker, "synchronize", fake_sync)
    async with SessionLocal() as db:
        run = ResearcherImportRun(status="interrupted")
        db.add(run)
        await db.commit()
        run_id = run.id
    await worker.run_next()
    assert not called
    async with SessionLocal() as db:
        run = await db.get(ResearcherImportRun, run_id)
        run.status = "queued"
        await db.commit()
    await worker.run_next()
    assert called[0]["resume"] == run_id and called[0]["queued_only"] is True


async def test_import_telemetry_success_resume_and_privacy(monkeypatch):
    from types import SimpleNamespace

    from structlog.contextvars import get_contextvars

    from app.researchers import service
    from tests.test_researcher_import import IDENTITY, FakeClient

    logs = []

    def log(event, **fields):
        logs.append({**get_contextvars(), "event": event, **fields})

    monkeypatch.setattr(service, "logger", SimpleNamespace(info=log, warning=log))
    events = []

    def capture(event, **properties):
        events.append((event, properties))

    async def discover(client):
        return [IDENTITY], {"complete": True, "reported_total": 1, "sitemap_only": []}

    monkeypatch.setattr(service, "discover", discover)
    monkeypatch.setattr(service, "capture", capture)
    monkeypatch.setattr("app.observability.client.capture", capture)
    result = await service.synchronize(engine, FakeClient(), progress=lambda _: None)
    assert [event for event, _ in events] == ["researcher_import_started", "background_job_completed"]
    terminal = events[-1][1]
    assert terminal["job_id"] == result["run_id"]
    assert terminal["outcome"] == "success"
    assert terminal["completed"] == 1 and terminal["selected"] == 1
    assert terminal["resumed"] is False and terminal["duration_seconds"] >= 0
    saved = [entry for entry in logs if entry["event"] == "researcher_section_saved"]
    assert saved and all(entry["researcher_id"] == IDENTITY["id"] for entry in saved)
    assert all(entry["job_id"] == result["run_id"] for entry in saved)
    assert logs[-1]["event"] == "researcher_import_finished"
    assert any(entry["event"] == "researcher_scrape_finished" for entry in logs)
    assert not any("content" in entry or "source_url" in entry for entry in logs)

    assert "errors" not in terminal and "options" not in terminal and "warnings" not in terminal
    # No duplicate job event when a completed run is submitted again.
    await service.synchronize(engine, FakeClient(), resume=UUID(result["run_id"]), progress=lambda _: None)
    assert len(events) == 2
    async with SessionLocal() as db:
        run = await db.get(ResearcherImportRun, UUID(result["run_id"]))
        run.status = "interrupted"
        await db.commit()
    await service.synchronize(engine, FakeClient(), resume=UUID(result["run_id"]), progress=lambda _: None)
    assert events[-1][1]["resumed"] is True


async def test_import_telemetry_failure_classification(monkeypatch):
    import asyncio

    import pytest

    from app.researchers import service
    from app.researchers.client import ImportFailure
    from tests.test_researcher_import import FakeClient

    events, issues = [], []
    monkeypatch.setattr(service, "capture", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.observability.client.capture", lambda event, **props: events.append(props))
    monkeypatch.setattr("app.observability.client.report_exception", lambda exc, **props: issues.append(str(exc)))
    for failure, outcome in [
        (ImportFailure("safe source failure"), "expected_failure"),
        (ValueError("secret source content"), "unexpected_failure"),
        (asyncio.CancelledError(), "cancelled"),
    ]:

        async def discover(client, failure=failure):
            raise failure

        monkeypatch.setattr(service, "discover", discover)
        if isinstance(failure, ImportFailure):
            await service.synchronize(engine, FakeClient(), progress=lambda _: None)
        else:
            with pytest.raises(type(failure)):
                await service.synchronize(engine, FakeClient(), progress=lambda _: None)
        assert events[-1]["outcome"] == outcome
        assert events[-1]["status"] == "interrupted"
    assert len(events) == 3 and len(issues) == 1
    assert "secret source content" not in str(events) + str(issues)

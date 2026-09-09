"""Removing a correction restores evidence without changing live history."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.academic_catalog import service
from app.academic_catalog.models import CatalogCourseRevision, CatalogDraft, CatalogSourceObservation
from app.academic_catalog.overrides import remove_overrides
from app.admin.directory import METU_ID, ensure_metu
from app.config import get_settings
from app.db.session import SessionLocal
from tests.conftest import auth_header, new_user_id


async def test_remove_override_restores_source_timestamp_and_survives_publication():
    user = uuid4()
    now = datetime.now(UTC)
    args = {"semester": "20261", "department": "240", "course": "2402201"}
    async with SessionLocal() as db:
        await ensure_metu(db)
        first = await service.ingest_observation(db, METU_ID, "get_course_info", args,
            {"course_code": "2402201", "title": "Original source", "credit": 3},
            now - timedelta(days=8), source_fetched_at=now - timedelta(days=8))
        draft = await db.get(CatalogDraft, first["draft_id"])
        await service.patch_draft(db, METU_ID, draft.id, expected_revision=draft.revision,
            patch={"title": "Admin correction"}, reason="Reviewed fixture", verify=True,
            verification_evidence="Registrar record", updated_by=user)
        published = await service.publish_drafts(db, METU_ID, "20261", [draft.id],
            expected_release_id=None, idempotency_key=str(uuid4()), reason="Publish correction", created_by=user)
        old_revision_id = draft.published_revision_id
        await db.commit()
        fetched = now - timedelta(days=4)
        result = await service.ingest_observation(db, METU_ID, "get_course_info", args,
            {"course_code": "2402201", "title": "New source title", "credit": 3},
            fetched, source_fetched_at=fetched)
        draft = await db.get(CatalogDraft, result["draft_id"])
        assert draft.data["title"] == "Admin correction"
        before = draft.revision
        with pytest.raises(HTTPException) as stale:
            await remove_overrides(db, METU_ID, draft.id, expected_revision=before - 1,
                fields=["title"], reason="Use source", updated_by=user)
        assert stale.value.status_code == 409
        await remove_overrides(db, METU_ID, draft.id, expected_revision=before,
            fields=["title"], reason="Registrar corrected source", updated_by=user)
        assert draft.data["title"] == "New source title"
        assert draft.field_overrides == {}
        assert draft.data["component_status"]["details"]["source_fetched_at"] == fetched.isoformat()
        assert not any(issue.get("code") == "source_conflict" for issue in draft.issues)
        assert (await db.get(CatalogCourseRevision, old_revision_id)).title == "Admin correction"
        observations_before = len((await db.scalars(select(CatalogSourceObservation))).all())
        await service.publish_drafts(db, METU_ID, "20261", [draft.id],
            expected_release_id=UUID(published["release_id"]), idempotency_key=str(uuid4()),
            reason="Publish restored source", created_by=user)
        await db.commit()
        assert len((await db.scalars(select(CatalogSourceObservation))).all()) == observations_before
        latest = await service.ingest_observation(db, METU_ID, "get_course_info", args,
            {"course_code": "2402201", "title": "Later source title", "credit": 3}, now, source_fetched_at=now)
        next_draft = await db.get(CatalogDraft, latest["draft_id"])
        assert next_draft.data["title"] == "Later source title"
        assert next_draft.field_overrides == {}


async def test_remove_override_without_source_leaves_unknown_instead_of_manual_value():
    async with SessionLocal() as db:
        await ensure_metu(db)
        draft = await service.create_draft(db, METU_ID, "20261", "2402201")
        await service.patch_draft(db, METU_ID, draft.id, expected_revision=draft.revision,
            patch={"title": "Manual only"}, reason="Fixture correction", verify=True,
            verification_evidence="Fixture review")
        await remove_overrides(db, METU_ID, draft.id, expected_revision=draft.revision,
            fields=["title"], reason="Remove correction", updated_by=uuid4())
        assert "title" not in draft.data
        assert draft.data["component_status"]["details"]["verified"] is False
        assert draft.data["component_status"]["details"]["source_fetched_at"] is None


async def test_override_removal_admin_http_contract(client, monkeypatch):
    user = new_user_id()
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(user))
    headers = auth_header(user)
    await client.get("/api/v1/profile", headers=headers)
    response = await client.post("/api/v1/admin/catalog/drafts", headers=headers, json={
        "term": "20261", "course_code": "2402201", "data": {"title": "Fixture"},
    })
    assert response.status_code == 201, response.text
    draft = response.json()
    response = await client.patch(f"/api/v1/admin/catalog/drafts/{draft['id']}", headers=headers, json={
        "expected_revision": draft["revision"], "patch": {"title": "Correction"}, "reason": "Admin correction",
    })
    assert response.status_code == 200, response.text
    draft = response.json()
    response = await client.post(f"/api/v1/admin/catalog/drafts/{draft['id']}/remove-overrides",
        headers=headers, json={"expected_revision": draft["revision"], "fields": ["title"], "reason": "Remove correction"})
    assert response.status_code == 200, response.text
    assert response.json()["field_overrides"] == {}
    assert response.json()["revision"] == draft["revision"] + 1


async def test_raw_observation_inspection_is_admin_and_organization_scoped(client, monkeypatch):
    from app.db.models import AccountDirectory, AdminMembership, AdminRole, Organization

    user = new_user_id()
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(user))
    headers = auth_header(user)
    await client.get("/api/v1/profile", headers=headers)
    async with SessionLocal() as db:
        result = await service.ingest_observation(db, METU_ID, "get_course_info",
            {"semester": "20261", "department": "240", "course": "2402201"},
            {"course_code": "2402201", "title": "Original raw title"}, datetime.now(UTC))
        await db.commit()
    url = f"/api/v1/admin/catalog/observations/{result['observation_id']}"
    response = await client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["payload"]["title"] == "Original raw title"
    assert response.json()["source_fetched_at"] is None
    assert response.json()["candidate_data"]["title"] == "Original raw title"
    outsider = new_user_id()
    other_headers = auth_header(outsider)
    await client.get("/api/v1/profile", headers=other_headers)
    assert (await client.get(url, headers=other_headers)).status_code == 403
    async with SessionLocal() as db:
        org = Organization(slug="raw-source-other", name="Other university")
        db.add(org)
        await db.flush()
        account = await db.get(AccountDirectory, outsider)
        account.organization_id = org.id
        db.add(AdminMembership(user_id=outsider, organization_id=org.id, role=AdminRole.operator, granted_by=user))
        await db.commit()
    assert (await client.get(url, headers=other_headers)).status_code == 404

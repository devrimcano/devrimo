"""Admin permissions and the actual Courses-tab request/response workflow."""

from uuid import uuid4

from sqlalchemy import select

from app.config import get_settings
from app.db.models import AccountDirectory, AdminAuditEvent, AdminMembership, AdminRole, Organization
from app.db.session import SessionLocal
from tests.conftest import auth_header, new_user_id


async def test_imports_fail_clearly_when_disabled_and_deduplicate_when_enabled(client, monkeypatch):
    from app.academic_catalog.models import CatalogImportJob

    user = new_user_id()
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_bootstrap_user_ids", str(user))
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", False)
    headers = auth_header(user)
    await client.get("/api/v1/profile", headers=headers)
    body = {"term": "20261", "reason": "Import all departments"}
    response = await client.post("/api/v1/admin/catalog/imports", headers=headers, json=body)
    assert response.status_code == 503
    assert "imports are disabled" in response.json()["detail"]
    async with SessionLocal() as db:
        assert await db.scalar(select(CatalogImportJob)) is None
    monkeypatch.setattr(settings, "academic_catalog_ingestion_enabled", True)
    first = await client.post("/api/v1/admin/catalog/imports", headers=headers, json=body)
    second = await client.post("/api/v1/admin/catalog/imports", headers=headers, json=body)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]


async def test_operator_can_inspect_but_cannot_edit_or_publish(client):
    user = new_user_id()
    headers = auth_header(user)
    await client.get("/api/v1/profile", headers=headers)
    async with SessionLocal() as db:
        account = await db.get(AccountDirectory, user)
        db.add(AdminMembership(user_id=user, organization_id=account.organization_id,
                               role=AdminRole.operator, granted_by=user))
        await db.commit()
    assert (await client.get("/api/v1/admin/catalog/courses?term=20261", headers=headers)).status_code == 200
    assert (await client.post("/api/v1/admin/catalog/drafts", headers=headers, json={
        "term": "20261", "course_code": "2402201", "data": {"title": "Unauthorized"},
    })).status_code == 403
    assert (await client.post("/api/v1/admin/catalog/publish", headers=headers, json={
        "term": "20261", "draft_ids": [str(uuid4())], "idempotency_key": str(uuid4()),
        "reason": "Unauthorized publication", "expected_release_id": None,
    })).status_code == 403


async def test_courses_tab_draft_publish_and_read_contract(client, monkeypatch):
    user = new_user_id()
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(user))
    domain_events = []
    monkeypatch.setattr(
        "app.academic_catalog.admin.capture",
        lambda event, **properties: domain_events.append((event, properties)),
    )
    headers = auth_header(user)
    await client.get("/api/v1/profile", headers=headers)
    response = await client.post("/api/v1/admin/catalog/drafts", headers=headers, json={
        "term": "20261", "course_code": "2402201", "reason": "Create reviewed course fixture",
        "data": {"title": "Fixture Course", "local_credits": 3, "ects": 5, "sections": []},
    })
    assert response.status_code == 201, response.text
    draft = response.json()
    draft_event = next(
        properties for event, properties in domain_events if properties["action"] == "catalog.draft.create"
    )
    assert draft_event["outcome"] == "success"
    assert draft_event["actor_user_id"] == str(user)
    assert draft_event["organization_id"]
    assert draft_event["draft_id"] == draft["id"]
    async with SessionLocal() as db:
        audit = await db.scalar(
            select(AdminAuditEvent)
            .where(AdminAuditEvent.action == "catalog.draft.create", AdminAuditEvent.actor_user_id == user)
            .order_by(AdminAuditEvent.created_at.desc())
        )
        assert audit is not None and audit.after_state["draft_id"] == draft["id"]
    response = await client.get("/api/v1/admin/catalog/courses?term=20261", headers=headers)
    assert response.status_code == 200, response.text
    listing = response.json()
    assert listing["total"] == 1
    assert listing["release_id"] is None
    assert listing["courses"][0]["draft_id"] == draft["id"]
    response = await client.get("/api/v1/admin/catalog/courses/2402201?term=20261", headers=headers)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert isinstance(detail["sections"], list)
    assert isinstance(detail["prerequisite_groups"], list)
    assert isinstance(detail["source_observations"], list)
    assert isinstance(detail["completeness"], dict)
    request = {"term": "20261", "draft_ids": [draft["id"]], "expected_release_id": None,
               "idempotency_key": str(uuid4()), "reason": "Publish fixture for contract validation"}
    response = await client.post("/api/v1/admin/catalog/publish", headers=headers, json=request)
    assert response.status_code == 200, response.text
    publish_event = next(properties for event, properties in domain_events if properties["action"] == "catalog.publish")
    assert publish_event["outcome"] == "success"
    assert publish_event["operation_id"] == response.json()["operation_id"]
    assert publish_event["release_id"] == response.json()["release_id"]
    replay = await client.post("/api/v1/admin/catalog/publish", headers=headers, json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json() == response.json()
    listing = (await client.get("/api/v1/admin/catalog/courses?term=20261", headers=headers)).json()
    assert listing["release_id"] is not None
    added = await client.post("/api/v1/admin/catalog/drafts", headers=headers, json={
        "term": "20261", "course_code": "2402202", "data": {"title": "New unpublished course"},
        "reason": "Import after initial publication",
    })
    assert added.status_code == 201, added.text
    drafts = (await client.get("/api/v1/admin/catalog/courses?term=20261&state=draft", headers=headers)).json()
    assert [row["course_code"] for row in drafts["courses"]] == ["2402202"]

    # A draft over an already-published course must still be editable: the
    # detail response keeps the published revision at the top level and nests
    # the active draft beside it.  Before that, the editor read the published
    # revision's number and every save was rejected.
    reopened = await client.post("/api/v1/admin/catalog/drafts", headers=headers, json={
        "term": "20261", "course_code": "2402201", "data": {"title": "Revised fixture"},
        "reason": "Revise an already published course",
    })
    assert reopened.status_code == 201, reopened.text
    revised = reopened.json()
    detail = (await client.get("/api/v1/admin/catalog/courses/2402201?term=20261", headers=headers)).json()
    assert detail["title"] == "Fixture Course", "the published revision stays at the top level"
    assert detail["draft"]["id"] == revised["id"]
    assert detail["draft_revision"] == revised["revision"]
    assert detail["state"] == "draft"
    edited = await client.patch(f"/api/v1/admin/catalog/drafts/{revised['id']}", headers=headers, json={
        "expected_revision": revised["revision"],
        "patch": {"title": "Revised fixture"},
        "reason": "Revise an already published course",
    })
    assert edited.status_code == 200, edited.text
    # The row is labelled "draft", so it must show what the draft holds rather
    # than the published revision behind it.
    rows = (await client.get("/api/v1/admin/catalog/courses?term=20261", headers=headers)).json()
    revised_row = next(row for row in rows["courses"] if row["course_code"] == "2402201")
    assert revised_row["state"] == "draft"
    assert revised_row["draft_id"] == revised["id"]
    assert revised_row["title"] == "Revised fixture"
    # The release history has to say how many courses each release carries.
    releases = (await client.get("/api/v1/admin/catalog/releases?term=20261", headers=headers)).json()
    assert releases["releases"][0]["active"] is True
    assert releases["releases"][0]["course_count"] == 1


async def test_campus_admin_cannot_read_or_patch_another_organizations_draft(client, monkeypatch):
    admin = new_user_id()
    outsider = new_user_id()
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(admin))
    for user in (admin, outsider):
        await client.get("/api/v1/profile", headers=auth_header(user))
    created = await client.post("/api/v1/admin/catalog/drafts", headers=auth_header(admin), json={
        "term": "20261", "course_code": "2402201", "data": {"title": "METU only"},
        "reason": "Tenant boundary fixture",
    })
    assert created.status_code == 201, created.text
    draft = created.json()
    async with SessionLocal() as db:
        org = Organization(slug="catalog-other", name="Other university")
        db.add(org)
        await db.flush()
        account = await db.get(AccountDirectory, outsider)
        account.organization_id = org.id
        db.add(AdminMembership(user_id=outsider, organization_id=org.id,
                               role=AdminRole.campus_admin, granted_by=admin))
        await db.commit()
    headers = auth_header(outsider)
    listed = await client.get("/api/v1/admin/catalog/courses?term=20261", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["courses"] == []
    assert (await client.get("/api/v1/admin/catalog/courses/2402201?term=20261", headers=headers)).status_code == 404
    patched = await client.patch(f"/api/v1/admin/catalog/drafts/{draft['id']}", headers=headers, json={
        "expected_revision": draft["revision"], "patch": {"title": "Cross tenant edit"},
        "reason": "Must be rejected",
    })
    assert patched.status_code == 404, patched.text

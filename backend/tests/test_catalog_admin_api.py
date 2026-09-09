"""Admin permissions and the actual Courses-tab request/response workflow."""

from uuid import uuid4

from app.config import get_settings
from app.db.models import AccountDirectory, AdminMembership, AdminRole, Organization
from app.db.session import SessionLocal
from tests.conftest import auth_header, new_user_id


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
    headers = auth_header(user)
    await client.get("/api/v1/profile", headers=headers)
    response = await client.post("/api/v1/admin/catalog/drafts", headers=headers, json={
        "term": "20261", "course_code": "2402201", "reason": "Create reviewed course fixture",
        "data": {"title": "Fixture Course", "local_credits": 3, "ects": 5, "sections": []},
    })
    assert response.status_code == 201, response.text
    draft = response.json()
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

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.config import get_settings
from app.core.crypto import decrypt_secret
from app.db.models import CourseGroupLink, Organization
from app.db.session import SessionLocal
from app.knowledge.retrieval import SearchFilters, search_knowledge
from tests.conftest import auth_header, new_user_id
from tests.test_campus_intelligence import _publish_turkish_records


async def test_course_group_update_preserves_secret_and_enforces_access(client, monkeypatch):
    admin_id = new_user_id()
    stranger_id = new_user_id()
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(admin_id))
    await client.get("/api/v1/profile", headers=auth_header(admin_id))
    created = await client.post(
        "/api/v1/admin/course-groups",
        headers=auth_header(admin_id),
        json={"course_code": "CENG213", "invite_url": "https://chat.whatsapp.com/original"},
    )
    assert created.status_code == 201, created.text
    group_id = UUID(created.json()["id"])
    path = f"/api/v1/admin/course-groups/{group_id}"
    body = {
        "course_code": "ceng 223",
        "section": "2",
        "active": False,
        "valid_until": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
    }
    denied = await client.put(path, headers=auth_header(stranger_id), json=body)
    assert denied.status_code == 403
    updated = await client.put(path, headers=auth_header(admin_id), json=body)
    assert updated.status_code == 200, updated.text
    assert "whatsapp" not in updated.text
    async with SessionLocal() as db:
        group = await db.get(CourseGroupLink, group_id)
        assert group.course_code == "CENG223"
        assert group.section == "2"
        assert group.active is False
        assert group.valid_until is not None
        assert decrypt_secret(group.invite_url_enc) == "https://chat.whatsapp.com/original"
    replacement = {**body, "active": True, "valid_until": None, "invite_url": "https://chat.whatsapp.com/replacement"}
    assert (await client.put(path, headers=auth_header(admin_id), json=replacement)).status_code == 200
    async with SessionLocal() as db:
        group = await db.get(CourseGroupLink, group_id)
        assert group.active is True
        assert group.valid_until is None
        assert decrypt_secret(group.invite_url_enc) == replacement["invite_url"]
        org = Organization(id=uuid4(), slug=f"other-{uuid4().hex}", name="Other campus")
        db.add(org)
        await db.flush()
        group.organization_id = org.id
        await db.commit()
    assert (await client.put(path, headers=auth_header(admin_id), json=body)).status_code == 404


async def test_search_filters_restrict_candidates_before_ranking():
    org = await _publish_turkish_records({"library": "Kütüphaneye yeni kitaplar geldi."})
    async with SessionLocal() as db:
        matches = await search_knowledge(db, "kütüphane", organization_id=org.id)
        assert matches
        source_id = UUID(matches[0]["source_id"])
        filtered = await search_knowledge(
            db,
            "kütüphane",
            SearchFilters(source_id=source_id, language="tr", record_types=("announcement",)),
            organization_id=org.id,
        )
        assert filtered and all(item["source_id"] == str(source_id) for item in filtered)
        for filters in (
            SearchFilters(source_id=uuid4()),
            SearchFilters(language="en"),
            SearchFilters(record_types=("event",)),
        ):
            assert await search_knowledge(db, "kütüphane", filters, organization_id=org.id) == []


async def test_workspace_lists_unpublished_revisions_and_filters_activity(client, monkeypatch):
    admin_id = new_user_id()
    headers = auth_header(admin_id)
    monkeypatch.setattr(get_settings(), "admin_bootstrap_user_ids", str(admin_id))
    await client.get("/api/v1/profile", headers=headers)
    config = {"records": [{"title": "Library notice", "content": "Library is open."}]}
    source_ids = []
    for name in ("Library", "Calendar"):
        created = await client.post(
            "/api/v1/admin/sources",
            headers=headers,
            json={"name": name, "kind": "curated", "config": config},
        )
        assert created.status_code == 201, created.text
        source_id = created.json()["id"]
        source_ids.append(source_id)
        detail = (await client.get(f"/api/v1/admin/sources/{source_id}", headers=headers)).json()
        revision_id = detail["revision_history"][0]["id"]
        published = await client.post(
            f"/api/v1/admin/sources/{source_id}/revisions/{revision_id}/publish",
            headers=headers,
        )
        assert published.status_code == 200, published.text
    revised = await client.post(
        f"/api/v1/admin/sources/{source_ids[0]}/revisions",
        headers=headers,
        json={"config": config},
    )
    assert revised.status_code == 201
    sources = (await client.get("/api/v1/admin/sources", headers=headers)).json()["items"]
    library = next(item for item in sources if item["id"] == source_ids[0])
    assert library["status"] == "published"
    assert library["draft_revisions"] == 1
    assert next(item for item in sources if item["id"] == source_ids[1])["draft_revisions"] == 0
    for params in ({"source_id": source_ids[0]}, {"source_name": "library"}):
        jobs = (await client.get("/api/v1/admin/ingestion-jobs", headers=headers, params=params)).json()["items"]
        assert len(jobs) == 1 and jobs[0]["source_id"] == source_ids[0]
    empty = await client.get("/api/v1/admin/ingestion-jobs?job_status=completed", headers=headers)
    assert empty.json()["items"] == []
    first = (await client.get("/api/v1/admin/ingestion-jobs?limit=1", headers=headers)).json()["items"]
    second = (await client.get("/api/v1/admin/ingestion-jobs?limit=1&offset=1", headers=headers)).json()["items"]
    assert first[0]["id"] != second[0]["id"]

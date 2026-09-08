from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.db.session import SessionLocal
from app.student.workspace import read_resource, update_resource


async def test_preferences_are_revisioned_idempotent_and_user_scoped():
    owner, other = uuid4(), uuid4()
    async with SessionLocal() as db:
        first = await update_resource(
            db, owner, "preference", "interests", {"value": {"topics": ["robotics"]}}, 0, "one"
        )
        assert first["revision"] == 1
        retry = await update_resource(
            db, owner, "preference", "interests", {"value": {"topics": ["robotics"]}}, 0, "one"
        )
        assert retry == first
        assert (await read_resource(db, other, "preference", "interests"))["data"]["value"] is None
        with pytest.raises(HTTPException) as conflict:
            await update_resource(db, owner, "preference", "interests", {"value": {}}, 0, "two")
        assert conflict.value.status_code == 409
        await db.rollback()
        with pytest.raises(HTTPException) as duplicate:
            await update_resource(db, owner, "preference", "interests", {"value": {}}, 1, "one")
        assert duplicate.value.status_code == 409


async def test_preference_changes_cannot_edit_academic_data_or_store_credentials():
    async with SessionLocal() as db:
        for key, value in (("transcript", {}), ("interests", {"password": "secret"})):
            with pytest.raises(HTTPException) as rejected:
                await update_resource(db, uuid4(), "preference", key, {"value": value}, 0, "request")
            assert rejected.value.status_code == 422


async def test_update_state_requires_visible_campus_record():
    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as hidden:
            await update_resource(db, uuid4(), "update", str(uuid4()), {"read": True}, 0, "request")
        assert hidden.value.status_code == 404

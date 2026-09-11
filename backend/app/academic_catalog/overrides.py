"""Explicitly retire admin corrections without rewriting published history."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.academic_catalog import service
from app.academic_catalog.models import (
    CatalogAdminOverride,
    CatalogCourse,
    CatalogSourceObservation,
    CatalogTerm,
)


async def remove_overrides(db, organization_id: UUID, draft_id: UUID, *,
                           expected_revision: int, fields: list[str], reason: str,
                           updated_by: UUID):
    draft = await service.get_draft(db, organization_id, draft_id, for_update=True)
    if draft is None:
        raise HTTPException(404, "Draft not found")
    if draft.state != "draft":
        raise HTTPException(409, "Only an active draft can be edited")
    if draft.revision != expected_revision:
        raise service.CatalogConflict("Draft changed since it was read", current_revision=draft.revision)
    selected = set(fields)
    overrides = dict(draft.field_overrides or {})
    if not selected or not selected.issubset(overrides):
        raise HTTPException(422, "Select existing override fields")
    course = await db.get(CatalogCourse, draft.course_id)
    term = await db.get(CatalogTerm, draft.term_id)
    observations = (await db.scalars(select(CatalogSourceObservation).where(
        CatalogSourceObservation.organization_id == organization_id,
        CatalogSourceObservation.term == term.term_code,
        or_(CatalogSourceObservation.course_code == course.course_code,
            CatalogSourceObservation.course_code.is_(None)),
    ).order_by(CatalogSourceObservation.observed_at, CatalogSourceObservation.created_at,
               CatalogSourceObservation.id))).all()

    # Replay the retained parsed evidence, without overrides or a new fetch.
    # A later failed read still makes its component unknown, even if an older
    # successful response supplies the last known value.
    source = SimpleNamespace(data={}, field_overrides={}, issues=[], revision=1)
    restored_ids = []
    for observation in observations:
        candidate = observation.candidate_data or {}
        if observation.course_code is None:
            matched = next((item for item in candidate.get("courses", [])
                            if isinstance(item, dict)
                            and service._course_code(item, None) == course.course_code), None)
            if matched is None:
                continue
            candidate = dict(matched.get("data") or {})
        else:
            candidate = dict(candidate)
            candidate.pop("courses", None)
        await service._merge_observation_into_draft(
            source, candidate, observation.issues or [],
            component=service._tool_component(observation.tool),
            status_value=observation.status, observed_at=observation.observed_at,
            source_fetched_at=observation.source_fetched_at,
        )
        restored_ids.append(str(observation.id))

    data = dict(draft.data or {})
    component_status = dict(data.get("component_status") or {})
    now = datetime.now(UTC)
    affected = set()
    for field in selected:
        value = overrides.pop(field)
        if field in source.data:
            data[field] = source.data[field]
        else:
            data.pop(field, None)
        affected.add(service.FIELD_COMPONENT_MAP.get(field, field))
        if field == "sections":
            affected.add("constraints")
        records = (await db.scalars(select(CatalogAdminOverride).where(
            CatalogAdminOverride.organization_id == organization_id,
            CatalogAdminOverride.draft_id == draft.id,
            CatalogAdminOverride.field_name == field,
            CatalogAdminOverride.active.is_(True),
        ))).all()
        for record in records:
            record.active = False
            record.removed_at = now
        # A separate removal record also documents inherited corrections,
        # whose original published draft and audit rows remain intact.
        db.add(CatalogAdminOverride(
            organization_id=organization_id, term_id=draft.term_id,
            course_id=draft.course_id, draft_id=draft.id, field_name=field,
            value={"removed_override": value, "restored_source": field in source.data},
            reason=reason, active=False, removed_at=now, created_by=updated_by,
        ))
    remaining_components = {service.FIELD_COMPONENT_MAP.get(field, field) for field in overrides}
    if "sections" in overrides:
        remaining_components.add("constraints")
    for component in affected:
        meta = dict((source.data.get("component_status") or {}).get(component) or {
            "component": component, "verified": False, "fresh": False,
            "source_status": "unknown", "source_fetched_at": None, "observed_at": None,
        })
        if component in remaining_components:
            meta.update(verified=False, fresh=False)
        component_status[component] = meta
    data["component_status"] = component_status
    data["_source_observation_ids"] = list(dict.fromkeys(
        [*(data.get("_source_observation_ids") or []), *restored_ids]
    ))
    draft.data = data
    draft.field_overrides = overrides
    draft.issues = [issue for issue in (draft.issues or [])
                    if not (isinstance(issue, dict) and issue.get("code") == "source_conflict"
                            and issue.get("field") in selected)]
    draft.reason = reason
    draft.updated_by = updated_by
    draft.revision += 1
    await db.flush()
    return draft

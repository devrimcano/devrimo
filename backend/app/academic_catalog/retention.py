"""Reclaim unreferenced failed observations without changing catalog history."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db.session import SessionLocal


async def sweep_failed_observations() -> int:
    cutoff = datetime.now(UTC) - timedelta(days=30)
    async with SessionLocal() as db:
        result = await db.execute(text("""
            DELETE FROM catalog_source_observations WHERE id IN (
              SELECT o.id FROM catalog_source_observations o
              WHERE o.created_at < :cutoff AND o.status NOT IN ('success','empty')
                AND NOT EXISTS (SELECT 1 FROM catalog_drafts d
                  WHERE (d.data->'_source_observation_ids') ? o.id::text)
                AND NOT EXISTS (SELECT 1 FROM catalog_course_revisions r WHERE r.source_observation_id=o.id)
                AND NOT EXISTS (SELECT 1 FROM catalog_sections r WHERE r.source_observation_id=o.id)
                AND NOT EXISTS (SELECT 1 FROM catalog_restrictions r WHERE r.source_observation_id=o.id)
                AND NOT EXISTS (SELECT 1 FROM catalog_prerequisite_groups r WHERE r.source_observation_id=o.id)
                AND NOT EXISTS (SELECT 1 FROM catalog_course_replacements r WHERE r.source_observation_id=o.id)
              ORDER BY o.created_at LIMIT 1000
            )
        """), {"cutoff": cutoff})
        await db.commit()
        return result.rowcount or 0

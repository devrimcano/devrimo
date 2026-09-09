"""Reclaim superseded, unreferenced evidence without changing catalog history."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db.session import SessionLocal


async def sweep_failed_observations() -> int:
    # Keep the entrypoint for the existing maintenance worker. Successful
    # evidence is eligible only after 90 days and only if replaced by another
    # successful observation of the exact same source request. In particular,
    # an old but latest source answer must remain available for override replay.
    cutoff = datetime.now(UTC) - timedelta(days=30)
    success_cutoff = datetime.now(UTC) - timedelta(days=90)
    async with SessionLocal() as db:
        result = await db.execute(text("""
            DELETE FROM catalog_source_observations WHERE id IN (
              SELECT o.id FROM catalog_source_observations o
              WHERE (
                (o.created_at < :cutoff AND o.status NOT IN ('success','empty'))
                OR (o.created_at < :success_cutoff AND o.status IN ('success','empty')
                  AND EXISTS (SELECT 1 FROM catalog_source_observations newer
                    WHERE newer.organization_id=o.organization_id
                      AND newer.tool=o.tool AND newer.arguments=o.arguments
                      AND newer.status IN ('success','empty')
                      AND newer.observed_at > o.observed_at))
              )
                AND NOT EXISTS (SELECT 1 FROM catalog_drafts d
                  WHERE (d.data->'_source_observation_ids') ? o.id::text)
                -- Draft provenance is bounded to the newest 100 observations.
                -- Once that bound is crossed, a draft can still contain fields
                -- merged from this observation without retaining its id. Keep
                -- evidence while a same-term draft could depend on it; listing
                -- observations have no course code and therefore protect the
                -- whole term, while detail observations protect one course.
                AND (
                  o.status NOT IN ('success','empty')
                  OR NOT EXISTS (
                    SELECT 1
                    FROM catalog_drafts d
                    JOIN catalog_terms t ON t.id = d.term_id
                    LEFT JOIN catalog_courses c ON c.id = d.course_id
                    WHERE d.organization_id = o.organization_id
                      AND t.organization_id = o.organization_id
                      AND t.term_code = o.term
                      AND (o.course_code IS NULL OR c.course_code = o.course_code)
                  )
                )
                AND NOT EXISTS (SELECT 1 FROM catalog_course_revisions r WHERE r.source_observation_id=o.id)
                AND NOT EXISTS (SELECT 1 FROM catalog_sections r WHERE r.source_observation_id=o.id)
                AND NOT EXISTS (SELECT 1 FROM catalog_restrictions r WHERE r.source_observation_id=o.id)
                AND NOT EXISTS (SELECT 1 FROM catalog_prerequisite_groups r WHERE r.source_observation_id=o.id)
                AND NOT EXISTS (SELECT 1 FROM catalog_course_replacements r WHERE r.source_observation_id=o.id)
              ORDER BY o.created_at LIMIT 1000
            )
        """), {"cutoff": cutoff, "success_cutoff": success_cutoff})
        await db.commit()
        return result.rowcount or 0

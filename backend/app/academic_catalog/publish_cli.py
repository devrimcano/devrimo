"""Publish every currently valid draft for one term as a single release.

Dry-run unless ``--apply`` is supplied.  This is an operator command for the
initial catalog cutover; ordinary publication continues to use the admin API.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.academic_catalog import service
from app.academic_catalog.models import CatalogDraft, CatalogTerm, CatalogTermActiveRelease
from app.admin.directory import METU_ID
from app.config import get_settings
from app.db.engine import database_pool_options, postgres_connect_args


async def run(args: argparse.Namespace) -> None:
    organization_id = UUID(args.organization)
    term_code = service.normalize_term(args.term)
    settings = get_settings()
    database_url = settings.database_migration_url
    if not database_url:
        raise SystemExit("DATABASE_MIGRATION_URL is required for catalog publication")
    engine = create_async_engine(
        database_url,
        **database_pool_options(settings),
        connect_args=postgres_connect_args(database_url),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            term = await db.scalar(
                select(CatalogTerm).where(
                    CatalogTerm.organization_id == organization_id,
                    CatalogTerm.term_code == term_code,
                )
            )
            if term is None:
                raise SystemExit(f"Academic term {term_code} was not found")
            drafts = list(
                (
                    await db.scalars(
                        select(CatalogDraft)
                        .where(
                            CatalogDraft.organization_id == organization_id,
                            CatalogDraft.term_id == term.id,
                            CatalogDraft.state == "draft",
                        )
                        .order_by(CatalogDraft.course_id, CatalogDraft.id)
                    )
                ).all()
            )
            ready = [draft for draft in drafts if not service._blocking_draft_issues(draft)]
            blocked = len(drafts) - len(ready)
            pointer = await db.scalar(
                select(CatalogTermActiveRelease).where(
                    CatalogTermActiveRelease.organization_id == organization_id,
                    CatalogTermActiveRelease.term_id == term.id,
                )
            )
            report: dict[str, object] = {
                "mode": "apply" if args.apply else "dry_run",
                "term": term_code,
                "drafts": len(drafts),
                "ready": len(ready),
                "blocked": blocked,
                "active_release_id": str(pointer.release_id) if pointer else None,
            }
            if not args.apply:
                print(json.dumps(report, indent=2))
                return
            if not ready:
                if pointer is None:
                    raise SystemExit("No publishable drafts and no active release; catalog cutover refused")
                report["status"] = "already_published"
                print(json.dumps(report, indent=2))
                return
            digest = hashlib.sha256(",".join(str(draft.id) for draft in ready).encode()).hexdigest()[:24]
            result = await service.publish_drafts(
                db,
                organization_id,
                term_code,
                [draft.id for draft in ready],
                expected_release_id=pointer.release_id if pointer else None,
                idempotency_key=f"initial-bulk-{term_code}-{digest}",
                reason=args.reason,
                created_by=None,
                acknowledge_conflicts=True,
            )
            await db.commit()
            report.update(
                status=result.get("status"),
                release_id=result.get("release_id"),
                published=len(result.get("published_draft_ids") or []),
            )
            print(json.dumps(report, indent=2, default=str))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", default=str(METU_ID))
    parser.add_argument("--term", required=True)
    parser.add_argument("--reason", default="Initial production catalog cache activation")
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()

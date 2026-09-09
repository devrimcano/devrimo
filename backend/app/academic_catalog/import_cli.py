"""Backfill shared cache observations into drafts; dry-run unless --apply.

The optional source URL is supplied through an environment variable and never
printed. The source transaction is read-only, including when source=destination.
No backfill publishes or claims an imported observation was freshly fetched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.academic_catalog.backfill import recover
from app.academic_catalog.service import ingest_observation
from app.admin.directory import METU_ID
from app.config import get_settings
from app.db.engine import postgres_connect_args
from app.db.session import SessionLocal


async def run(args):
    source_url = os.environ.get(args.source_url_env) if args.source_url_env else get_settings().database_url
    if not source_url:
        raise ValueError("Source database URL environment variable is unset")
    if source_url.startswith("postgresql://"):
        source_url = source_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    source = create_async_engine(source_url, connect_args=postgres_connect_args(source_url))
    try:
        async with source.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            rows = [dict(row) for row in (await conn.execute(text(
                "SELECT key_hash,payload,updated_at FROM schedule_data_cache "
                "WHERE namespace='course-catalog' AND owner_hash IS NULL ORDER BY updated_at,key_hash"
            ))).mappings()]
            await conn.rollback()
    finally:
        await source.dispose()
    accepted, unresolved, counts = recover(rows, args.term)
    applied = 0
    if args.apply:
        for row in accepted:
            observed = row["updated_at"]
            if isinstance(observed, str):
                observed = datetime.fromisoformat(observed)
            async with SessionLocal() as db:
                await ingest_observation(db, UUID(args.organization), row["tool"], row["values"], row["payload"],
                                         observed, source_fetched_at=None)
                await db.commit()
                applied += 1
    report = {"mode": "applied_to_drafts" if args.apply else "dry_run", "source_rows": len(rows),
              "recoverable_rows": len(accepted), "unresolved_rows": unresolved, "by_tool": counts,
              "applied": applied, "published": 0}
    print(json.dumps(report, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url-env", help="Name of an environment variable containing the source PostgreSQL URL")
    parser.add_argument("--organization", default=str(METU_ID))
    parser.add_argument("--term", action="append", default=[], help="Additional known official term code")
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()

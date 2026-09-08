"""Run from backend: python -m app.researchers.cli sync --dry-run --limit 1."""

import argparse
import asyncio
import json
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.db.engine import postgres_connect_args
from app.logging import configure_logging
from app.observability.client import shutdown
from app.observability.logs import shutdown as shutdown_logs
from app.researchers.client import AvesisClient, ImportFailure
from app.researchers.discovery import discover
from app.researchers.parser import parse_page
from app.researchers.service import collect_section, report, synchronize


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("--dry-run", action="store_true", help="Fetch and parse without opening the database")
    sync.add_argument("--limit", type=int)
    sync.add_argument("--researcher-id", type=int)
    sync.add_argument("--resume", type=uuid.UUID)
    status = commands.add_parser("status")
    status.add_argument("run_id", type=uuid.UUID)
    args = parser.parse_args()
    if args.command == "sync":
        if args.limit is not None and args.limit < 1:
            parser.error("--limit must be positive")
        if args.resume and (args.dry_run or args.limit or args.researcher_id):
            parser.error("--resume uses saved options and cannot be combined with other flags")
    return args


async def execute(args):
    settings = get_settings()
    # Transport debug output can include Proxy-Authorization; never enable it here.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).disabled = True
    proxy = settings.avesis_proxy_url.get_secret_value() if settings.avesis_proxy_url else None
    if args.command == "sync" and args.dry_run:
        async with AvesisClient(proxy) as client:
            identities, discovery = await discover(client)
            chosen = [i for i in identities if args.researcher_id is None or i["id"] == args.researcher_id]
            if args.researcher_id and not chosen:
                raise ImportFailure("Researcher ID is not in the public directory")
            previews = []
            for ident in chosen[: args.limit]:
                page = await client.fetch(ident["source_url"])
                parsed = parse_page(page.body, page.url, ident["alias"], expected_id=ident["id"])
                counts = {}
                for key, url in parsed.sections.items():
                    content = await collect_section(client, url, ident)
                    counts[key] = {"items": len(content["items"]), "detail_links": len(content["detail_urls"])}
                previews.append({"id": ident["id"], "fields": parsed.fields, "sections": counts})
            return {"dry_run": True, "discovery": discovery, "profiles": previews, "requests": client.requests}
    if not settings.database_url.startswith("postgresql+"):
        raise ImportFailure("AVESIS requires the project's PostgreSQL DATABASE_URL; SQLite is not supported")
    engine = create_async_engine(
        settings.database_url, pool_pre_ping=True, connect_args=postgres_connect_args(settings.database_url)
    )
    try:
        if args.command == "status":
            async with AsyncSession(engine) as db:
                return await report(db, args.run_id)
        async with AvesisClient(proxy) as client:
            return await synchronize(
                engine,
                client,
                limit=args.limit,
                researcher_id=args.researcher_id,
                resume=args.resume,
                progress=lambda line: print(line, flush=True),
            )
    finally:
        await engine.dispose()


def main():
    configure_logging()
    try:
        result = asyncio.run(execute(arguments()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status", "completed") == "completed" else 2
    except ImportFailure as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    except KeyboardInterrupt:
        print("Import stopped. Resume using the printed run ID.")
        return 130
    except Exception as exc:
        # Database/transport exceptions may contain DSNs or proxy credentials.
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "message": "Check database availability and migrations. Resume using the printed run ID.",
                }
            )
        )
        return 1

    finally:
        shutdown_logs()
        shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

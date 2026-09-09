"""Database checkpoints and deterministic refreshes for the public directory."""

import asyncio
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.contextvars import bound_contextvars

from app.logging import get_logger
from app.observability.client import capture
from app.observability.jobs import observed_job
from app.researchers.client import ImportFailure, StopImport
from app.researchers.discovery import discover
from app.researchers.models import Researcher, ResearcherImportItem, ResearcherImportRun, ResearcherSection
from app.researchers.parser import UUID, canonical, combine, digest, parse_page

logger = get_logger(__name__)

LOCK_ID = 680923014  # One AVESIS importer per database, including limited runs.


def now():
    return datetime.now(UTC)


def _run_telemetry(run: ResearcherImportRun) -> dict[str, str | None]:
    """Read the queued admin correlation envelope without changing the schema."""
    metadata = (run.options or {}).get("_telemetry")
    if not isinstance(metadata, dict):
        return {"actor_user_id": None, "organization_id": None, "request_id": None}
    values: dict[str, str | None] = {}
    for key in ("actor_user_id", "organization_id", "request_id"):
        value = metadata.get(key)
        values[key] = str(value).strip()[:128] if value else None
    return values


async def save_section(db, source_id, key, url, content) -> bool:
    content_hash = digest(content)
    previous = await db.scalar(
        select(ResearcherSection.content_hash).where(
            ResearcherSection.researcher_id == source_id,
            ResearcherSection.section == key,
            ResearcherSection.language == "en",
        )
    )
    stmt = insert(ResearcherSection).values(
        id=uuid.uuid4(),
        researcher_id=source_id,
        section=key,
        language="en",
        source_url=url,
        content=content,
        content_hash=content_hash,
        fetched_at=now(),
    )
    update = {"source_url": url, "fetched_at": now()}
    if previous != content_hash:
        update.update(content=content, content_hash=content_hash)
    await db.execute(stmt.on_conflict_do_update(constraint="uq_researcher_section_language", set_=update))
    return previous != content_hash


async def collect_section(client, url, identity, *, detail=False):
    queue, visited, pages, details = [url], set(), [], set()
    while queue:
        current = queue.pop(0)
        key = canonical(current)
        if key in visited:
            continue
        if len(visited) >= 500:
            raise ImportFailure("Section pagination exceeded 500 pages")
        visited.add(key)
        page = await client.fetch(current)
        parsed = parse_page(page.body, page.url, identity["alias"], detail=detail, expected_id=identity["id"])
        page_hash = digest(parsed.content)
        if page_hash in {digest(p) for p in pages}:
            raise ImportFailure("Section pagination repeated page content")
        pages.append(parsed.content)
        queue.extend(p for p in parsed.pagination if p not in visited)
        details.update(parsed.details)
    content = combine(pages)
    content["detail_urls"] = sorted(details)
    return content


async def process_item(db, client, item):
    with bound_contextvars(job_id=str(item.run_id), researcher_id=item.source_id):
        logger.info("researcher_scrape_started", completed_sections=len(item.completed_sections))
        await _process_item(db, client, item)
        logger.info(
            "researcher_scrape_finished",
            status=item.status,
            record_outcome=item.outcome,
            completed_sections=len(item.completed_sections),
            error_count=len(item.errors),
        )


async def _process_item(db, client, item):
    ident = item.identity
    errors = []
    item.status = "running"
    item.errors = []
    await db.commit()
    try:
        page = await client.fetch(ident["source_url"])
        parsed = parse_page(page.body, page.url, ident["alias"], expected_id=ident["id"])
        existing = await db.get(Researcher, ident["id"])
        fields = {**parsed.fields, "alias": ident["alias"], "network_id": ident["network_id"], "source_url": page.url}
        if existing is None:
            item.outcome = "created"
        elif item.outcome != "created":
            item.outcome = item.outcome or "unchanged"
            if any(getattr(existing, k) != v for k, v in fields.items()):
                item.outcome = "updated"
        stmt = insert(Researcher).values(id=ident["id"], **fields, last_seen_at=now())
        await db.execute(
            stmt.on_conflict_do_update(index_elements=[Researcher.id], set_={**fields, "last_seen_at": now()})
        )
        if await save_section(db, ident["id"], "general", page.url, parsed.content) and item.outcome == "unchanged":
            item.outcome = "updated"
        await db.commit()
        logger.info("researcher_section_saved", section="general", record_outcome=item.outcome)
        sections = parsed.sections
        if not sections:
            raise ImportFailure("Profile navigation is missing; refusing a partial profile")
        tasks = list(sections.items())
        queued = set(sections)
        while tasks:
            key, url = tasks.pop(0)
            try:
                if key in item.completed_sections:
                    stored = await db.scalar(
                        select(ResearcherSection).where(
                            ResearcherSection.researcher_id == ident["id"],
                            ResearcherSection.section == key,
                            ResearcherSection.language == "en",
                        )
                    )
                    if stored is None:
                        raise ImportFailure("Checkpoint refers to a missing stored section")
                    content = stored.content
                    logger.info("researcher_section_skipped", section=key, reason="checkpoint_complete")
                else:
                    with bound_contextvars(section=key):
                        content = await collect_section(client, url, ident, detail=key.startswith("activity:"))
                    changed = await save_section(db, ident["id"], key, url, content)
                    if changed and item.outcome == "unchanged":
                        item.outcome = "updated"
                    item.completed_sections = [*item.completed_sections, key]
                    await db.commit()
                    logger.info("researcher_section_saved", section=key, changed=changed)
                for detail_url in content.get("detail_urls", []):
                    match = UUID.search(detail_url)
                    detail_key = "activity:" + (match[0].lower() if match else digest(canonical(detail_url)))
                    if detail_key not in queued:
                        queued.add(detail_key)
                        tasks.append((detail_key, detail_url))
            except StopImport as exc:
                logger.warning("researcher_section_failed", section=key, error_type=type(exc).__name__, error=str(exc))
                raise
            except ImportFailure as exc:
                logger.warning("researcher_section_failed", section=key, error_type=type(exc).__name__, error=str(exc))
                errors.append({"section": key, "error": str(exc)})
        item.status = "incomplete" if errors else "completed"
        item.errors = errors
        await db.commit()
    except ImportFailure as exc:
        logger.warning("researcher_scrape_failed", error_type=type(exc).__name__, error=str(exc))
        item.status = "incomplete"
        item.errors = [*errors, {"section": "profile", "error": str(exc)}]
        await db.commit()
        if isinstance(exc, StopImport):
            raise


async def report(db, run_id):
    run = await db.get(ResearcherImportRun, run_id)
    if run is None:
        raise ImportFailure("Import run does not exist")
    rows = list((await db.scalars(select(ResearcherImportItem).where(ResearcherImportItem.run_id == run_id))).all())
    warning_rows = (
        await db.execute(
            select(ResearcherSection.researcher_id, ResearcherSection.section, ResearcherSection.content["warnings"])
            .join(ResearcherImportItem, ResearcherImportItem.source_id == ResearcherSection.researcher_id)
            .where(ResearcherImportItem.run_id == run_id, ResearcherSection.language == "en")
        )
    ).all()
    warnings = [
        {"id": source_id, "section": section, "warnings": warnings}
        for source_id, section, warnings in warning_rows
        if warnings
    ]
    return {
        "warnings": warnings,
        "run_id": str(run.id),
        "status": run.status,
        "options": run.options,
        "discovery": run.discovery,
        "selected": len(rows),
        "counts": dict(Counter(row.status for row in rows)),
        "outcomes": dict(Counter(row.outcome for row in rows if row.outcome)),
        "errors": [{"id": row.source_id, "errors": row.errors} for row in rows if row.errors],
    }


async def synchronize(
    engine, client, *, limit=None, researcher_id=None, resume=None, progress=print, queued_only=False
):
    async with engine.connect() as lock:
        if not await lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_ID}):
            raise ImportFailure("Another AVESIS import is already running")
        await lock.commit()
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                if resume:
                    run = await db.get(ResearcherImportRun, resume)
                    if run is None:
                        raise ImportFailure("Import run does not exist")
                    if queued_only and run.status != "queued":
                        return await report(db, run.id)
                    if run.status == "completed":
                        return await report(db, run.id)
                    limit, researcher_id = run.options.get("limit"), run.options.get("researcher_id")
                    run.status, run.finished_at = "running", None
                else:
                    run = ResearcherImportRun(
                        options={"language": "en", "limit": limit, "researcher_id": researcher_id}
                    )
                    db.add(run)
                run.options = {**run.options, "lock_pid": await lock.scalar(text("SELECT pg_backend_pid()"))}
                run.discovery = {key: value for key, value in (run.discovery or {}).items() if key != "last_error"}
                await db.commit()
                run_id = run.id
                telemetry = _run_telemetry(run)
                actor_user_id = telemetry["actor_user_id"]
                organization_id = telemetry["organization_id"]
                request_id = telemetry["request_id"]
                with observed_job(
                    "researcher_import",
                    job_id=str(run_id),
                    distinct_id=actor_user_id or "avesis-importer",
                    request_id=request_id,
                    source_id="avesis",
                    resumed=bool(resume),
                    language="en",
                    actor_user_id=actor_user_id,
                    organization_id=organization_id,
                ) as observation:
                    capture(
                        "researcher_import_started",
                        distinct_id=actor_user_id or "avesis-importer",
                        job_id=str(run_id),
                        resumed=bool(resume),
                        language="en",
                        actor_user_id=actor_user_id,
                        organization_id=organization_id,
                        request_id=request_id,
                    )
                    progress(f"Import run: {run_id}")
                    try:
                        if not run.discovery.get("complete"):
                            identities, discovery = await discover(client)
                            selected = identities
                            if researcher_id is not None:
                                selected = [item for item in identities if item["id"] == researcher_id]
                                if not selected:
                                    raise ImportFailure("Researcher ID is not in the public directory")
                            if limit:
                                selected = selected[:limit]
                            run.discovery = discovery
                            for ident in selected:
                                db.add(ResearcherImportItem(run_id=run_id, source_id=ident["id"], identity=ident))
                            await db.commit()
                        items = list(
                            (
                                await db.scalars(
                                    select(ResearcherImportItem)
                                    .where(
                                        ResearcherImportItem.run_id == run_id,
                                        ResearcherImportItem.status != "completed",
                                    )
                                    .order_by(ResearcherImportItem.source_id)
                                )
                            ).all()
                        )
                        for position, item in enumerate(items, 1):
                            source_id = item.source_id
                            await process_item(db, client, item)
                            progress(f"{position}/{len(items)} researcher {source_id}: {item.status} ({item.outcome})")
                        summary = await report(db, run_id)
                        unresolved = summary["counts"].get("incomplete", 0) or run.discovery.get("sitemap_only")
                        run.status = "incomplete" if unresolved else "completed"
                        run.finished_at = now()
                        await db.commit()
                    except ImportFailure as exc:
                        logger.warning("researcher_import_interrupted", error_type=type(exc).__name__, error=str(exc))
                        observation.expected_failure("source_unavailable", status="interrupted")
                        await db.rollback()
                        run = await db.get(ResearcherImportRun, run_id)
                        run.status, run.finished_at = "interrupted", now()
                        run.discovery = {**run.discovery, "last_error": str(exc)}
                        await db.commit()
                    except (Exception, asyncio.CancelledError) as exc:
                        logger.warning("researcher_import_stopped", error_type=type(exc).__name__)
                        if isinstance(exc, asyncio.CancelledError):
                            observation.cancelled("worker_shutdown", status="interrupted")
                        else:
                            # Raw transport/database exceptions can contain credentials or scraped content.
                            observation.failed(
                                RuntimeError(f"AVESIS import failed ({type(exc).__name__})"),
                                status="interrupted",
                                failure_type=type(exc).__name__,
                            )
                        await db.rollback()
                        run = await db.get(ResearcherImportRun, run_id)
                        run.status, run.finished_at = "interrupted", now()
                        await db.commit()
                        raise
                    result = await report(db, run_id)
                    observation.detail(
                        status=result["status"],
                        selected=result["selected"],
                        completed=result["counts"].get("completed", 0),
                        incomplete=result["counts"].get("incomplete", 0),
                        pending=result["counts"].get("pending", 0),
                        created=result["outcomes"].get("created", 0),
                        updated=result["outcomes"].get("updated", 0),
                        unchanged=result["outcomes"].get("unchanged", 0),
                    )
                    if result["status"] == "incomplete":
                        observation.expected_failure("incomplete_sections_or_discovery")
                    logger.info("researcher_import_finished", **observation.details)
                    return result
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})
            await lock.commit()

"""Consume only explicitly queued admin jobs; interrupted runs need Resume."""

import asyncio

from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal, engine
from app.logging import get_logger
from app.researchers.client import AvesisClient, ImportFailure
from app.researchers.models import ResearcherImportRun
from app.researchers.service import synchronize

logger = get_logger(__name__)


async def run_next():
    async with SessionLocal() as db:
        run_id = await db.scalar(
            select(ResearcherImportRun.id)
            .where(ResearcherImportRun.status == "queued")
            .order_by(ResearcherImportRun.started_at)
            .limit(1)
        )
    if run_id is None:
        return
    secret = get_settings().avesis_proxy_url
    async with AvesisClient(secret.get_secret_value() if secret else None) as client:
        await synchronize(
            engine,
            client,
            resume=run_id,
            queued_only=True,
            progress=lambda message: logger.info("researcher_import_progress", progress=message),
        )


async def run_admin_import_loop(stop_event):
    while not stop_event.is_set():
        try:
            await run_next()
        except ImportFailure:
            # A CLI process or another API worker can own the same execution lock.
            pass
        except Exception as exc:
            logger.warning("researcher_import_worker_failed", error_type=type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5)
        except TimeoutError:
            pass

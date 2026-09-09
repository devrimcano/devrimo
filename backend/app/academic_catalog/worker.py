"""Resumable catalog imports. Only admin publication changes student-visible data."""

from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import DateTime, case, cast, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.academic_catalog.models import (
    CatalogCourse, CatalogDraft, CatalogHttpBudget, CatalogImportJob,
    CatalogTerm, CatalogTermActiveRelease,
)
from app.academic_catalog import service as catalog_service
from app.academic_catalog.source import CatalogSource
from app.campus import service as campus_service
from app.campus.credentials import secrets_for
from app.campus.warmer import ISTANBUL, _wanted_courses, _within_hours
from app.config import get_settings
from app.db.models import AccountDirectory, AccountStatus
from app.db.session import SessionLocal, engine
from app.logging import get_logger

logger = get_logger(__name__)


class LeaseLost(Exception):
    """A newer worker owns this job; an older worker must stop writing."""


class ImportDeferred(Exception):
    """A policy limit, not a failed source read."""


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def admit_request(organization_id: UUID, job_id: UUID, lease_token: UUID | None = None) -> None:
    """Serialize and reserve every HTTP attempt, including redirects and sign-in."""
    settings = get_settings()
    while True:
        local = datetime.now(ISTANBUL)
        if not _within_hours(local, settings.catalog_warm_hours):
            raise ImportDeferred("outside_import_hours")
        now = datetime.now(UTC)
        async with SessionLocal() as db:
            if lease_token is not None:
                await _owned_job(db, job_id, lease_token)
            await db.execute(insert(CatalogHttpBudget).values(
                organization_id=organization_id, budget_date=local.date(), attempted_count=0,
                next_request_at=now, daily_limit=settings.catalog_warm_daily_limit,
                min_interval_seconds=settings.catalog_warm_interval_seconds,
            ).on_conflict_do_nothing())
            row = await db.scalar(select(CatalogHttpBudget).where(
                CatalogHttpBudget.organization_id == organization_id,
                CatalogHttpBudget.budget_date == local.date(),
            ).with_for_update())
            if row.attempted_count >= settings.catalog_warm_daily_limit:
                raise ImportDeferred("daily_http_budget_exhausted")
            wait = max(0, (_aware(row.next_request_at) - now).total_seconds()) if row.next_request_at else 0
            if wait <= 0:
                row.attempted_count += 1
                row.daily_limit = settings.catalog_warm_daily_limit
                row.min_interval_seconds = settings.catalog_warm_interval_seconds
                row.next_request_at = now + timedelta(seconds=(
                    settings.catalog_warm_interval_seconds + random.uniform(0, settings.catalog_warm_jitter_seconds)
                ))
                await db.execute(update(CatalogImportJob).where(CatalogImportJob.id == job_id).values(
                    lease_until=now + timedelta(seconds=settings.academic_catalog_job_lease_seconds),
                ))
                await db.commit()
                return
            await db.commit()
        await asyncio.sleep(min(wait, 30))


async def _claim():
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        jobs = (await db.scalars(select(CatalogImportJob).where(or_(
            CatalogImportJob.status == "queued",
            (CatalogImportJob.status == "running") & (CatalogImportJob.lease_until < now),
        ), or_(
            CatalogImportJob.checkpoint["retry_at"].as_string().is_(None),
            cast(CatalogImportJob.checkpoint["retry_at"].as_string(), DateTime(timezone=True)) <= now,
        )).order_by(
            case((CatalogImportJob.payload["scheduled"].as_boolean().is_(True), 1), else_=0),
            CatalogImportJob.created_at,
        ).with_for_update(skip_locked=True).limit(1))).all()
        job = None
        for candidate in jobs:
            retry_at = (candidate.checkpoint or {}).get("retry_at")
            if retry_at and _aware(datetime.fromisoformat(retry_at)) > now:
                continue
            job = candidate
            break
        if job is None:
            return None
        job.lease_token = uuid4()
        job.status = "running"
        job.started_at = job.started_at or now
        job.lease_until = now + timedelta(seconds=get_settings().academic_catalog_job_lease_seconds)
        await db.commit()
        return job


async def _account(organization_id: UUID):
    async with SessionLocal() as db:
        candidates = await campus_service.users_with_tool(db, "course_info")
        permitted = set((await db.scalars(select(AccountDirectory.user_id).where(
            AccountDirectory.organization_id == organization_id,
            AccountDirectory.status == AccountStatus.active,
            AccountDirectory.user_id.in_(candidates),
        ))).all())
        ordered = sorted(permitted, key=str)
        if not ordered:
            return None
        start = datetime.now(ISTANBUL).toordinal() % len(ordered)
        for user_id in ordered[start:] + ordered[:start]:
            secret = secrets_for(await campus_service.get_credential(db, user_id))
            if secret and secret.has("metu_password"):
                return user_id, secret
    return None


def initial_steps(job) -> list[dict]:
    term = job.term
    codes = job.course_codes or (job.payload or {}).get("course_codes") or []
    if codes:
        steps = []
        for code in dict.fromkeys(codes):
            if not str(code).isdigit() or len(str(code)) != 7:
                raise ValueError("Import requires full seven-digit course codes")
            for tool in ("get_course_info", "get_course_prerequisites", "get_course_replacements"):
                steps.append({"tool": tool, "values": {"semester": term, "department": code[:3], "course": code}})
        return steps
    if job.department:
        return [{"tool": tool, "values": {"semester": term, "department": job.department}}
                for tool in ("list_program_courses", "get_thesis_courses")]
    return [{"tool": "get_departments_and_semesters", "values": {}}]


def _rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("result", "value", "courses"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def expand_steps(step: dict, payload, term: str) -> list[dict]:
    tool, values = step["tool"], step["values"]
    if tool == "get_departments_and_semesters":
        semesters = payload.get("semesters", []) if isinstance(payload, dict) else []
        if not any(str(row.get("code")) == term for row in semesters):
            raise ValueError("Requested term is absent from the official term directory")
        return [{"tool": tool, "values": {"semester": term, "department": str(row["code"])}}
                for row in payload.get("departments", []) if row.get("code")
                for tool in ("list_program_courses", "get_thesis_courses")]
    if tool in {"list_program_courses", "get_thesis_courses"}:
        steps = []
        for row in _rows(payload):
            code = str(row.get("course_code") or "")
            if len(code) == 7 and code.isdigit():
                for name in ("get_course_info", "get_course_prerequisites", "get_course_replacements"):
                    steps.append({"tool": name, "values": {**values, "department": code[:3], "course": code}})
        return steps
    if tool == "get_course_info":
        return [{"tool": "get_section_constraints", "values": {**values, "section": str(row["section"])}}
                for row in payload.get("sections", []) if row.get("section") is not None]
    return []


async def _owned_job(db, job_id, lease_token):
    job = await db.scalar(select(CatalogImportJob).where(
        CatalogImportJob.id == job_id,
        CatalogImportJob.lease_token == lease_token,
        CatalogImportJob.status == "running",
    ).with_for_update())
    if job is None:
        raise LeaseLost("import_lease_replaced")
    return job


async def _save(job_id, lease_token, **values):
    async with SessionLocal() as db:
        job = await _owned_job(db, job_id, lease_token)
        for name, value in values.items():
            setattr(job, name, value)
        await db.commit()


async def schedule_due() -> int:
    """Schedule known-term discovery and missing components without starvation.

    A term must already have been discovered or explicitly selected by an admin.
    Daily per-course jobs cover details AND prerequisites/restrictions, so a warm
    detail response cannot hide missing eligibility components indefinitely.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    made = 0
    async with SessionLocal() as db:
        terms = (await db.scalars(select(CatalogTerm).outerjoin(
            CatalogTermActiveRelease, CatalogTermActiveRelease.term_id == CatalogTerm.id,
        ).where(or_(CatalogTerm.is_current.is_(True), CatalogTermActiveRelease.release_id.is_not(None))))).all()
        for term in terms:
            demand = await _wanted_courses(term.term_code)
            courses = (await db.scalars(select(CatalogCourse).join(
                CatalogDraft, CatalogDraft.course_id == CatalogCourse.id,
            ).where(
                CatalogCourse.organization_id == term.organization_id,
                CatalogDraft.term_id == term.id,
            ).distinct().order_by(CatalogCourse.course_code))).all()
            # Missing/oldest observations eventually get a turn after high demand.
            candidates = sorted(courses, key=lambda row: (-demand.get(row.course_code, 0), row.course_code))
            for course in candidates:
                last_job = await db.scalar(select(CatalogImportJob).where(
                    CatalogImportJob.organization_id == term.organization_id,
                    CatalogImportJob.term == term.term_code,
                    CatalogImportJob.course_codes.contains([course.course_code]),
                ).order_by(CatalogImportJob.created_at.desc()).limit(1))
                if last_job is not None and (
                    last_job.status in {"queued", "running"}
                    or (now - _aware(last_job.updated_at)).total_seconds() < 86400
                ):
                    continue
                await catalog_service.enqueue_import(db, term.organization_id, term.term_code, course_codes=[course.course_code],
                                     reason="Scheduled catalog freshness check", payload={"scheduled": True})
                made += 1
                if made >= settings.catalog_warm_courses_per_pass:
                    break
            directory_job = await db.scalar(select(CatalogImportJob).where(
                CatalogImportJob.organization_id == term.organization_id,
                CatalogImportJob.term == term.term_code,
                CatalogImportJob.payload["discovery_only"].as_boolean().is_(True),
                CatalogImportJob.created_at > now - timedelta(days=30),
            ).limit(1))
            if directory_job is None:
                await catalog_service.enqueue_import(db, term.organization_id, term.term_code, reason="Scheduled catalog discovery",
                                     payload={"scheduled": True, "discovery_only": True})
                made += 1
        await db.commit()
    return made


async def run_once() -> int:
    settings = get_settings()
    if not settings.academic_catalog_ingestion_enabled or not settings.catalog_warm_enabled:
        return 0
    if not _within_hours(datetime.now(ISTANBUL), settings.catalog_warm_hours):
        return 0
    await schedule_due()
    job = await _claim()
    if job is None:
        return 0
    selected = await _account(job.organization_id)
    if selected is None:
        await _save(job.id, job.lease_token, status="queued", lease_until=None, error_code="no_eligible_source_account")
        return 0
    user_id, secret = selected
    checkpoint = dict(job.checkpoint or {})
    try:
        steps = checkpoint.get("steps") or initial_steps(job)
    except ValueError:
        await _save(job.id, job.lease_token, status="failed", lease_until=None, error_code="invalid_import_scope")
        return 0
    offset = int(checkpoint.get("offset", 0))
    fetched = 0
    # Dedicated connection: session advisory lock never returns to the pool held.
    lock_key = int.from_bytes(hashlib.sha256(f"catalog-source:{user_id}".encode()).digest()[:8], "big", signed=True)
    async with engine.connect() as lock:
        acquired = await lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
        if not acquired:
            await _save(job.id, job.lease_token, status="queued", lease_until=None)
            return 0
        source = None
        try:
            source = CatalogSource(secret, lambda: admit_request(job.organization_id, job.id, job.lease_token))
            while offset < len(steps) and fetched < settings.catalog_warm_batch:
                step = steps[offset]
                payload = await source.read(step["tool"], step["values"])
                observed = datetime.now(UTC)
                async with SessionLocal() as db:
                    await _owned_job(db, job.id, job.lease_token)
                    await catalog_service.ingest_observation(db, job.organization_id, step["tool"], step["values"], payload,
                                             observed, source_fetched_at=observed, job_id=job.id)
                    await db.commit()
                added = expand_steps(step, payload, job.term)
                if (job.payload or {}).get("discovery_only") and step["tool"] in {"list_program_courses", "get_thesis_courses"}:
                    added = []
                # Restrictions for this course precede the next expensive detail fetch.
                if step["tool"] == "get_course_info":
                    steps[offset + 1:offset + 1] = added
                else:
                    existing = {str((s["tool"], sorted(s["values"].items()))) for s in steps}
                    steps.extend(s for s in added if str((s["tool"], sorted(s["values"].items()))) not in existing)
                offset += 1
                fetched += 1
                checkpoint = {"steps": steps, "offset": offset}
                await _save(job.id, job.lease_token, checkpoint=checkpoint, error_code=None, attempts=0)
            done = offset >= len(steps)
            await _save(job.id, job.lease_token, status="completed" if done else "queued", checkpoint=checkpoint,
                        lease_until=None, completed_at=datetime.now(UTC) if done else None)
        except LeaseLost:
            logger.info("catalog_import_lease_replaced", job_id=str(job.id))
        except ImportDeferred as exc:
            checkpoint = {"steps": steps, "offset": offset}
            await _save(job.id, job.lease_token, status="queued", checkpoint=checkpoint, lease_until=None, error_code=str(exc))
        except Exception as exc:
            # Retain failed evidence without provider error text (which can
            # contain authentication details). Prior verified values stay intact.
            if offset < len(steps):
                try:
                    async with SessionLocal() as db:
                        await _owned_job(db, job.id, job.lease_token)
                        await catalog_service.ingest_observation(db, job.organization_id, steps[offset]["tool"],
                                                 steps[offset]["values"], None, datetime.now(UTC),
                                                 source_fetched_at=None, job_id=job.id)
                        await db.commit()
                except Exception:
                    logger.warning("catalog_failure_observation_unavailable", job_id=str(job.id))
            attempts = int(job.attempts or 0) + 1
            auth = type(exc).__name__ == "SAISAuthError"
            checkpoint = {"steps": steps, "offset": offset,
                          "retry_at": (datetime.now(UTC) + timedelta(seconds=min(3600, 60 * 2 ** attempts))).isoformat()}
            await _save(job.id, job.lease_token, status="failed" if auth or attempts >= settings.academic_catalog_max_attempts else "queued",
                        attempts=attempts, checkpoint=checkpoint, lease_until=None,
                        error_code="source_authentication_failed" if auth else type(exc).__name__)
            logger.warning("catalog_import_failed", job_id=str(job.id), error_type=type(exc).__name__)
        finally:
            try:
                if source is not None:
                    await source.aclose()
            finally:
                await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
    return fetched

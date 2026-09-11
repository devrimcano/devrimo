"""Resumable catalog imports. Only admin publication changes student-visible data."""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
import traceback
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import ARRAY, DateTime, Text, case, cast, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.academic_catalog import service as catalog_service
from app.academic_catalog.models import (
    CatalogCourse,
    CatalogDraft,
    CatalogHttpBudget,
    CatalogImportJob,
    CatalogTerm,
    CatalogTermActiveRelease,
)
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


class CatalogPassResult(int):
    """Numeric worker count with a safe, machine-readable pass outcome.

    The standalone worker historically returned an integer and the runtime
    uses that count for aggregate pass telemetry.  Keep the integer shape for
    callers while carrying the distinction between an idle pass, a deferred
    job, and a completed or failed job.  The runtime can inspect these bounded
    fields without requiring source payloads or exception text.
    """

    def __new__(
        cls,
        count: int = 0,
        *,
        outcome: str = "idle",
        job_id: UUID | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        scheduled_count: int = 0,
    ):
        value = super().__new__(cls, count)
        value.outcome = outcome
        value.job_id = str(job_id) if job_id is not None else None
        value.error_code = error_code
        value.error_detail = error_detail
        value.scheduled_count = scheduled_count
        return value


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _safe_error_detail(message: str | None) -> str | None:
    if not message:
        return None
    value = str(message).strip()
    if not value:
        return None
    return value[:512]


def _failure_detail(exc: BaseException, step: dict | None) -> str | None:
    """Something a person can act on, for an exception that carries no message.

    Every catalog import on production has failed with error_code "ValueError"
    and error_detail null, which is what an exception raised with no arguments
    leaves behind: the admin panel shows a job that failed and no reason, and
    the reviewed catalog stays empty with nothing to read about why. A class
    name and the step being fetched is not a diagnosis, but it is a place to
    look, and it costs nothing when the message is there.
    """
    detail = _safe_error_detail(str(exc))
    if detail:
        return detail
    where = ""
    if step:
        values = step.get("values") or {}
        named = ", ".join(f"{key}={values[key]}" for key in sorted(values) if values[key] is not None)
        where = f" while fetching {step.get('tool')}" + (f" ({named})" if named else "")
    return _safe_error_detail(f"{type(exc).__name__} was raised with no message{where}")


async def admit_request(organization_id: UUID, job_id: UUID | None, lease_token: UUID | None = None) -> None:
    """Fence catalog requests; retain budget admission only for the legacy warmer."""
    settings = get_settings()
    if job_id is not None:
        async with SessionLocal() as db:
            job = await _owned_job(db, job_id, lease_token)
            if job.organization_id != organization_id:
                raise LeaseLost("import_organization_mismatch")
            job.lease_until = datetime.now(UTC) + timedelta(seconds=settings.academic_catalog_job_lease_seconds)
            await db.commit()
        return
    while True:
        local = datetime.now(ISTANBUL)
        if not _within_hours(local, settings.catalog_warm_hours):
            raise ImportDeferred("outside_import_hours")
        now = datetime.now(UTC)
        async with SessionLocal() as db:
            if lease_token is not None:
                await _owned_job(db, job_id, lease_token)
            inserted = await db.scalar(insert(CatalogHttpBudget).values(
                organization_id=organization_id, budget_date=local.date(), attempted_count=0,
                next_request_at=now, daily_limit=settings.catalog_warm_daily_limit,
                min_interval_seconds=settings.catalog_warm_interval_seconds,
            ).on_conflict_do_nothing().returning(CatalogHttpBudget.budget_date))
            row = await db.scalar(select(CatalogHttpBudget).where(
                CatalogHttpBudget.organization_id == organization_id,
                CatalogHttpBudget.budget_date == local.date(),
            ).with_for_update())
            if row is None:
                # The insert and the select are in one transaction. A row can
                # only be absent if the database rejected the insert without
                # reporting a conflict, which is a real admission failure and
                # must never be treated as permission to make a request.
                raise RuntimeError("catalog HTTP budget row could not be created")
            if inserted is not None:
                # The counter resets at local midnight, but the request clock
                # must not. Carry the previous day's reservation into a newly
                # created row so an overnight window cannot issue two requests
                # back-to-back at the date boundary.
                previous = await db.scalar(select(CatalogHttpBudget).where(
                    CatalogHttpBudget.organization_id == organization_id,
                    CatalogHttpBudget.budget_date < local.date(),
                ).order_by(CatalogHttpBudget.budget_date.desc()).limit(1).with_for_update())
                if previous is not None and previous.next_request_at is not None:
                    previous_next = _aware(previous.next_request_at)
                    current_next = _aware(row.next_request_at) if row.next_request_at else None
                    if current_next is None or previous_next > current_next:
                        row.next_request_at = previous_next
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
                if job_id is not None:
                    await db.execute(update(CatalogImportJob).where(CatalogImportJob.id == job_id).values(
                        lease_until=now + timedelta(seconds=settings.academic_catalog_job_lease_seconds),
                    ))
                await db.commit()
                return
            await db.commit()
        await asyncio.sleep(min(wait, 30))


class LeaseKeeper:
    """Keeps a running job's lease alive without a transaction per request.

    The gate this replaces is an httpx request hook: it fires once for every
    HTTP request the source client makes, redirects included. It opened a
    session, took a row lock on the job, moved `lease_until` and committed -
    four round trips to a database in another country, four or five times per
    course, for a timestamp with fifteen minutes of headroom on it. Against the
    10,048-step whole-term plan that is close to two of its eight hours.

    So the touch is rate-limited in memory. The fence it also provided is
    unchanged in strength: every write a pass makes still goes through
    _owned_job, so a worker whose lease was taken over still cannot write. It
    only learns about it later, and the writes it would have made are the ones
    already fenced.
    """

    def __init__(self, organization_id: UUID, job_id: UUID | None, lease_token: UUID | None):
        self.organization_id = organization_id
        self.job_id = job_id
        self.lease_token = lease_token
        self._next_touch = 0.0

    async def __call__(self) -> None:
        if self.job_id is None:
            # The legacy warmer's gate, which really does have to run per
            # request: it is a daily HTTP budget, not a lease.
            await admit_request(self.organization_id, None, self.lease_token)
            return
        now = time.monotonic()
        if now < self._next_touch:
            return
        # Claim the window before awaiting, so a burst of concurrent requests
        # cannot all decide to touch the same row at the same moment.
        self._next_touch = now + max(1.0, get_settings().catalog_import_lease_touch_seconds)
        await admit_request(self.organization_id, self.job_id, self.lease_token)


class SourceFleet:
    """One or more independent clients on the same account.

    A client is a portal session, and a portal session serves one page at a
    time: SAIS keeps a single app-proxy session per cookie jar, so two
    overlapping calls on one client would read each other's pages. That is what
    the client's own lock is for, and it is why fetching two courses at once
    needs two clients rather than two calls.

    A fleet of one is exactly the previous behaviour, one page after another.
    """

    def __init__(self, sources):
        self.sources = list(sources)
        self._idle: asyncio.Queue = asyncio.Queue()
        for source in self.sources:
            self._idle.put_nowait(source)

    @property
    def size(self) -> int:
        return len(self.sources)

    async def _read(self, step: dict):
        source = await self._idle.get()
        try:
            return step, await source.read(step["tool"], step["values"]), None
        except Exception as exc:
            # Returned rather than raised, so one unreadable page cannot
            # discard the pages fetched alongside it. The caller decides which
            # failures belong to a page and which end the pass.
            return step, None, exc
        finally:
            self._idle.put_nowait(source)

    async def read_all(self, window: list[dict]):
        """Every step in the window, in plan order, each with its outcome."""
        if len(window) == 1:
            return [await self._read(window[0])]
        return list(await asyncio.gather(*(self._read(step) for step in window)))

    async def aclose(self) -> None:
        for source in self.sources:
            try:
                await source.aclose()
            except Exception:
                logger.warning("catalog_source_close_failed")


def _step_key(step: dict) -> str:
    return str((step["tool"], sorted(step["values"].items())))


async def _claim(*, manual_only: bool = False):
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        eligibility = []
        if manual_only:
            eligibility = [CatalogImportJob.requested_by.is_not(None),
                           CatalogImportJob.payload["scheduled"].as_boolean().is_not(True)]
        jobs = (await db.scalars(select(CatalogImportJob).where(*eligibility, or_(
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
    if tool == "get_thesis_courses":
        # Thesis courses are recorded from their listing alone.  The listing
        # observation ingests each row with its identity and ``is_thesis`` flag;
        # expanding it into detail and rule reads for every thesis number is the
        # large request cost this plan deliberately avoids.
        return []
    if tool == "list_program_courses":
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
        checkpoint = values.get("checkpoint")
        if "checkpoint_offset" not in values and isinstance(checkpoint, dict):
            values["checkpoint_offset"] = int(checkpoint.get("offset", 0))
        for name, value in values.items():
            setattr(job, name, value)
        await db.commit()


async def _save_offset(
    job_id,
    lease_token,
    offset: int,
    *,
    retry_at: str | None = None,
    clear_retry_at: bool = False,
    **values,
):
    """Persist progress without rewriting a growing operation plan.

    The step plan is durable whenever it changes. Between expansions only the
    scalar cursor and job fields change, so update the dedicated cursor column.
    This keeps a whole-school import's ordinary write size bounded rather than
    rewriting the already-known JSONB operation plan after each request. The
    plan is still rewritten when a source response expands it.
    """
    async with SessionLocal() as db:
        await _owned_job(db, job_id, lease_token)
        update_values = {"checkpoint_offset": offset, **values}
        if retry_at is not None:
            checkpoint = func.jsonb_set(
                CatalogImportJob.checkpoint,
                cast(["offset"], ARRAY(Text)),
                func.to_jsonb(offset),
                True,
            )
            update_values["checkpoint"] = func.jsonb_set(
                checkpoint,
                cast(["retry_at"], ARRAY(Text)),
                func.to_jsonb(retry_at),
                True,
            )
        elif clear_retry_at:
            update_values["checkpoint"] = CatalogImportJob.checkpoint.op("-")("retry_at")
        await db.execute(
            update(CatalogImportJob)
            .where(
                CatalogImportJob.id == job_id,
                CatalogImportJob.lease_token == lease_token,
                CatalogImportJob.status == "running",
            )
            .values(**update_values)
        )
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
    course_jobs = 0
    discovery_jobs = 0
    async with SessionLocal() as db:
        terms = (await db.scalars(select(CatalogTerm).outerjoin(
            CatalogTermActiveRelease, CatalogTermActiveRelease.term_id == CatalogTerm.id,
        ).where(or_(CatalogTerm.is_current.is_(True), CatalogTermActiveRelease.release_id.is_not(None))))).all()
        for term in terms:
            demand = await _wanted_courses(term.term_code)
            if course_jobs < settings.catalog_warm_courses_per_pass:
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
                    await catalog_service.enqueue_import(
                        db,
                        term.organization_id,
                        term.term_code,
                        course_codes=[course.course_code],
                        reason="Scheduled catalog freshness check",
                        payload={"scheduled": True},
                    )
                    course_jobs += 1
                    made += 1
                    if course_jobs >= settings.catalog_warm_courses_per_pass:
                        break
            directory_job = await db.scalar(select(CatalogImportJob).where(
                CatalogImportJob.organization_id == term.organization_id,
                CatalogImportJob.term == term.term_code,
                CatalogImportJob.payload["discovery_only"].as_boolean().is_(True),
                CatalogImportJob.created_at > now - timedelta(days=30),
            ).limit(1))
            if directory_job is None and discovery_jobs < settings.catalog_warm_discoveries_per_pass:
                await catalog_service.enqueue_import(db, term.organization_id, term.term_code, reason="Scheduled catalog discovery",
                                     payload={"scheduled": True, "discovery_only": True})
                discovery_jobs += 1
                made += 1
        await db.commit()
    logger.info(
        "catalog_schedule_due",
        jobs=made,
        course_jobs=course_jobs,
        discovery_jobs=discovery_jobs,
    )
    return made


async def run_once() -> CatalogPassResult:
    settings = get_settings()
    if not settings.academic_catalog_ingestion_enabled:
        return CatalogPassResult(0, outcome="idle", error_code="ingestion_disabled")
    background_allowed = settings.catalog_warm_enabled and _within_hours(datetime.now(ISTANBUL), settings.catalog_warm_hours)
    scheduled_count = await schedule_due() if background_allowed else 0
    job = await _claim(manual_only=not background_allowed)
    if job is None:
        return CatalogPassResult(0, outcome="idle", scheduled_count=scheduled_count)
    selected = await _account(job.organization_id)
    if selected is None:
        await _save(
            job.id,
            job.lease_token,
            status="queued",
            lease_until=None,
            error_code="no_eligible_source_account",
            error_detail=None,
        )
        return CatalogPassResult(
            0,
            outcome="deferred",
            job_id=job.id,
            error_code="no_eligible_source_account",
            error_detail="No eligible source account was available.",
            scheduled_count=scheduled_count,
        )
    user_id, secret = selected
    checkpoint = dict(job.checkpoint or {})
    try:
        stored_steps = checkpoint.get("steps")
        plan_persisted = isinstance(stored_steps, list) and bool(stored_steps)
        steps = stored_steps if plan_persisted else initial_steps(job)
    except ValueError:
        await _save(
            job.id,
            job.lease_token,
            status="failed",
            lease_until=None,
            error_code="invalid_import_scope",
            error_detail=None,
        )
        return CatalogPassResult(
            0,
            outcome="failed",
            job_id=job.id,
            error_code="invalid_import_scope",
            scheduled_count=scheduled_count,
        )
    force_refresh = bool((job.payload or {}).get("force_refresh"))
    if not plan_persisted and steps and not force_refresh:
        # An unforced job may already be satisfied by pages read recently.
        # Expansion steps stay so the plan can still grow; a leaf whose
        # observation is inside its reuse window is not fetched again.
        async with SessionLocal() as db:
            steps = await catalog_service.drop_fresh_leaf_steps(
                db, job.organization_id, job.term, steps, force=False,
            )
    # 0034 moves the hot cursor out of the growing JSONB plan. The fallback
    # keeps jobs written before that migration resumable and also tolerates
    # hand-created legacy fixtures that only have the JSONB cursor.
    # Migration 0034 backfills every row, including zero.  A scalar zero is
    # therefore a real cursor and must not fall back to a stale JSON offset.
    # The getattr fallback keeps this worker compatible with lightweight test
    # doubles and pre-migration objects that do not expose the new attribute.
    checkpoint_offset = getattr(job, "checkpoint_offset", None)
    offset = int(checkpoint_offset if checkpoint_offset is not None else checkpoint.get("offset", 0))
    retry_pending = bool(checkpoint.get("retry_at"))
    fetched = 0
    # A job created before the first operation has no durable plan yet. Save it
    # once so offset-only updates remain resumable even when the first response
    # does not expand the plan.
    if not plan_persisted:
        await _save(
            job.id,
            job.lease_token,
            checkpoint={"steps": steps, "offset": offset},
            error_code=None,
            error_detail=None,
            attempts=0,
        )
        retry_pending = False
    # Dedicated connection: session advisory lock never returns to the pool held.
    lock_key = int.from_bytes(hashlib.sha256(f"catalog-source:{user_id}".encode()).digest()[:8], "big", signed=True)
    async with engine.connect() as lock:
        acquired = await lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
        if not acquired:
            await _save(
                job.id,
                job.lease_token,
                status="queued",
                lease_until=None,
                error_code="source_account_busy",
                error_detail=None,
            )
            return CatalogPassResult(
                0,
                outcome="deferred",
                job_id=job.id,
                error_code="source_account_busy",
                error_detail="Source account lock is busy; retrying later.",
                scheduled_count=scheduled_count,
            )
        source = None
        try:
            # One keeper for the whole pass: `lease_until` moves on a timer
            # rather than once per HTTP request. See LeaseKeeper.
            keeper = LeaseKeeper(job.organization_id, job.id, job.lease_token)
            concurrency = max(1, int(settings.catalog_import_concurrency))
            source = SourceFleet(CatalogSource(secret, keeper) for _ in range(concurrency))
            # Skipping one department is not the same as a source answering
            # nothing at all. These two tell those apart.
            succeeded = 0
            # Where this pass began, which is how "the source has never
            # answered" is told apart from "this batch happened to be
            # unreadable". See the guard below.
            started_offset = offset
            listing_refusal: ValueError | None = None
            listing_refusal_step: dict | None = None
            # Plan expansions that have not been written yet. The cursor is only
            # ever persisted together with the plan the steps behind it grew, so
            # an interrupted pass re-reads a few pages rather than skipping the
            # courses those pages discovered. See _checkpoint below.
            plan_pending = False
            since_checkpoint = 0
            checkpoint_every = max(1, int(settings.catalog_import_checkpoint_steps))
            # A pass may not outlive its own lease. The batch is a step count
            # and a step's cost depends on METU, so the wall clock is what
            # actually bounds this.
            pass_deadline = time.monotonic() + max(
                30.0,
                settings.academic_catalog_job_lease_seconds * settings.catalog_import_pass_lease_fraction,
            )

            async def _checkpoint(**values):
                """Write the cursor, and the operation plan when it has grown.

                One transaction, not two, and the 1.1 MB whole-term plan is
                written when it changes rather than after every fourth step.
                """
                nonlocal plan_pending, since_checkpoint, retry_pending
                if plan_pending:
                    await _save(
                        job.id,
                        job.lease_token,
                        checkpoint={"steps": steps, "offset": offset},
                        error_code=None,
                        error_detail=None,
                        attempts=0,
                        **values,
                    )
                else:
                    await _save_offset(
                        job.id,
                        job.lease_token,
                        offset,
                        clear_retry_at=retry_pending,
                        error_code=None,
                        error_detail=None,
                        attempts=0,
                        **values,
                    )
                plan_pending = False
                since_checkpoint = 0
                retry_pending = False

            while offset < len(steps) and fetched < settings.catalog_import_batch:
                if time.monotonic() > pass_deadline:
                    break
                # As many pages as there are clients to read them with, never
                # more than the batch still allows.
                window = steps[offset:offset + min(source.size, settings.catalog_import_batch - fetched)]
                results = await source.read_all(window)
                # A failure that is not about one page - a lost lease, an
                # authentication failure, a deferral - ends the pass, but only
                # after the pages fetched alongside it have been kept.
                stop: BaseException | None = None
                # Expansions are applied after the whole window rather than
                # immediately, because the rest of the window has already been
                # read and inserting ahead of it would move it under the cursor.
                inserts: list[dict] = []
                appends: list[dict] = []
                for step, payload, failure in results:
                    if failure is not None and not isinstance(failure, ValueError):
                        stop = failure
                        break
                    if isinstance(failure, ValueError):
                        # A page METU will not give us is one page, not a broken
                        # import. The parser is right to refuse what it cannot
                        # read or identify; what was wrong is that the refusal
                        # ended the pass. Twice that has cost a whole school: a
                        # programme with no course list sorts first among 207
                        # departments, and later a single section's restriction
                        # table stopped an import 429 steps in with 2467 courses
                        # already read. It is recorded against its own step - it
                        # shows on that course in the panel - and the plan moves
                        # on. A pass where nothing answered still fails, below.
                        logger.warning(
                            "catalog_step_skipped",
                            job_id=str(job.id),
                            step_tool=step["tool"],
                            step_values=step["values"],
                            detail=_safe_error_detail(str(failure)),
                        )
                        async with SessionLocal() as db:
                            await _owned_job(db, job.id, job.lease_token)
                            await catalog_service.ingest_observation(
                                db, job.organization_id, step["tool"], step["values"], None,
                                datetime.now(UTC), source_fetched_at=None, job_id=job.id,
                            )
                            await db.commit()
                        # The first one, which is the one that says what went
                        # wrong; the ones after it are usually the same again.
                        listing_refusal = listing_refusal or failure
                        listing_refusal_step = listing_refusal_step or step
                        offset += 1
                        fetched += 1
                        since_checkpoint += 1
                        continue
                    observed = datetime.now(UTC)
                    async with SessionLocal() as db:
                        # The fence stays on the cheap path: a worker whose
                        # lease was replaced must not write an observation.
                        await _owned_job(db, job.id, job.lease_token)
                        await catalog_service.ingest_observation(
                            db, job.organization_id, step["tool"], step["values"], payload,
                            observed, source_fetched_at=observed, job_id=job.id,
                        )
                        await db.commit()
                    added = expand_steps(step, payload, job.term)
                    discovery_only = (job.payload or {}).get("discovery_only")
                    if discovery_only and step["tool"] == "list_program_courses":
                        added = []
                    if added and not force_refresh:
                        async with SessionLocal() as db:
                            added = await catalog_service.drop_fresh_leaf_steps(
                                db, job.organization_id, job.term, added, force=False,
                            )
                    if added:
                        # Restrictions for this course precede the next
                        # expensive detail fetch; a listing's courses go on the
                        # end. Applied after the window, below.
                        (inserts if step["tool"] == "get_course_info" else appends).extend(added)
                    offset += 1
                    fetched += 1
                    succeeded += 1
                    since_checkpoint += 1
                if inserts:
                    steps[offset:offset] = inserts
                    plan_pending = True
                if appends:
                    existing = {_step_key(s) for s in steps}
                    fresh = [s for s in appends if _step_key(s) not in existing]
                    if fresh:
                        steps.extend(fresh)
                        plan_pending = True
                if stop is not None:
                    # The cursor points at the step that was not read, and the
                    # plan the read pages grew is kept, so the retry resumes
                    # here rather than re-walking the window.
                    await _checkpoint()
                    raise stop
                if since_checkpoint >= checkpoint_every:
                    await _checkpoint()
            if succeeded == 0 and listing_refusal is not None and started_offset == 0:
                # A source that never answered at all belongs in the job's
                # error, rather than in an import that reports success and
                # imports nothing. But only when it never answered *at all*.
                #
                # This used to fire whenever a single batch refused end to end,
                # and a batch is fifteen steps. Fifteen consecutive unreadable
                # restriction tables is an ordinary thing for METU to have -
                # a department whose tables are not published, a run of
                # cross-listed service courses - and it killed the import.
                #
                # Measured: a whole-term job walked 4817 of 10048 steps over
                # roughly twenty-four hours, hit a run of refusals in
                # get_section_constraints, raised, exhausted its three attempts
                # and was marked failed - discarding forty-eight per cent of a
                # day's work. The job's own record disproves the inference the
                # code was making: a source that "is not working" does not
                # answer 4817 times first.
                #
                # A parse refusal is now what it says it is: this page could not
                # be read. It is recorded on its own observation and shows
                # against that course in the panel, and the pass moves on. The
                # failures that really are about the source - a lost lease, an
                # authentication failure, a deferral - have their own exception
                # types and still end the pass above.
                raise listing_refusal
            done = offset >= len(steps)
            await _checkpoint(
                status="completed" if done else "queued",
                lease_until=None,
                completed_at=datetime.now(UTC) if done else None,
            )
            return CatalogPassResult(
                fetched,
                outcome="completed" if done else "progress",
                job_id=job.id,
                scheduled_count=scheduled_count,
            )
        except LeaseLost:
            logger.info("catalog_import_lease_replaced", job_id=str(job.id))
            return CatalogPassResult(
                fetched,
                outcome="lease_lost",
                job_id=job.id,
                error_code="lease_lost",
                scheduled_count=scheduled_count,
            )
        except ImportDeferred as exc:
            await _save_offset(
                job.id,
                job.lease_token,
                offset,
                status="queued",
                lease_until=None,
                error_code=str(exc),
                error_detail=_safe_error_detail(str(exc)),
            )
            return CatalogPassResult(
                fetched,
                outcome="deferred",
                job_id=job.id,
                error_code=str(exc),
                error_detail=_safe_error_detail(str(exc)),
                scheduled_count=scheduled_count,
            )
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
            retry_at = (datetime.now(UTC) + timedelta(seconds=min(3600, 60 * 2 ** attempts))).isoformat()
            # Past the end of the plan when every listing was skipped, so the
            # step that first refused is the one worth naming.
            failed_step = steps[offset] if offset < len(steps) else listing_refusal_step
            error_code = "source_authentication_failed" if auth else type(exc).__name__
            error_detail = None if auth else _failure_detail(exc, failed_step)
            await _save_offset(
                job.id,
                job.lease_token,
                offset,
                retry_at=retry_at,
                status="failed" if auth or attempts >= settings.academic_catalog_max_attempts else "queued",
                attempts=attempts,
                lease_until=None,
                error_code=error_code,
                error_detail=error_detail,
            )
            # The traceback, because the class name alone has cost this project
            # a day: the same ValueError has failed every import and nothing
            # recorded where it came from. Not written to the job row - that is
            # read by the admin panel - only to the worker's own log, and the
            # step values are course and department codes, not the account's.
            logger.warning(
                "catalog_import_failed",
                job_id=str(job.id),
                error_type=type(exc).__name__,
                detail=error_detail,
                step_tool=(failed_step or {}).get("tool"),
                step_values=(failed_step or {}).get("values"),
                traceback=None if auth else traceback.format_exc()[-2000:],
            )
            return CatalogPassResult(
                fetched,
                outcome="failed",
                job_id=job.id,
                error_code=error_code,
                error_detail=error_detail,
                scheduled_count=scheduled_count,
            )
        finally:
            try:
                if source is not None:
                    await source.aclose()
            finally:
                await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
    return CatalogPassResult(fetched, outcome="idle", scheduled_count=scheduled_count)

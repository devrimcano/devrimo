"""Small, paginated operations views. Never return scraped bodies or proxy secrets."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.audit import record_event
from app.admin.auth import AdminPermission, AdminPrincipal, require
from app.config import get_settings
from app.db.session import get_db
from app.researchers.models import Researcher, ResearcherImportItem, ResearcherImportRun, ResearcherSection
from app.researchers.service import LOCK_ID

router = APIRouter(prefix="/researchers")
CONTROL_LOCK = LOCK_ID + 1


class RunView(BaseModel):
    id: UUID
    status: str
    started_at: datetime
    finished_at: datetime | None
    selected: int
    completed: int
    incomplete: int
    pending: int
    running: int
    discovered: int | None
    limit: int | None
    last_error: str | None


class DashboardView(BaseModel):
    researchers: int
    sections: int
    busy: bool
    proxy_enabled: bool
    runs: list[RunView]


class StartImport(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=10000)


class ImportErrorView(BaseModel):
    section: str
    error: str


class ItemView(BaseModel):
    id: int
    name: str
    source_url: str
    status: str
    outcome: str | None
    completed_sections: int
    errors: list[ImportErrorView]


class ItemPage(BaseModel):
    items: list[ItemView]
    total: int
    offset: int


class ResearcherView(BaseModel):
    id: int
    name: str
    title: str | None
    affiliation: str | None
    email: str | None
    source_url: str
    last_seen_at: datetime


class ResearcherPage(BaseModel):
    items: list[ResearcherView]
    total: int
    offset: int


async def importer_busy(db) -> bool:
    # The importer owns this session lock throughout the crawl. A killed process
    # may leave status='running', but its disconnected session releases the lock.
    return bool(
        await db.scalar(
            text("""
        SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory'
            AND classid=0 AND objid=:key AND objsubid=1 AND granted
            AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))
    """),
            {"key": LOCK_ID},
        )
    )


async def views(db, runs, busy):
    if not runs:
        return []
    counts = (
        await db.execute(
            select(
                ResearcherImportItem.run_id,
                ResearcherImportItem.status,
                func.count(),
            )
            .where(ResearcherImportItem.run_id.in_([run.id for run in runs]))
            .group_by(ResearcherImportItem.run_id, ResearcherImportItem.status)
        )
    ).all()
    by_run = {}
    for run_id, state, count in counts:
        by_run.setdefault(run_id, {})[state] = count
    lock_pid = (
        await db.scalar(
            text("""
        SELECT pid FROM pg_locks WHERE locktype='advisory'
        AND classid=0 AND objid=:key AND objsubid=1 AND granted
        AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
    """),
            {"key": LOCK_ID},
        )
        if busy
        else None
    )
    running_runs = [run for run in runs if run.status == "running"]
    active = next((run for run in running_runs if lock_pid and run.options.get("lock_pid") == lock_pid), None)
    # Compatibility with a CLI import started before lock ownership was recorded.
    legacy = [run for run in running_runs if not run.options.get("lock_pid")]
    if active is None and lock_pid and len(legacy) == 1:
        active = legacy[0]
    active_id = active.id if active else None
    result = []
    for run in runs:
        count = by_run.get(run.id, {})
        state = run.status
        if state == "running" and run.id != active_id:
            state = "interrupted"
        result.append(
            RunView(
                id=run.id,
                status=state,
                started_at=run.started_at,
                finished_at=run.finished_at,
                selected=sum(count.values()),
                completed=count.get("completed", 0),
                incomplete=count.get("incomplete", 0),
                pending=count.get("pending", 0),
                running=count.get("running", 0),
                discovered=run.discovery.get("reported_total"),
                limit=run.options.get("limit"),
                last_error=run.discovery.get("last_error"),
            )
        )
    return result


@router.get("/dashboard", response_model=DashboardView)
async def dashboard(
    principal: AdminPrincipal = Depends(require(AdminPermission.researchers_read)),
    db: AsyncSession = Depends(get_db),
):
    busy = await importer_busy(db)
    runs = list(
        (await db.scalars(select(ResearcherImportRun).order_by(ResearcherImportRun.started_at.desc()).limit(20))).all()
    )
    return DashboardView(
        researchers=await db.scalar(select(func.count()).select_from(Researcher)),
        sections=await db.scalar(select(func.count()).select_from(ResearcherSection)),
        busy=busy,
        proxy_enabled=bool(get_settings().avesis_proxy_url and get_settings().avesis_proxy_url.get_secret_value()),
        runs=await views(db, runs, busy),
    )


@router.get("", response_model=ResearcherPage)
async def directory(
    q: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
    principal: AdminPrincipal = Depends(require(AdminPermission.researchers_read)),
    db: AsyncSession = Depends(get_db),
):
    query = select(Researcher)
    if q.strip():
        pattern = q.strip()
        query = query.where(
            or_(
                Researcher.name.icontains(pattern, autoescape=True),
                Researcher.affiliation.icontains(pattern, autoescape=True),
            )
        )
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.scalars(query.order_by(Researcher.name, Researcher.id).offset(offset).limit(25))).all()
    return ResearcherPage(
        items=[ResearcherView.model_validate(row, from_attributes=True) for row in rows], total=total, offset=offset
    )


@router.get("/runs/{run_id}/items", response_model=ItemPage)
async def run_items(
    run_id: UUID,
    offset: int = Query(default=0, ge=0),
    errors_only: bool = False,
    principal: AdminPrincipal = Depends(require(AdminPermission.researchers_read)),
    db: AsyncSession = Depends(get_db),
):
    if await db.get(ResearcherImportRun, run_id) is None:
        raise HTTPException(404, "Import run not found")
    query = select(ResearcherImportItem).where(ResearcherImportItem.run_id == run_id)
    if errors_only:
        query = query.where(ResearcherImportItem.status == "incomplete")
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.scalars(query.order_by(ResearcherImportItem.source_id).offset(offset).limit(25))).all()
    return ItemPage(
        items=[
            ItemView(
                id=row.source_id,
                name=row.identity.get("display_name", row.identity.get("alias", str(row.source_id))),
                source_url=row.identity["source_url"],
                status=row.status,
                outcome=row.outcome,
                completed_sections=len(row.completed_sections),
                errors=row.errors,
            )
            for row in rows
        ],
        total=total,
        offset=offset,
    )


async def ensure_idle(db):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CONTROL_LOCK})
    queued = await db.scalar(select(ResearcherImportRun.id).where(ResearcherImportRun.status == "queued").limit(1))
    if await importer_busy(db) or queued:
        raise HTTPException(409, "An import is already running or queued")


@router.post("/runs", response_model=RunView, status_code=202)
async def start_import(
    body: StartImport,
    principal: AdminPrincipal = Depends(require(AdminPermission.researchers_write)),
    db: AsyncSession = Depends(get_db),
):
    await ensure_idle(db)
    run = ResearcherImportRun(status="queued", options={"language": "en", "limit": body.limit})
    db.add(run)
    await db.flush()
    await record_event(
        db,
        actor_user_id=principal.user.id,
        action="researchers.import.start",
        result="success",
        after={"run_id": str(run.id), "limit": body.limit},
    )
    return (await views(db, [run], False))[0]


@router.post("/runs/{run_id}/resume", response_model=RunView, status_code=202)
async def resume_import(
    run_id: UUID,
    principal: AdminPrincipal = Depends(require(AdminPermission.researchers_write)),
    db: AsyncSession = Depends(get_db),
):
    await ensure_idle(db)
    run = await db.get(ResearcherImportRun, run_id)
    if run is None:
        raise HTTPException(404, "Import run not found")
    if run.status == "completed":
        raise HTTPException(409, "This import is already complete; start a new refresh instead")
    run.status, run.finished_at = "queued", None
    await record_event(
        db,
        actor_user_id=principal.user.id,
        action="researchers.import.resume",
        result="success",
        after={"run_id": str(run.id)},
    )
    return (await views(db, [run], False))[0]

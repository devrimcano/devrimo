import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agents.pool import reset_pool
from app.agents.reconciler import run_reconciler_loop
from app.api.v1 import router as api_v1_router
from app.api.v1.admin import sync_directory
from app.campus.manifest import commits_by_slug
from app.config import get_settings
from app.db.session import SessionLocal
from app.knowledge.embeddings import close_embedding_client
from app.logging import configure_logging, get_logger
from app.observability import ObservabilityMiddleware
from app.observability.client import initialize as posthog_initialize
from app.observability.client import report_exception
from app.observability.client import shutdown as posthog_shutdown
from app.observability.context import REQUEST_ID_HEADER, current_request_id
from app.observability.jobs import observed_job
from app.observability.logs import shutdown as posthog_logs_shutdown
from app.observability.runtime import SERVICE_BROKER, configure_service

configure_service(SERVICE_BROKER)
configure_logging()
logger = get_logger(__name__)


async def _run_directory_sync_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    if not settings.supabase_secret_key or not settings.supabase_url:
        return
    while not stop_event.is_set():
        # Each pass is its own unit of work: a failing directory sync used to
        # produce one warning line with no correlation id and no issue, so a
        # user directory that had been stale for days looked like silence.
        with observed_job("directory_sync") as job:
            try:
                async with SessionLocal() as db:
                    synced = await sync_directory(db)
                job.succeeded(users=synced)
                logger.info("admin_directory_synced", users=synced)
            except Exception as exc:
                job.failed(exc)
                logger.warning("admin_directory_sync_failed", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.admin_directory_sync_seconds)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.agent_tracing_enabled:
        # OpenInference redaction flags are set in deployment configuration.
        # This exporter writes to our Agno DB; it does not send traces to Agno.
        from agno.tracing import setup_tracing

        from app.agents.store import get_agno_db

        setup_tracing(db=get_agno_db(), batch_processing=True)
    # Constructed eagerly so a missing or mis-configured key is reported at
    # boot rather than discovered later as an absence of data.
    posthog_initialize()
    stop_event = asyncio.Event()
    reconciler_task = asyncio.create_task(run_reconciler_loop(stop_event))
    directory_sync_task = asyncio.create_task(_run_directory_sync_loop(stop_event))
    logger.info("startup_complete")
    try:
        yield
    finally:
        stop_event.set()
        await reconciler_task
        await directory_sync_task
        # Every resident agent is holding MCP subprocesses that have a
        # student's METU credentials in their environment; leaving them
        # parented to a dead broker is not acceptable.
        await reset_pool()
        await close_embedding_client()
        # Last, so anything the teardown above reported is still flushed. An
        # unflushed queue at SIGTERM loses exactly the events that explain
        # why the process is going away.
        await asyncio.to_thread(posthog_shutdown)
        await asyncio.to_thread(posthog_logs_shutdown)


app = FastAPI(title="Devrimo Agent Broker", lifespan=lifespan)

settings = get_settings()
# Added first, so it sits *inside* CORSMiddleware: Starlette applies middleware
# in reverse, and CORS headers must survive on responses this one observes.
app.add_middleware(ObservabilityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/health")
async def root_health() -> dict[str, object]:
    """Liveness, plus which commit each campus MCP server was built from.

    The commits are reported because the servers are pinned by build arg and
    the build deletes their ``.git`` directories: without this, the pin is
    unverifiable from a running container. They name public commits in public
    repositories, so exposing them unauthenticated discloses nothing that
    reading those repositories would not — and the alternative, guessing which
    image is deployed during an incident, is worse.
    """
    return {"status": "ok", "campus_servers": commits_by_slug(settings.campus_mcp_root)}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """The fallback reporter, and the response the student actually receives.

    Starlette runs this handler *outside* ObservabilityMiddleware, so anything
    reported from here has already lost the request id, the user and the tags —
    which is why the middleware reports first. This stays as a genuine fallback
    for exceptions raised before the middleware could see them, and it
    deduplicates against the middleware's report rather than filing a second,
    context-free issue for the same failure.

    The request id is echoed back so a student's screenshot of an error, a
    browser event, a proxy log and a broker issue all name the same id.
    """
    request_id = current_request_id.get() or request.headers.get(REQUEST_ID_HEADER)
    logger.error("unhandled_exception", path=request.url.path, error=str(exc), request_id=request_id)
    reported = report_exception(
        exc,
        path=request.url.path,
        method=request.method,
        request_id=request_id,
        handler="unhandled_exception",
    )
    if not reported:
        logger.debug("unhandled_exception_already_reported", path=request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error", "request_id": request_id},
        headers={REQUEST_ID_HEADER: request_id} if request_id else None,
    )

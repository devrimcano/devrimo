import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import router as api_v1_router
from app.campus.manifest import commits_by_slug
from app.campus.session_pool import close_all
from app.config import get_settings
from app.db.session import validate_runtime_database
from app.knowledge.embeddings import close_embedding_client
from app.logging import configure_logging, get_logger
from app.observability import ObservabilityMiddleware
from app.observability.client import initialize as posthog_initialize
from app.observability.client import report_exception
from app.observability.client import shutdown as posthog_shutdown
from app.observability.context import REQUEST_ID_HEADER, current_request_id
from app.observability.logs import shutdown as posthog_logs_shutdown
from app.observability.runtime import SERVICE_BROKER, configure_service
from app.workspace.gateway import create_gateway

configure_service(SERVICE_BROKER)
configure_logging()
logger = get_logger(__name__)


gateway_server, gateway_app = create_gateway()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await validate_runtime_database()
    # Constructed eagerly so a missing or mis-configured key is reported at
    # boot rather than discovered later as an absence of data.
    posthog_initialize()
    logger.info("startup_complete")
    try:
        async with gateway_server.session_manager.run():
            yield
    finally:
        # Shut down leased campus subprocesses before the API exits.
        await close_all()
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
    expose_headers=["X-Run-ID", "X-Request-ID"],
)

app.include_router(api_v1_router, prefix="/api/v1")
app.mount("/mcp", gateway_app)


@app.get("/health")
async def root_health() -> dict[str, str]:
    """Liveness only.

    The public edge serves this path, so it answers with the one thing a
    liveness probe needs and nothing that describes the deployment.
    """
    return {"status": "ok"}


@app.get("/internal/build-manifest")
async def build_manifest() -> dict[str, object]:
    """Which commit each campus MCP server was built from.

    Kept off ``/health`` deliberately. The servers are pinned by build arg and
    their ``.git`` directories are deleted at build time, so a running process
    is the only place the pin can be read — but the public edge proxies
    ``/health``, and an image's exact third-party pins are supply-chain detail
    that does not need to be world-readable. Caddy routes nothing under
    ``/internal`` and the API listens on loopback, so this answers only to a
    process on the host.
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
    request_id = (
        getattr(request.state, "request_id", None) or current_request_id.get() or request.headers.get(REQUEST_ID_HEADER)
    )
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

"""Process identity that every telemetry signal is labelled with.

Three services run this source tree — the FastAPI broker, the knowledge worker,
and the test suite — and until now all three reported themselves to PostHog as
``devrimo-broker``. A worker log line and a request log line were therefore
indistinguishable, which makes "is the worker healthy?" unanswerable from the
data.

The service name is process-level state rather than a setting because it is a
property of the *entrypoint*, not of the deployment: the same ``.env`` starts
both. Each entrypoint calls :func:`configure_service` before it configures
logging or touches the PostHog client.

``release`` is the deployed commit. It is what turns "this started failing" into
"this started failing in that change", and it is passed through to the frontend
source-map upload so browser stack traces resolve against the same revision.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

# Bumped when the meaning of an emitted property changes, so a query can tell
# events produced by two different shapes of this code apart.
TELEMETRY_SCHEMA_VERSION = 2

SERVICE_BROKER = "devrimo-broker"
SERVICE_KNOWLEDGE_WORKER = "devrimo-knowledge-worker"

# Written by ``scripts/deploy-vps.sh`` next to the deployed tree, so the running
# services learn their revision without every systemd unit needing a new
# ``Environment=`` line. The env var wins when both are present.
_RELEASE_FILE = Path(__file__).resolve().parents[3] / ".release-sha"

_service_name = SERVICE_BROKER


def configure_service(name: str) -> None:
    """Name this process. Called once, first thing, by each entrypoint."""
    global _service_name
    _service_name = name


def service_name() -> str:
    return _service_name


@lru_cache
def release() -> str:
    """The deployed commit, from the environment or the deploy marker file."""
    from app.config import get_settings

    configured = get_settings().release.strip()
    if configured:
        return configured
    try:
        return _RELEASE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def environment() -> str:
    from app.config import get_settings

    return get_settings().environment


def service_properties() -> dict[str, Any]:
    """The labels every event, log record and span carries."""
    properties: dict[str, Any] = {
        "service": service_name(),
        "environment": environment(),
        "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
    }
    current_release = release()
    if current_release:
        properties["release"] = current_release
    return properties

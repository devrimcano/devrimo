"""Render one student's campus MCP servers as launchable stdio specs.

Replaces the Hermes ``config.yaml`` rendering this module used to do. Agno
launches each server itself, as a subprocess of the broker, so there is no
config file to merge and no container to stage it into — a spec here becomes
an ``mcp.StdioServerParameters`` in :mod:`app.campus.toolkits` and nothing else.

The credential isolation that used to come from one container per student now
comes from process environment: the MCP SDK spawns each server with only
``HOME/LOGNAME/PATH/SHELL/TERM/USER`` inherited from the broker plus this
spec's ``env``, so a campus server sees its own student's METU password and
neither another student's nor the broker's own ``SECRET_ENCRYPTION_KEY``.

Nothing here touches the database or the network, which is what keeps it cheap
to test: given a catalog entry, a secrets bundle, and the two path roots, every
spec is a pure function of the four.
"""

from dataclasses import dataclass, field
from uuid import UUID

from app.campus.catalog import CAMPUS_TOOLS, CampusTool
from app.campus.credentials import CampusSecrets
from app.campus.validation import validate_odtuclass_base_url
from app.config import get_settings


@dataclass(frozen=True)
class CampusServerSpec:
    """Everything needed to launch one campus MCP server for one student."""

    tool_id: str
    command: str
    args: tuple[str, ...]
    # Carries the student's METU password. Never log this dict directly; use
    # :meth:`describe`.
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    include_tools: tuple[str, ...] = ()
    requires_confirmation_tools: tuple[str, ...] = ()

    def __repr__(self) -> str:  # pragma: no cover - defensive, not behaviour
        return f"CampusServerSpec(tool_id={self.tool_id!r}, env_keys={sorted(self.env)}, ...redacted)"

    def describe(self) -> dict[str, object]:
        """A log-safe view: which server, which knobs, which env keys — no values."""
        return {
            "tool_id": self.tool_id,
            "command": self.command,
            "env_keys": sorted(self.env),
            "cwd": self.cwd,
        }


def _render_env(tool: CampusTool, values: dict[str, str]) -> dict[str, str]:
    rendered: dict[str, str] = {}
    for key, template in tool.env_template.items():
        try:
            value = template.format(**values)
        except KeyError:
            # A template referencing a value the bundle doesn't carry is a
            # catalog bug, not a user error; skip the key rather than break
            # every other server for this student.
            continue
        # Empty is meaningfully different from absent here: an empty
        # ODTUCLASS_TOKEN would make the upstream client prefer a blank token
        # over falling back to username/password login.
        if value:
            rendered[key] = value
    return rendered


def enabled_tools(enabled_ids: list[str], secrets: CampusSecrets | None) -> list[CampusTool]:
    """The tools the student asked for that they've actually supplied credentials for."""
    if secrets is None:
        return []
    chosen = set(enabled_ids)
    return [tool for tool in CAMPUS_TOOLS if tool.id in chosen and all(secrets.has(kind) for kind in tool.requires)]


def state_dir_for(state_root: str, user_id: UUID, tool: CampusTool) -> str | None:
    """The private working directory this student's copy of ``tool`` runs in."""
    if tool.state_slug is None:
        return None
    return f"{state_root.rstrip('/')}/{user_id}/{tool.state_slug}"


def _runtime_flags() -> dict[str, str]:
    settings = get_settings()
    return {"course_info_session_cache": "1" if settings.course_info_session_cache else "0"}


def build_server_specs(
    user_id: UUID,
    enabled_ids: list[str],
    secrets: CampusSecrets | None,
    *,
    mcp_root: str,
    state_root: str,
) -> list[CampusServerSpec]:
    # Secrets, plus the non-secret switches a server reads from its
    # environment. Keeping the second kind here is what makes turning one off a
    # broker config change and a restart rather than an image rebuild.
    values = {**(secrets.as_template_values() if secrets else {}), **_runtime_flags()}
    root = mcp_root.rstrip("/")

    specs: list[CampusServerSpec] = []
    for tool in enabled_tools(enabled_ids, secrets):
        launch_values = values
        if tool.id == "odtuclass":
            # Validate again at the launch boundary so legacy rows or direct
            # service callers cannot bypass the request model and hand an
            # arbitrary endpoint to the credential-bearing MCP process. Keep
            # this check inside the ODTUClass branch: an old invalid optional
            # URL must not disable unrelated campus servers.
            launch_values = {
                **values,
                "odtuclass_base_url": validate_odtuclass_base_url(values.get("odtuclass_base_url")) or "",
            }
        venv_root = f"{root}/{tool.venv_slug}"
        specs.append(
            CampusServerSpec(
                tool_id=tool.id,
                command=f"{venv_root}/.venv/bin/python",
                # odtuclass ships loose modules rather than an installable
                # package, so it is launched by script path — which is only
                # knowable once the install root is.
                args=tuple(arg.format(venv_root=venv_root) for arg in tool.args),
                env=_render_env(tool, launch_values),
                cwd=state_dir_for(state_root, user_id, tool),
                include_tools=tool.include_tools,
                requires_confirmation_tools=tool.requires_confirmation_tools,
            )
        )
    return specs


def working_directories(specs: list[CampusServerSpec]) -> list[str]:
    """CWDs that must exist before the servers are launched."""
    return [spec.cwd for spec in specs if spec.cwd]

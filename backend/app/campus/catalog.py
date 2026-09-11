"""The registry of campus MCP servers that can be attached to a user's agent.

Each entry describes one Model Context Protocol server that the broker
launches over stdio as a subprocess, once per student who enabled it. Running
them per-student rather than as shared HTTP services is deliberate: three of
the four upstream servers are single-tenant and read their credentials from
process environment, so a shared deployment would put every student's METU
password in one process. Here each subprocess is spawned with only its own
student's credentials — see :mod:`app.campus.mcp_config`.

``env_template`` values are rendered by :mod:`app.campus.mcp_config` against a
:class:`~app.campus.credentials.CampusSecrets` bundle. Keys whose value renders
empty are dropped, so a server that needs a credential the student hasn't
supplied simply never appears in the generated config.
"""

from dataclasses import dataclass, field
from typing import Literal

# Credentials a server can require. The onboarding UI asks for the union of
# every requirement across the tools the student chose to enable.
CredentialKind = Literal["metu_password", "odtuclass"]


@dataclass(frozen=True)
class CampusTool:
    id: str
    name_en: str
    name_tr: str
    description_en: str
    description_tr: str
    # What the student is trusting this server with, in their own words. Shown
    # verbatim in onboarding — this is the consent copy, not decoration.
    scope_en: str
    scope_tr: str
    requires: tuple[CredentialKind, ...]
    # The virtualenv this server is launched from, as a directory name under
    # ``campus_mcp_root``. A slug rather than an absolute path so the install
    # location is deployment policy (app/config.py), not catalog data.
    venv_slug: str
    args: tuple[str, ...]
    env_template: dict[str, str] = field(default_factory=dict)
    # Set when the server writes relative to its CWD. Renders to a private
    # per-user directory under ``campus_state_root`` so two students never
    # share a cache. ``None`` means the server needs no writable directory.
    state_slug: str | None = None
    # Exact upstream names the model may receive. An allowlist means an
    # upstream release adding a tool grants no new authority automatically.
    include_tools: tuple[str, ...] = ()
    # Allowed tools which still require the student to approve the exact call.
    requires_confirmation_tools: tuple[str, ...] = ()
    default_enabled: bool = True


# Every server is installed into its own virtualenv under ``campus_mcp_root``
# by the image build, so each one gets an interpreter with only its own
# dependency tree — upstream pins conflict (mcp vs fastmcp, pydantic ranges)
# and a single shared site-packages would make the set unresolvable.

CAMPUS_TOOLS: tuple[CampusTool, ...] = (
    CampusTool(
        id="sais",
        name_en="SAIS student portal",
        name_tr="SAIS öğrenci portalı",
        description_en="Transcript, GPA, weekly schedule, and portal announcements",
        description_tr="Transkript, ortalama, haftalık ders programı ve portal duyuruları",
        scope_en="Reads your student record from student.metu.edu.tr. Read-only.",
        scope_tr="student.metu.edu.tr üzerindeki öğrenci kaydını okur. Salt okunur.",
        requires=("metu_password",),
        venv_slug="sais",
        args=("-m", "sais_mcp.server", "--transport", "stdio"),
        env_template={
            "SAIS_USERNAME": "{metu_username}",
            "SAIS_PASSWORD": "{metu_password}",
            "LOCALE": "{locale}",
        },
        state_slug="sais",
        include_tools=("get_student_info", "get_schedule", "get_transcript", "get_announcements"),
    ),
    CampusTool(
        id="course_info",
        name_en="Course catalog",
        name_tr="Ders kataloğu",
        description_en="Course codes, sections, prerequisites, ECTS, and curriculum categories",
        description_tr="Ders kodları, şubeler, ön koşullar, AKTS ve müfredat kategorileri",
        scope_en="Reads the METU course catalog and your curriculum requirements. Read-only.",
        scope_tr="ODTÜ ders kataloğunu ve müfredat gereksinimlerini okur. Salt okunur.",
        requires=("metu_password",),
        venv_slug="course-info",
        args=("-m", "metu_course_info_mcp", "--transport", "stdio"),
        env_template={
            "SAIS_USERNAME": "{metu_username}",
            "SAIS_PASSWORD": "{metu_password}",
            "LOCALE": "{locale}",
            # The vendored client holds the SAIS app-proxy session for ten
            # minutes instead of rebuilding it on every call. Switchable from
            # the broker's settings so backing it out is a config change and a
            # restart rather than an image rebuild.
            "COURSE_INFO_SESSION_CACHE": "{course_info_session_cache}",
            # Reuse the held session's navigation position instead of
            # re-selecting the department before every course read.
            "COURSE_INFO_NAV_ELISION": "{course_info_nav_elision}",
        },
        state_slug="course-info",
        include_tools=(
            "get_departments_and_semesters",
            "search_departments",
            "list_program_courses",
            "get_course_info",
            "get_section_constraints",
            "get_course_prerequisites",
            "get_course_replacements",
            "get_thesis_courses",
            "get_student_course_categories",
            "get_student_courses_by_category",
            "get_student_curriculum",
        ),
    ),
    CampusTool(
        id="odtuclass",
        name_en="ODTÜClass",
        name_tr="ODTÜClass",
        description_en="Enrolled courses, announcements, syllabi, and upcoming assignment deadlines",
        description_tr="Kayıtlı dersler, duyurular, izlenceler ve yaklaşan ödev teslimleri",
        scope_en="Reads your ODTÜClass courses and deadlines. Read-only.",
        scope_tr="ODTÜClass derslerini ve teslim tarihlerini okur. Salt okunur.",
        requires=("odtuclass",),
        venv_slug="odtuclass",
        args=("{venv_root}/odtuclass_mcp.py",),
        env_template={
            "ODTUCLASS_USERNAME": "{metu_username}",
            "ODTUCLASS_PASSWORD": "{metu_password}",
            "ODTUCLASS_TOKEN": "{odtuclass_token}",
            "ODTUCLASS_BASE_URL": "{odtuclass_base_url}",
        },
        # The upstream client caches its Moodle session token under
        # ``os.getcwd()/.odtuclass_cache`` and writes downloads next to it, so
        # its CWD has to be on the writable per-user volume.
        state_slug="odtuclass",
        include_tools=(
            "get_enrolled_courses",
            "get_course_announcements",
            "get_course_syllabus",
            "get_upcoming_assignments",
            "get_lab_recitation_info",
        ),
    ),
    CampusTool(
        id="webmail",
        name_en="METU webmail",
        name_tr="ODTÜ webmail",
        description_en="Read, search, and send mail from your @metu.edu.tr account",
        description_tr="@metu.edu.tr hesabında posta okuma, arama ve gönderme",
        scope_en=(
            "Reads and searches your METU mailbox and can send or reply only after you approve the exact message. "
            "It cannot delete, move, mark, or forward mail."
        ),
        scope_tr=(
            "ODTÜ posta kutunu okur ve arar; yalnızca tam iletiyi onayladıktan sonra gönderir veya yanıtlar. "
            "Posta silemez, taşıyamaz, işaretleyemez veya iletemez."
        ),
        requires=("metu_password",),
        venv_slug="webmail",
        args=("-m", "metu_webmail_mcp", "--transport", "stdio"),
        env_template={
            "METU_USERNAME": "{metu_username}",
            "METU_PASSWORD": "{metu_password}",
            "LOCALE": "{locale}",
        },
        state_slug="webmail",
        include_tools=(
            "get_mailbox_status",
            "list_folders",
            "list_emails",
            "search_emails",
            "read_email",
            "get_attachment",
            "send_email",
            "reply_email",
        ),
        requires_confirmation_tools=("send_email", "reply_email"),
        # Opt-in: it can send mail as the student, so it should be a decision
        # they make rather than a default they discover afterwards.
        default_enabled=False,
    ),
)

TOOLS_BY_ID: dict[str, CampusTool] = {tool.id: tool for tool in CAMPUS_TOOLS}
TOOL_IDS: frozenset[str] = frozenset(TOOLS_BY_ID)
DEFAULT_ENABLED_TOOL_IDS: tuple[str, ...] = tuple(t.id for t in CAMPUS_TOOLS if t.default_enabled)


def normalize_tool_ids(tool_ids: list[str] | None) -> list[str]:
    """Drop unknown ids and de-duplicate, preserving catalog order.

    Unknown ids are dropped rather than rejected so that removing a tool from
    the catalog doesn't 500 every request from a client that still remembers it.
    """
    if tool_ids is None:
        return list(DEFAULT_ENABLED_TOOL_IDS)
    requested = set(tool_ids)
    return [tool.id for tool in CAMPUS_TOOLS if tool.id in requested]

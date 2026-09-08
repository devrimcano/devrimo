"""Canonical API request and response shapes exported through OpenAPI."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.campus.catalog import CAMPUS_TOOLS, CampusTool
from app.campus.credentials import CampusSecrets
from app.campus.mcp_config import enabled_tools as resolve_enabled_tools
from app.db.models import Agent, AgentStatus, CampusCredential, ChatSession, UserProfile

ChatRole = Literal["system", "user", "assistant"]
Locale = Literal["tr", "en"]
CampusCredentialKind = Literal["metu_password", "odtuclass"]


class AgentOut(BaseModel):
    id: str
    user_id: str
    status: AgentStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, agent: Agent) -> "AgentOut":
        return cls(
            id=str(agent.id),
            user_id=str(agent.user_id),
            status=agent.status,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
        )


class ChatMessageIn(BaseModel):
    role: ChatRole
    content: str


class ChatCompletionsRequestIn(BaseModel):
    messages: list[ChatMessageIn]
    session_id: str | None = None
    stream: bool | None = None
    model: str | None = None


class ChatConfirmationIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=64)
    requirement_id: str = Field(min_length=1, max_length=255)
    approved: bool


class ChatMessageOut(BaseModel):
    role: ChatRole
    content: str
    created_at: str | None = None


class ChatSessionOut(BaseModel):
    id: str
    title: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, session: ChatSession) -> "ChatSessionOut":
        return cls(id=session.id, title=session.title, created_at=session.created_at, updated_at=session.updated_at)


class ChatSessionListOut(BaseModel):
    sessions: list[ChatSessionOut]


class ChatSessionDetailOut(ChatSessionOut):
    messages: list[ChatMessageOut]


class MemoryEntryOut(BaseModel):
    id: str
    content: str


class MemoryListOut(BaseModel):
    memories: list[MemoryEntryOut] = Field(default_factory=list)


# --- Campus MCP tools ---------------------------------------------------


class CampusToolOut(BaseModel):
    """One entry in the campus tool catalog, as the onboarding UI renders it."""

    id: str
    name_en: str
    name_tr: str
    description_en: str
    description_tr: str
    scope_en: str
    scope_tr: str
    requires: list[CampusCredentialKind]
    default_enabled: bool
    # Chosen by the student.
    enabled: bool = False
    # Chosen *and* backed by credentials that actually satisfy `requires`, i.e.
    # this server will be in the container's MCP config.
    active: bool = False

    @classmethod
    def from_catalog(cls, tool: CampusTool, *, enabled: bool, active: bool) -> "CampusToolOut":
        return cls(
            id=tool.id,
            name_en=tool.name_en,
            name_tr=tool.name_tr,
            description_en=tool.description_en,
            description_tr=tool.description_tr,
            scope_en=tool.scope_en,
            scope_tr=tool.scope_tr,
            requires=list(tool.requires),
            default_enabled=tool.default_enabled,
            enabled=enabled,
            active=active,
        )


class CampusConnectionOut(BaseModel):
    """The student's campus connection. Never carries a secret.

    ``has_password``/``has_odtuclass_token`` exist so the UI can say "a
    password is stored" and offer to replace it, without the value ever
    leaving the database.
    """

    connected: bool
    metu_username: str | None = None
    has_password: bool = False
    has_odtuclass_token: bool = False
    odtuclass_base_url: str | None = None
    locale: Locale = "tr"
    enabled_tools: list[str] = Field(default_factory=list)
    verified_at: datetime | None = None
    verification_error: str | None = None
    # True between saving a change and the agent container being rebuilt with it.
    needs_restart: bool = False
    tools: list[CampusToolOut] = Field(default_factory=list)

    @classmethod
    def from_model(
        cls,
        credential: CampusCredential | None,
        secrets: CampusSecrets | None,
        enabled_ids: list[str],
    ) -> "CampusConnectionOut":
        active_ids = {tool.id for tool in resolve_enabled_tools(enabled_ids, secrets)}
        tools = [
            CampusToolOut.from_catalog(
                tool,
                enabled=tool.id in set(enabled_ids),
                active=tool.id in active_ids,
            )
            for tool in CAMPUS_TOOLS
        ]
        if credential is None:
            return cls(connected=False, tools=tools)
        return cls(
            connected=True,
            metu_username=credential.metu_username,
            has_password=credential.metu_password_enc is not None,
            has_odtuclass_token=credential.odtuclass_token_enc is not None,
            odtuclass_base_url=credential.odtuclass_base_url,
            locale=credential.locale,
            enabled_tools=enabled_ids,
            verified_at=credential.verified_at,
            verification_error=credential.verification_error,
            needs_restart=credential.config_dirty,
            tools=tools,
        )


class CampusConnectionIn(BaseModel):
    metu_username: str = Field(min_length=1, max_length=255)
    # ``None`` keeps whatever is stored; "" clears it. See
    # app/campus/service.py::upsert_credential.
    metu_password: str | None = Field(default=None, max_length=512)
    odtuclass_token: str | None = Field(default=None, max_length=512)
    odtuclass_base_url: str | None = Field(default=None, max_length=255)
    locale: Locale = "tr"
    enabled_tools: list[str] | None = None
    # Skip the live SSO check — useful when METU is down and the student would
    # rather save now and find out later.
    skip_verification: bool = False

class CampusVerifyIn(BaseModel):
    metu_username: str = Field(min_length=1, max_length=255)
    metu_password: str = Field(min_length=1, max_length=512)


class CampusVerifyOut(BaseModel):
    ok: bool
    unreachable: bool = False
    detail: str | None = None


# --- Profile / onboarding -----------------------------------------------


class ProfileOut(BaseModel):
    user_id: str
    display_name: str | None = None
    department: str | None = None
    locale: Locale = "tr"
    mail_facts_enabled: bool = False
    onboarding_step: str | None = None
    onboarding_completed: bool = False
    onboarding_completed_at: datetime | None = None

    @classmethod
    def from_model(cls, profile: UserProfile) -> "ProfileOut":
        return cls(
            user_id=str(profile.user_id),
            display_name=profile.display_name,
            department=profile.department,
            locale=profile.locale,
            mail_facts_enabled=profile.mail_facts_enabled,
            onboarding_step=profile.onboarding_step,
            onboarding_completed=profile.onboarding_completed,
            onboarding_completed_at=profile.onboarding_completed_at,
        )


class ProfileIn(BaseModel):
    """Every field optional: the wizard PATCHes one step's worth at a time."""

    display_name: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=255)
    locale: Locale | None = None
    mail_facts_enabled: bool | None = None
    onboarding_step: str | None = Field(default=None, max_length=64)
    onboarding_completed: bool | None = None

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.db.models import AdminRole


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class DeleteUserIn(ReasonIn):
    confirm_email: str = Field(min_length=3, max_length=320)


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value:
            raise ValueError("A valid email is required")
        return value


class AgentActionIn(BaseModel):
    action: Literal["start", "stop", "restart", "destroy"]
    reason: str = Field(min_length=3, max_length=1000)


class MembershipIn(BaseModel):
    user_id: UUID
    role: AdminRole
    organization_id: UUID | None = None
    reason: str = Field(min_length=3, max_length=1000)


class RuntimeSettingsIn(BaseModel):
    model_id: str = Field(min_length=2, max_length=255)
    max_tokens: int = Field(ge=256, le=131072)
    scholar_history_runs: int = Field(ge=0, le=50)
    tool_call_limit: int = Field(ge=1, le=50)
    learning_enabled: bool
    input_token_price: float = Field(ge=0, le=1)
    output_token_price: float = Field(ge=0, le=1)
    # These defaults keep the fields optional for older admin clients. The PUT
    # handler uses ``model_fields_set`` so an omitted field preserves the
    # current database/environment value instead of writing this default.
    rate_limit_enabled: bool = False
    rate_limit_chat_per_minute: int = Field(default=20, ge=0, le=100000)
    rate_limit_catalog_per_minute: int = Field(default=120, ge=0, le=100000)
    reason: str = Field(min_length=3, max_length=1000)

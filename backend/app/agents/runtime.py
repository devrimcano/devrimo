from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AgentRuntimeSettings


@dataclass(frozen=True)
class AgentRuntimeConfig:
    model_id: str
    max_tokens: int
    scholar_history_runs: int
    tool_call_limit: int
    learning_enabled: bool
    input_token_price: float
    output_token_price: float
    revision: int

    def as_dict(self) -> dict:
        return asdict(self)


def default_runtime_config() -> AgentRuntimeConfig:
    settings = get_settings()
    return AgentRuntimeConfig(
        model_id=settings.agent_model,
        max_tokens=settings.agent_max_tokens,
        scholar_history_runs=settings.scholar_history_runs,
        tool_call_limit=settings.agent_tool_call_limit,
        learning_enabled=settings.agent_learning_enabled,
        input_token_price=settings.posthog_input_token_price,
        output_token_price=settings.posthog_output_token_price,
        revision=0,
    )


async def get_runtime_config(db: AsyncSession) -> AgentRuntimeConfig:
    settings = get_settings()
    row = await db.get(AgentRuntimeSettings, "default")
    return AgentRuntimeConfig(
        model_id=row.model_id if row and row.model_id else settings.agent_model,
        max_tokens=row.max_tokens if row and row.max_tokens is not None else settings.agent_max_tokens,
        scholar_history_runs=(
            row.scholar_history_runs if row and row.scholar_history_runs is not None else settings.scholar_history_runs
        ),
        tool_call_limit=(
            row.tool_call_limit if row and row.tool_call_limit is not None else settings.agent_tool_call_limit
        ),
        learning_enabled=(
            row.learning_enabled if row and row.learning_enabled is not None else settings.agent_learning_enabled
        ),
        input_token_price=(
            row.input_token_price if row and row.input_token_price is not None else settings.posthog_input_token_price
        ),
        output_token_price=(
            row.output_token_price
            if row and row.output_token_price is not None
            else settings.posthog_output_token_price
        ),
        revision=row.revision if row else 0,
    )


@dataclass(frozen=True)
class RateLimitConfig:
    """The request windows the API enforces, off until the row says otherwise."""

    enabled: bool
    chat_per_minute: int
    catalog_per_minute: int

    def as_dict(self) -> dict:
        # The runtime-settings API has always namespaced these controls. Keep
        # the internal names concise without leaking a different response
        # contract to the admin client.
        return {
            "rate_limit_enabled": self.enabled,
            "rate_limit_chat_per_minute": self.chat_per_minute,
            "rate_limit_catalog_per_minute": self.catalog_per_minute,
        }


def default_rate_limit_config() -> RateLimitConfig:
    settings = get_settings()
    return RateLimitConfig(
        enabled=settings.rate_limit_enabled,
        chat_per_minute=settings.rate_limit_chat_per_minute,
        catalog_per_minute=settings.rate_limit_catalog_per_minute,
    )


async def get_rate_limit_config(db: AsyncSession) -> RateLimitConfig:
    settings = get_settings()
    row = await db.get(AgentRuntimeSettings, "default")
    return RateLimitConfig(
        enabled=row.rate_limit_enabled if row and row.rate_limit_enabled is not None else settings.rate_limit_enabled,
        chat_per_minute=(
            row.rate_limit_chat_per_minute
            if row and row.rate_limit_chat_per_minute is not None
            else settings.rate_limit_chat_per_minute
        ),
        catalog_per_minute=(
            row.rate_limit_catalog_per_minute
            if row and row.rate_limit_catalog_per_minute is not None
            else settings.rate_limit_catalog_per_minute
        ),
    )

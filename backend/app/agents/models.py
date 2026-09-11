"""Model construction shared by the legacy and Scholar profiles.

This is the only place in the broker where a model client is built, which makes
it the one place worth instrumenting: injecting a PostHog-wrapped
``AsyncOpenAI`` here captures every generation the process makes, including the
ones Agno initiates on its own — the tool loop's follow-up completions, the
Scholar learning pass, and tool-result compression — none of which are visible
from the chat route.
"""

from agno.models.base import Model

from app.agents.runtime import AgentRuntimeConfig, default_runtime_config
from app.config import get_settings
from app.logging import get_logger
from app.observability.flags import FLAG_AGENT_MODEL, flag_variant
from app.observability.llm import build_traced_async_client

logger = get_logger(__name__)


def opencode_session_headers(session_id: str | None) -> dict[str, str]:
    """The header OpenCode Go began requiring, and refusing requests without.

    Measured against the runs this broker actually made: every turn up to
    2026-09-06 completed, and every turn from 2026-09-07 onwards failed with

        Error from provider (Console Go): Request is missing x-opencode-session
        and cannot be routed efficiently.

    Four days in which the assistant answered nobody, while the API key was
    valid the whole time and nothing in this repository had changed. The
    provider had started enforcing a header we never sent.

    Their documentation asks for "a stable session ID for each conversation",
    which is exactly what a chat session id is, so that is what travels.
    """
    if not session_id:
        return {}
    return {"x-opencode-session": session_id}


def _traced(model: Model, runtime: AgentRuntimeConfig, session_id: str | None = None) -> Model:
    """Swap in a PostHog-instrumented async client, if one can be built.

    Agno caches ``async_client`` and only rebuilds it when closed, so setting
    the field is enough. When PostHog is unconfigured this returns the model
    untouched and Agno constructs its own stock client as before — the agent
    behaves identically either way.
    """
    settings = get_settings()
    client_params = {
        "api_key": settings.agent_openai_api_key,
        "base_url": settings.agent_openai_base_url,
    }
    # The instrumented client replaces Agno's own, so the header has to be set
    # here too or tracing quietly turns the provider requirement back off.
    headers = opencode_session_headers(session_id)
    if headers:
        client_params["default_headers"] = headers
    client = build_traced_async_client(
        input_token_price=runtime.input_token_price,
        output_token_price=runtime.output_token_price,
        **client_params,
    )
    if client is None:
        return model
    model.async_client = client
    return model


# OpenCode Go does not serve every model on the same wire API. Muse Spark, the
# GPT 5.6 family and Grok answer on `/responses`; GLM, Kimi, DeepSeek, MiMo and
# the rest are OpenAI chat-completions models. The gateway does not translate
# between them - a model sent to the wrong path fails there, as a bare 500 or
# "not supported for format openai" - so the choice has to be made here, not in
# the model id alone. Qwen and MiniMax are served only on the Anthropic
# `/messages` endpoint, for which this broker builds no client.
_OPENCODE_RESPONSES_MODELS = ("muse-spark", "gpt-5.6", "gpt-6", "grok-")
_OPENCODE_MESSAGES_MODELS = ("qwen3.8", "qwen3.7", "qwen3.6", "qwen3.5", "minimax")


def opencode_uses_responses(model_id: str) -> bool:
    """Whether OpenCode Go serves this model on the Responses API."""
    return model_id.lower().startswith(_OPENCODE_RESPONSES_MODELS)


def _build_opencode_model(model_id: str, runtime: AgentRuntimeConfig, session_id: str | None) -> Model:
    settings = get_settings()
    headers = opencode_session_headers(session_id) or None
    if model_id.lower().startswith(_OPENCODE_MESSAGES_MODELS):
        raise RuntimeError(
            f"{model_id} is served on OpenCode Go's Anthropic /messages endpoint, which this "
            "broker builds no client for; choose a Responses or chat-completions model."
        )
    if opencode_uses_responses(model_id):
        from agno.models.openai import OpenAIResponses

        return _traced(
            OpenAIResponses(
                id=model_id,
                api_key=settings.agent_openai_api_key,
                base_url=settings.agent_openai_base_url,
                max_output_tokens=runtime.max_tokens,
                default_headers=headers,
            ),
            runtime,
            session_id,
        )
    from agno.models.openai import OpenAIChat

    return _traced(
        OpenAIChat(
            id=model_id,
            api_key=settings.agent_openai_api_key,
            base_url=settings.agent_openai_base_url,
            max_tokens=runtime.max_tokens,
            default_headers=headers,
        ),
        runtime,
        session_id,
    )


def build_model(runtime: AgentRuntimeConfig | None = None, *, session_id: str | None = None) -> Model:
    settings = get_settings()
    runtime = runtime or default_runtime_config()
    if settings.agent_runtime == "fake":
        from app.agents.echo_model import EchoModel

        return EchoModel()

    # A flagged model override makes a model A/B measurable directly from the
    # $ai_generation events, and makes rolling back a bad model immediate.
    model_id = flag_variant(FLAG_AGENT_MODEL, default=runtime.model_id)

    base_url = (settings.agent_openai_base_url or "").lower()
    if "opencode.ai" in base_url or model_id == "muse-spark-1.2-contributor":
        return _build_opencode_model(model_id, runtime, session_id)

    if "openrouter.ai" in base_url:
        from agno.models.openrouter import OpenRouter

        return _traced(
            OpenRouter(
                id=model_id,
                api_key=settings.agent_openai_api_key,
                base_url=settings.agent_openai_base_url,
                max_tokens=runtime.max_tokens,
            ),
            runtime,
        )

    from agno.models.openai import OpenAIChat

    return _traced(
        OpenAIChat(
            id=model_id,
            api_key=settings.agent_openai_api_key,
            base_url=settings.agent_openai_base_url,
            max_tokens=runtime.max_tokens,
        ),
        runtime,
    )

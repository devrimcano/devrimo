"""The header OpenCode Go requires, and the path it has to travel.

Every turn this broker made up to 2026-09-06 completed; every turn from
2026-09-07 onwards failed with

    Error from provider (Console Go): Request is missing x-opencode-session
    and cannot be routed efficiently.

Four days in which the assistant answered nobody. Verified against the live
provider with the production key: the same request is 400 MissingSessionID
without the header and 200 with it.

These tests pin the header itself and the route it takes from the run to the
model client, because the failure was not in either end - it was that nothing
carried a conversation id between them.
"""

import inspect

from app.agents import builders, legacy, models
from app.agents.scholar import build as scholar_build


def test_a_conversation_id_becomes_the_provider_session_header():
    assert models.opencode_session_headers("session-abc") == {"x-opencode-session": "session-abc"}


def test_no_conversation_means_no_header_rather_than_an_empty_one():
    """An empty value is a header the provider still has to reject."""
    assert models.opencode_session_headers(None) == {}
    assert models.opencode_session_headers("") == {}


def test_opencode_models_are_routed_to_the_wire_api_that_serves_them():
    """A model on the wrong wire API fails at the provider, not here.

    OpenCode Go serves Muse Spark, the GPT 5.6 family and Grok on /responses,
    and GLM, DeepSeek, MiMo and Kimi on /chat/completions. Sending one to the
    other path returns a bare 500 or "not supported for format openai", so the
    routing decision is pinned here.
    """
    for model_id in ("muse-spark-1.3-contributor", "muse-spark-1.2-contributor", "gpt-5.6-luna", "grok-4.6"):
        assert models.opencode_uses_responses(model_id), f"{model_id} should use /responses"
    for model_id in ("glm-5.3-flash", "deepseek-v4-flash", "mimo-v2.5", "kimi-k2.6", "hy3"):
        assert not models.opencode_uses_responses(model_id), f"{model_id} should use /chat/completions"


def test_every_builder_between_the_run_and_the_client_carries_the_session():
    """The bug was a missing parameter, so the parameter is what is pinned.

    A turn knows its session id; the model client is what must send it. If any
    link in this chain stops accepting it the header silently stops being sent,
    which is exactly the failure these tests exist to catch.
    """
    for function in (
        models.build_model,
        builders.build_agent,
        legacy.build_legacy_agent,
        scholar_build.build_scholar_agent,
    ):
        parameters = inspect.signature(function).parameters
        assert "session_id" in parameters, f"{function.__module__}.{function.__qualname__} drops the session id"


def test_the_worker_hands_the_run_its_own_session():
    """The one caller that knows which conversation a turn belongs to."""
    source = inspect.getsource(__import__("app.assistant.worker", fromlist=["worker"]))
    assert "session_id=run.session_id" in source, "the worker stopped passing the run's session to the agent"

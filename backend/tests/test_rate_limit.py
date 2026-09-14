"""The request-window infrastructure: off by default, correct when switched on.

Nothing here needs a database or a server: the mechanism is a sliding window
per scope and account, and the dependency is a pass-through while the runtime
setting keeps it disabled.
"""

import inspect
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.admin.schemas import RuntimeSettingsIn
from app.agents.runtime import RateLimitConfig
from app.api import rate_limit as api_rate_limit
from app.api.v1 import schedule
from app.core import rate_limit


def test_rate_limit_config_uses_the_admin_contract_names():
    assert RateLimitConfig(enabled=True, chat_per_minute=5, catalog_per_minute=60).as_dict() == {
        "rate_limit_enabled": True,
        "rate_limit_chat_per_minute": 5,
        "rate_limit_catalog_per_minute": 60,
    }


def test_runtime_settings_distinguish_omitted_rate_limits():
    body = RuntimeSettingsIn(
        model_id="openai/gpt-test",
        max_tokens=4096,
        scholar_history_runs=3,
        tool_call_limit=10,
        learning_enabled=True,
        input_token_price=0,
        output_token_price=0,
        reason="Compatibility test",
    )
    assert not {
        "rate_limit_enabled",
        "rate_limit_chat_per_minute",
        "rate_limit_catalog_per_minute",
    } & body.model_fields_set


def test_a_window_allows_its_budget_then_refuses():
    rate_limit.reset()
    user = str(uuid4())
    for _ in range(3):
        assert rate_limit.retry_after_seconds("chat", user, 3) is None
    retry_after = rate_limit.retry_after_seconds("chat", user, 3)
    assert retry_after is not None
    assert 1 <= retry_after <= 60


def test_retry_after_rounds_up_to_the_end_of_the_window(monkeypatch):
    rate_limit.reset()
    moments = iter((0.0, 0.1))
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: next(moments))
    assert rate_limit.retry_after_seconds("chat", "account-a", 1) is None
    assert rate_limit.retry_after_seconds("chat", "account-a", 1) == 60


def test_scopes_and_users_keep_separate_windows():
    rate_limit.reset()
    assert rate_limit.retry_after_seconds("chat", "account-a", 1) is None
    assert rate_limit.retry_after_seconds("catalog", "account-a", 1) is None
    assert rate_limit.retry_after_seconds("chat", "account-b", 1) is None


def test_zero_means_unlimited():
    rate_limit.reset()
    for _ in range(100):
        assert rate_limit.retry_after_seconds("chat", "account-a", 0) is None


async def test_the_dependency_is_a_pass_through_while_disabled(monkeypatch):
    rate_limit.reset()
    user = SimpleNamespace(id=uuid4())

    async def disabled(_db):
        return RateLimitConfig(enabled=False, chat_per_minute=1, catalog_per_minute=1)

    monkeypatch.setattr(api_rate_limit, "get_rate_limit_config", disabled)
    dependency = api_rate_limit.rate_limited("chat")
    assert await dependency(user=user, db=None) is user
    assert await dependency(user=user, db=None) is user


async def test_the_dependency_refuses_over_budget_when_enabled(monkeypatch):
    rate_limit.reset()
    user = SimpleNamespace(id=uuid4())

    async def enabled(_db):
        return RateLimitConfig(enabled=True, chat_per_minute=1, catalog_per_minute=1)

    monkeypatch.setattr(api_rate_limit, "get_rate_limit_config", enabled)
    dependency = api_rate_limit.rate_limited("chat")
    assert await dependency(user=user, db=None) is user
    with pytest.raises(HTTPException) as excinfo:
        await dependency(user=user, db=None)
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"]


def test_an_unknown_scope_is_a_programming_error():
    with pytest.raises(ValueError):
        api_rate_limit.rate_limited("not-a-scope")


@pytest.mark.parametrize(
    "endpoint",
    (
        schedule.search_departments,
        schedule.courses,
        schedule.search_courses,
        schedule.course_sections,
        schedule.planner_inputs,
        schedule.bulk_course_sections,
        schedule.bulk_constraints,
        schedule.course_section_constraints,
        schedule.curriculum_plan,
    ),
)
async def test_expensive_catalog_routes_share_the_catalog_limit(endpoint, monkeypatch):
    rate_limit.reset()
    user = SimpleNamespace(id=uuid4())

    async def enabled(_db):
        return RateLimitConfig(enabled=True, chat_per_minute=10, catalog_per_minute=1)

    monkeypatch.setattr(api_rate_limit, "get_rate_limit_config", enabled)
    dependency = inspect.signature(endpoint).parameters["user"].default.dependency
    assert await dependency(user=user, db=None) is user
    with pytest.raises(HTTPException) as excinfo:
        await dependency(user=user, db=None)
    assert excinfo.value.status_code == 429


def test_compose_forwards_rate_limit_environment_to_the_broker():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    broker = compose.split("\n  broker:\n", 1)[1].split("\n  knowledge-worker:\n", 1)[0]
    assert 'RATE_LIMIT_ENABLED: "${RATE_LIMIT_ENABLED:-false}"' in broker
    assert 'RATE_LIMIT_CHAT_PER_MINUTE: "${RATE_LIMIT_CHAT_PER_MINUTE:-20}"' in broker
    assert 'RATE_LIMIT_CATALOG_PER_MINUTE: "${RATE_LIMIT_CATALOG_PER_MINUTE:-120}"' in broker

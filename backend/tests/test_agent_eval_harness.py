"""The eval harness must never delete a memory it did not write.

The cleanup endpoint clears every memory the account owns, so the harness runs
the memory scenarios only when it has verified an empty (or explicitly
disposable) account, and a failed read of that state is not read as "empty".
"""

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "agent_eval_harness", Path(__file__).resolve().parents[1] / "scripts" / "agent_eval.py"
)
harness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harness)


def _set(*, allow: bool, disposable: bool):
    harness.ALLOW_MEMORY_WRITE = allow
    harness.DISPOSABLE = disposable


def test_memory_scenarios_need_the_flag_and_a_verified_account():
    _set(allow=False, disposable=True)
    assert not harness.memory_write_allowed([])
    _set(allow=True, disposable=False)
    assert harness.memory_write_allowed([])
    assert not harness.memory_write_allowed([{"id": "x", "content": "y"}])
    # A failed GET must never be read as "the account is empty".
    assert not harness.memory_write_allowed(None)
    _set(allow=True, disposable=True)
    assert harness.memory_write_allowed([{"id": "x", "content": "y"}])


def test_the_jargon_check_leaves_ordinary_turkish_alone():
    assert harness.JARGON.search("Kayıt yok, sorgulama tamam") is None
    assert harness.JARGON.search("published release") is not None


def test_memory_revision_counts_completed_updates_not_started_calls():
    result = {
        "tools": ["read", "update", "update"],
        "completed_tools": ["read", "update"],
    }
    assert harness._completed_update_calls(result) == 1


def test_memory_scenario_rejects_a_started_but_failed_update():
    memory = next(item for item in harness.build_scenarios("session") if item["name"] == "memory")
    failures = memory["check"](
        {
            "tools": ["update"],
            "completed_tools": [],
            "errors": [],
            "answer": "I will remember that.",
            "jargon": [],
        }
    )
    assert "the preference was promised but not written" in failures


def test_ask_counts_only_successful_completed_updates(monkeypatch):
    events = [
        {"devrimo": {"type": "tool_call_started", "tool": "update"}},
        {"devrimo": {"type": "tool_call_error", "tool": "update", "message": "stale"}},
        {"devrimo": {"type": "tool_call_completed", "tool": "update"}},
        {"devrimo": {"type": "tool_call_started", "tool": "update"}},
        {"devrimo": {"type": "tool_call_completed", "tool": "update", "success": True}},
    ]

    class Stream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_lines(self):
            return [f"data: {json.dumps(event)}" for event in events] + ["data: [DONE]"]

    monkeypatch.setattr(harness.httpx, "stream", lambda *_args, **_kwargs: Stream())
    result = harness.ask({"Authorization": "Bearer test"}, "hello")

    assert result["tools"] == ["update", "update"]
    assert result["completed_tools"] == ["update"]
    assert harness._completed_update_calls(result) == 1


def test_non_memory_eval_never_reads_memories(monkeypatch):
    monkeypatch.setattr(harness, "ALLOW_MEMORY_WRITE", True)
    monkeypatch.setattr(harness, "DISPOSABLE", False)
    scenario = {"name": "greeting", "prompt": "hello", "session": None, "check": lambda _result: []}
    monkeypatch.setattr(harness, "build_scenarios", lambda _session: [scenario])
    monkeypatch.setattr(harness, "acquire_token", lambda: "token")
    monkeypatch.setattr(
        harness,
        "ask",
        lambda *_args: {"seconds": 0, "ttft": 0, "tools": [], "errors": [], "answer": "hello", "jargon": []},
    )

    def unexpected_memory_read(_headers):
        raise AssertionError("non-memory eval must not read memories")

    monkeypatch.setattr(harness, "list_memories", unexpected_memory_read)
    monkeypatch.setattr(harness.sys, "argv", ["agent_eval.py"])

    assert harness.main() == 0


def test_unflagged_memory_eval_skips_without_reading_memories(monkeypatch):
    monkeypatch.setattr(harness, "ALLOW_MEMORY_WRITE", False)
    scenario = {"name": "memory", "prompt": "remember", "session": "s", "check": lambda _result: []}
    monkeypatch.setattr(harness, "build_scenarios", lambda _session: [scenario])
    monkeypatch.setattr(harness, "acquire_token", lambda: "token")

    def unexpected_memory_read(_headers):
        raise AssertionError("unflagged memory eval must not read memories")

    monkeypatch.setattr(harness, "list_memories", unexpected_memory_read)
    monkeypatch.setattr(harness.sys, "argv", ["agent_eval.py"])

    assert harness.main() == 0


def test_memory_restoration_is_scoped_and_revision_checked(monkeypatch):
    calls = []

    async def workspace_call(_headers, name, arguments):
        calls.append((name, arguments))
        return {"data": {"revision": 3, "memories": []}}

    monkeypatch.setattr(harness, "_workspace_call", workspace_call)
    monkeypatch.setattr(
        harness,
        "memory_snapshot",
        lambda _headers: {"revision": 3, "memories": []},
    )
    ok, detail = harness.restore_memory_snapshot(
        {},
        {"revision": 1, "memories": []},
        {"revision": 2, "memories": [{"id": "eval", "content": "temporary"}]},
    )

    assert ok is True
    assert "restored" in detail
    assert calls[0][0] == "update"
    assert calls[0][1]["expected_revision"] == 2
    assert calls[0][1]["changes"] == {"memories": []}


def test_memory_restoration_reports_a_concurrent_change(monkeypatch):
    async def workspace_call(_headers, _name, _arguments):
        return {"data": {"revision": 3, "memories": []}}

    monkeypatch.setattr(harness, "_workspace_call", workspace_call)
    monkeypatch.setattr(
        harness,
        "memory_snapshot",
        lambda _headers: {"revision": 4, "memories": [{"id": "other", "content": "new"}]},
    )
    ok, detail = harness.restore_memory_snapshot(
        {},
        {"revision": 1, "memories": []},
        {"revision": 2, "memories": [{"id": "eval", "content": "temporary"}]},
    )

    assert ok is False
    assert "concurrently" in detail


def test_memory_cleanup_refuses_a_newer_revision_before_update(monkeypatch):
    def unexpected_restore(*_args):
        raise AssertionError("must not restore")

    monkeypatch.setattr(
        harness,
        "memory_snapshot",
        lambda _headers: {"revision": 4, "memories": [{"id": "other", "content": "new"}]},
    )
    monkeypatch.setattr(harness, "restore_memory_snapshot", unexpected_restore)

    ok, detail = harness.cleanup_memory_snapshot(
        {},
        {"revision": 1, "memories": []},
        expected_revision=3,
    )

    assert ok is False
    assert "concurrently" in detail

"""The eval harness must never delete a memory it did not write.

The cleanup endpoint clears every memory the account owns, so the harness runs
the memory scenarios only when it has verified an empty (or explicitly
disposable) account, and a failed read of that state is not read as "empty".
"""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "agent_eval_harness", Path(__file__).resolve().parents[1] / "scripts" / "agent_eval.py"
)
harness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harness)


def _set(*, allow: bool, disposable: bool):
    harness.ALLOW_MEMORY_WRITE = allow
    harness.DISPOSABLE = disposable


def test_memory_scenarios_need_the_flags_and_a_verified_account():
    _set(allow=False, disposable=True)
    assert not harness.memory_write_allowed([])
    _set(allow=True, disposable=False)
    assert not harness.memory_write_allowed([])
    # A failed GET must never be read as "the account is empty".
    _set(allow=True, disposable=True)
    assert not harness.memory_write_allowed(None)
    assert harness.memory_write_allowed([])
    assert harness.memory_write_allowed([{"id": "x", "content": "y"}])


def test_the_jargon_check_leaves_ordinary_turkish_alone():
    assert harness.JARGON.search("Kayıt yok, sorgulama tamam") is None
    assert harness.JARGON.search("published release") is not None

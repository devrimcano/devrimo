"""Building campus toolkits: what happens when the filesystem says no.

`connect_toolkits` already treats one unavailable campus server as survivable.
These cover the earlier half of the same path — `build_toolkits`, where a
working directory that cannot be created used to raise and cost the student
their entire agent rather than the one tool.
"""

import stat
from pathlib import Path

from app.agents.toolset import build_toolkits
from app.campus.mcp_config import CampusServerSpec

TIMEOUT = 30


def spec(tool_id: str, cwd: str | None) -> CampusServerSpec:
    return CampusServerSpec(
        tool_id=tool_id,
        command="/opt/mcp/x/.venv/bin/python",
        args=("-m", f"{tool_id}_mcp"),
        env={"METU_PASSWORD": "hunter2"},
        cwd=cwd,
        include_tools=("read",),
    )


def test_allowlist_and_confirmation_policy_reach_mcp_tools(tmp_path):
    rendered = CampusServerSpec(
        tool_id="webmail",
        command="/opt/mcp/webmail/.venv/bin/python",
        args=("-m", "metu_webmail_mcp"),
        cwd=str(tmp_path / "webmail"),
        include_tools=("read_email", "send_email"),
        requires_confirmation_tools=("send_email",),
    )

    toolkit = build_toolkits([rendered], timeout_seconds=TIMEOUT)[0]

    assert toolkit.include_tools == ["read_email", "send_email"]
    assert toolkit.requires_confirmation_tools == ["send_email"]


def test_creates_the_working_directory_private(tmp_path):
    target = tmp_path / "student" / "odtuclass"

    toolkits = build_toolkits([spec("odtuclass", str(target))], timeout_seconds=TIMEOUT)

    assert len(toolkits) == 1
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_server_without_state_dir_needs_no_filesystem(tmp_path):
    toolkits = build_toolkits([spec("sais", None)], timeout_seconds=TIMEOUT)

    assert len(toolkits) == 1
    assert list(tmp_path.iterdir()) == []


def test_unusable_state_dir_skips_that_server_only(tmp_path):
    """The reported failure: CAMPUS_STATE_ROOT unwritable took down the agent.

    One tool losing its cache is a degraded agent; raising here was a missing
    one. The server is skipped rather than launched without a `cwd`, because a
    server that caches a session token relative to its working directory would
    otherwise write it into the broker's own cwd, shared by every student.
    """
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")

    toolkits = build_toolkits(
        [
            spec("odtuclass", str(blocked / "student" / "odtuclass")),
            spec("sais", str(tmp_path / "student" / "sais")),
        ],
        timeout_seconds=TIMEOUT,
    )

    assert [t.name for t in toolkits] == ["campus:sais"]
    assert Path(tmp_path / "student" / "sais").is_dir()

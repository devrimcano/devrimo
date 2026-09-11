"""The fixed cost every model step pays before the student's question is read.

The tool schemas travel with every request the agent makes, and a turn makes
several - a simple factual question measured four model steps, an advisory one
twelve. So a schema that is large is not large once; it is large once per step.

Measured on 2026-09-11, the `update` tool's `changes` parameter was a union of
four fully-expanded Pydantic models - PlanChanges with its whole PlanEntry /
PlanState / PlanMeeting tree inline - at about 17,500 characters, larger than
the other six tools put together and paid on every step of every turn,
including "Hi".

It was dead weight on the common path: the model fills `changes` from the
`application` object a prior `plan` returned, not by constructing it from the
schema, and the server re-validates it against the resource kind regardless. So
the schema guaranteed nothing the server was not already guaranteeing, and cost
thousands of tokens per step to do it.

This test pins the size down, so a future union does not quietly put it back.
"""

import json
from uuid import uuid4

from app.agents.platform_tools import build_platform_tools


def _schema_chars(tool) -> int:
    return len(json.dumps(tool.parameters or {}, ensure_ascii=False))


def test_no_single_tool_schema_dwarfs_the_rest():
    """The update schema used to be larger than the other six combined."""
    tools = {t.name: _schema_chars(t) for t in build_platform_tools(uuid4())}
    update = tools["update"]
    others = sum(size for name, size in tools.items() if name != "update")
    assert update < others, (
        f"the update schema is {update} chars against {others} for the other six - "
        "a parameter union has expanded into the tool schema again"
    )


def test_the_update_schema_stays_within_a_sane_budget():
    """A concrete ceiling, well under the ~17,500 it regressed to once.

    The number is not sacred; it is a tripwire. If a change needs to cross it,
    that is a decision to make on purpose, not by adding a typed union to the
    tool signature and paying for it on every step of every turn.
    """
    tools = {t.name: _schema_chars(t) for t in build_platform_tools(uuid4())}
    assert tools["update"] < 4000, f"update schema is {tools['update']} chars"

"""The announcements the model makes on its way to a tool call.

The instructions say "Never narrate your thinking, search process, or
tool-selection process". The model narrates anyway - one short sentence before
each tool call - and those sentences were streamed as answer content and stayed
there. A real PHYS 213 answer arrived as

    PHYS 213 şubelerini buluyorum — resmi bölüm listesini çekiyorum.PHYS 213
    şubelerini kontrol ediyorum.PHYS 213 şubelerini kontrol ediyorum.PHYS 213
    şubelerini kontrol ediyorum — resmî listeyi alıyorum.**Bu dönem PHYS
    213'ün 2 şubesi var:**

four times over, run together without spaces, in front of the answer.

What made this diagnosable rather than a matter of taste: the stored transcript
never contained them. Reopening the conversation showed the clean answer and
watching it arrive did not, so the stream was demonstrably wrong about its own
run rather than merely noisy. These tests hold the stream to what was stored.
"""

import json

import pytest

from app.assistant.events import _PREAMBLE_CHARACTERS, _serialize_events
from app.observability.turns import TurnObservation


class _Event:
    def __init__(self, event: str, **fields):
        self.event = event
        for key, value in fields.items():
            setattr(self, key, value)


def _content(text: str):
    return _Event("RunContent", content=text)


def _tool_started(name: str = "search"):
    return _Event("ToolCallStarted", tool=_Event("x", tool_name=name))


async def _collect(events):
    async def stream():
        for event in events:
            yield event

    chunks = []
    async for raw in _serialize_events(stream(), "m", "user", TurnObservation.__new__(TurnObservation)):
        text = raw.decode()
        if text.startswith("data: {"):
            chunks.append(json.loads(text.removeprefix("data: ")))
    return chunks


def _answer(chunks) -> str:
    parts = []
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if content:
                parts.append(content)
    return "".join(parts)


def _reasoning(chunks) -> list[str]:
    return [c["devrimo"]["text"] for c in chunks if (c.get("devrimo") or {}).get("type") == "reasoning"]


@pytest.fixture(autouse=True)
def _observation(monkeypatch):
    """The observation object only counts here; its transport is not under test."""
    monkeypatch.setattr(TurnObservation, "tool_started", lambda self, tool: None, raising=False)


async def test_an_announcement_before_a_tool_call_never_reaches_the_answer():
    chunks = await _collect([
        _content("PHYS 213 şubelerini kontrol ediyorum."),
        _tool_started(),
        _content("**Bu dönem PHYS 213'ün 2 şubesi var:** " + "x" * _PREAMBLE_CHARACTERS),
    ])
    answer = _answer(chunks)
    assert "kontrol ediyorum" not in answer
    assert "2 şubesi var" in answer


async def test_the_announcement_is_kept_as_reasoning_rather_than_dropped():
    """It is real progress information; it belongs beside the tool, not lost."""
    chunks = await _collect([
        _content("PHYS 213 şubelerini kontrol ediyorum."),
        _tool_started(),
        _content("y" * (_PREAMBLE_CHARACTERS + 1)),
    ])
    assert _reasoning(chunks) == ["PHYS 213 şubelerini kontrol ediyorum."]


async def test_repeated_announcements_do_not_stack_in_front_of_the_answer():
    """The reported shape: four announcements, then the answer."""
    events = []
    for _ in range(4):
        events.append(_content("PHYS 213 şubelerini kontrol ediyorum."))
        events.append(_tool_started())
    events.append(_content("Bu dönem 2 şube var. " + "z" * _PREAMBLE_CHARACTERS))

    chunks = await _collect(events)
    answer = _answer(chunks)
    assert answer.count("kontrol ediyorum") == 0
    assert answer.startswith("Bu dönem 2 şube var.")
    assert len(_reasoning(chunks)) == 4


async def test_a_short_answer_with_no_tool_call_still_arrives():
    """Nothing follows it to mark it as an announcement, so it is the answer.

    It is held to the end of the stream rather than streamed, which for a reply
    this short is the difference between arriving at once and arriving at once.
    """
    chunks = await _collect([_content("2 eder.")])
    assert _answer(chunks) == "2 eder."
    assert _reasoning(chunks) == []


async def test_a_long_answer_starts_streaming_before_it_is_finished():
    """A student watching a long reply should not wait for all of it.

    Past the threshold the segment is committed and every later fragment goes
    straight out, which is what the stream did for everything before this.
    """
    tail = "!" * 50
    chunks = await _collect([
        _content("a" * (_PREAMBLE_CHARACTERS + 1)),
        _content(tail),
    ])
    deltas = [c for c in chunks if _answer([c])]
    assert len(deltas) >= 2, "the answer was buffered instead of streamed"
    assert _answer(chunks).endswith(tail)

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


def _tool_completed(name: str, result=None):
    return _Event("ToolCallCompleted", tool=_Event("x", tool_name=name, result=result))


def _tool_completed_with_error(name: str, result=None):
    return _Event(
        "ToolCallCompleted",
        tool=_Event("x", tool_name=name, result=result, tool_call_error=True),
    )


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
        _tool_started(),
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
        _tool_started(),
        _content("PHYS 213 şubelerini kontrol ediyorum."),
        _tool_started(),
        _content("y" * (_PREAMBLE_CHARACTERS + 1)),
    ])
    assert _reasoning(chunks) == ["PHYS 213 şubelerini kontrol ediyorum."]


async def test_repeated_announcements_do_not_stack_in_front_of_the_answer():
    """The reported shape: four announcements, then the answer."""
    events = [_tool_started()]
    for _ in range(4):
        events.append(_content("PHYS 213 şubelerini kontrol ediyorum."))
        events.append(_tool_started())
    events.append(_content("Bu dönem 2 şube var. " + "z" * _PREAMBLE_CHARACTERS))

    chunks = await _collect(events)
    answer = _answer(chunks)
    assert answer.count("kontrol ediyorum") == 0
    assert answer.startswith("Bu dönem 2 şube var.")
    assert len(_reasoning(chunks)) == 4


async def test_a_turn_that_calls_no_tool_is_never_held_back():
    """Safe completed sentences still stream before the final sentence."""
    chunks = await _collect([_content("2 eder. "), _content("Ders programı hazır.")])
    assert _answer(chunks) == "2 eder. Ders programı hazır."
    assert _reasoning(chunks) == []
    frames = [c for c in chunks if _answer([c])]
    assert len(frames) == 2, "a toolless reply was buffered instead of streamed"


async def test_the_first_announcement_survives_and_the_rest_do_not():
    """Before the first tool call there is no way to tell the two apart.

    That is the deliberate limit of doing this in the broker: one lead-in
    sentence gets through. Four of them stacked in front of the answer was the
    reported defect, and those are gone.
    """
    events = [_content("PHYS 213 şubelerini buluyorum.")]
    for _ in range(3):
        events.append(_tool_started())
        events.append(_content("PHYS 213 şubelerini kontrol ediyorum."))
    events.append(_tool_started())
    events.append(_content("Bu dönem 2 şube var. " + "z" * _PREAMBLE_CHARACTERS))

    chunks = await _collect(events)
    answer = _answer(chunks)
    assert answer.count("kontrol ediyorum") == 0
    assert answer.startswith("PHYS 213 şubelerini buluyorum.")
    assert len(_reasoning(chunks)) == 3


async def test_a_long_answer_starts_streaming_before_it_is_finished():
    """A student watching a long reply should not wait for all of it.

    Past the threshold the segment is committed and every later fragment goes
    straight out, which is what the stream did for everything before this.
    """
    tail = "!" * 50
    chunks = await _collect([
        _tool_started(),
        _content("a" * (_PREAMBLE_CHARACTERS + 1) + "."),
        _content(tail),
    ])
    deltas = [c for c in chunks if _answer([c])]
    assert len(deltas) >= 2, "the answer was buffered instead of streamed"
    assert _answer(chunks).endswith(tail)


async def test_an_unsupported_turkish_claim_replaces_the_whole_sentence():
    chunks = await _collect([_content("Tercihini "), _content("kaydettim.")])
    assert _answer(chunks) == "Değişiklik doğrulanamadı."


async def test_a_claim_split_at_any_boundary_never_reaches_the_student():
    claim = "kaydettim."
    for split in range(1, len(claim)):
        chunks = await _collect([_content(claim[:split]), _content(claim[split:])])
        answer = _answer(chunks)
        assert "kaydettim" not in answer
        assert answer == "Değişiklik doğrulanamadı."

    third_person = "Tercihini kaydetti."
    for split in range(1, len(third_person)):
        chunks = await _collect([_content(third_person[:split]), _content(third_person[split:])])
        assert _answer(chunks) == "Değişiklik doğrulanamadı."

    delivered = "Email delivered."
    for split in range(1, len(delivered)):
        chunks = await _collect([_content(delivered[:split]), _content(delivered[split:])])
        assert _answer(chunks) == "I could not verify email delivery."

    subject_adverb = "Plan successfully updated."
    for split in range(1, len(subject_adverb)):
        chunks = await _collect([_content(subject_adverb[:split]), _content(subject_adverb[split:])])
        assert _answer(chunks) == "I could not verify the change."

async def test_an_incomplete_claim_prefix_is_preserved_as_ordinary_text():
    assert _answer(await _collect([_content("kay")])) == "kay"


async def test_mutation_claims_are_operation_and_result_specific():
    update_does_not_prove_email = await _collect(
        [_tool_completed("update", "{}"), _content("I sent the email.")]
    )
    assert _answer(update_does_not_prove_email) == "I could not verify email delivery."

    failed_update_does_not_prove_save = await _collect(
        [_tool_completed_with_error("update", "{}"), _content("Kaydettim.")]
    )
    assert _answer(failed_update_does_not_prove_save) == "Değişiklik doğrulanamadı."

    approval_is_not_delivery = await _collect(
        [_tool_completed("send_email", '{"status":"approval_required"}'), _content("I sent the email.")]
    )
    assert _answer(approval_is_not_delivery) == "I could not verify email delivery."

    sent = await _collect(
        [_tool_completed("send_email", '{"status":"sent","message_id":"m-1"}'), _content("I sent the email.")]
    )
    assert _answer(sent) == "I sent the email."

    mixed = await _collect(
        [_tool_completed("update", {"success": True}), _content("I saved it and sent the email.")]
    )
    assert _answer(mixed) == "I saved it and I could not verify email delivery."


async def test_a_claim_waits_for_its_later_successful_completion():
    chunks = await _collect(
        [_content("I sent the email."), _tool_completed("send_email", '{"status":"sent"}')]
    )
    assert _answer(chunks) == "I sent the email."


async def test_tool_completion_exposes_only_a_success_boolean():
    chunks = await _collect(
        [_tool_completed("update", "private result"), _tool_completed_with_error("undo", "private error")]
    )
    completions = [
        chunk["devrimo"]
        for chunk in chunks
        if (chunk.get("devrimo") or {}).get("type") == "tool_call_completed"
    ]
    assert completions == [
        {"type": "tool_call_completed", "tool": "update", "server": None, "success": True},
        {"type": "tool_call_completed", "tool": "undo", "server": None, "success": False},
    ]
    assert all("private" not in str(item) for item in completions)

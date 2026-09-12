"""What a chat turn is allowed to weigh.

These bounds exist because the deployed edge had none: a 20 MB POST to
/api/v1/chat/completions was accepted in full before authentication refused it,
and neither the message nor the thread had a maximum. The point of the tests is
that the ceiling is finite and that ordinary use stays well under it.
"""

import pytest
from pydantic import ValidationError

from app.schemas import (
    MAX_MESSAGE_CHARACTERS,
    MAX_MESSAGES_PER_TURN,
    MAX_TURN_CHARACTERS,
    ChatCompletionsRequestIn,
)


def _turn(*contents: str) -> dict:
    return {"messages": [{"role": "user", "content": content} for content in contents]}


def test_a_single_oversized_message_is_refused():
    with pytest.raises(ValidationError):
        ChatCompletionsRequestIn.model_validate(_turn("A" * (MAX_MESSAGE_CHARACTERS + 1)))


def test_a_message_at_the_limit_is_accepted():
    ChatCompletionsRequestIn.model_validate(_turn("A" * MAX_MESSAGE_CHARACTERS))


def test_too_many_messages_are_refused():
    with pytest.raises(ValidationError):
        ChatCompletionsRequestIn.model_validate(_turn(*(["hi"] * (MAX_MESSAGES_PER_TURN + 1))))


def test_many_legal_messages_can_still_exceed_the_thread_budget():
    """The two caps multiply, and the product is what an attacker would use."""
    each = "A" * MAX_MESSAGE_CHARACTERS
    count = (MAX_TURN_CHARACTERS // MAX_MESSAGE_CHARACTERS) + 1
    assert count <= MAX_MESSAGES_PER_TURN, "this test must stay under the message-count cap"
    with pytest.raises(ValidationError) as caught:
        ChatCompletionsRequestIn.model_validate(_turn(*([each] * count)))
    assert "too long" in str(caught.value)


def test_a_term_of_ordinary_conversation_still_fits():
    """A long thread is resent whole on every turn; it must not become unusable."""
    ChatCompletionsRequestIn.model_validate(_turn(*(["A" * 700] * 400)))

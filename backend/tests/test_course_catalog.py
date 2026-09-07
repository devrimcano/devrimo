"""The pieces the schedule builder trusts to be boring: cache, unwrap, identity.

Each of these replaced something that was quietly wrong rather than loudly
broken — a cache that never released a key, a JSON reader that retyped course
codes, a department lookup that would answer with a row count. The point of
these tests is that the failure mode is *silence*, so nothing downstream is
going to notice a regression for us.
"""

import asyncio

import pytest

from app.campus.course_info import department_code, department_options, json_value
from app.campus.mcp_results import mcp_payload, parse_json_document
from app.core.ttl_cache import TTLCache


class _Result:
    """The shape Agno hands back from an MCP tool call."""

    def __init__(self, content=None, structured=None):
        self.content = content
        self.metadata = {"structured_content": structured} if structured is not None else {}


# --- unwrapping -------------------------------------------------------------


def test_structured_content_wins_over_the_rendered_text():
    """The typed payload is the answer; the text block is a rendering of it."""
    result = _Result(
        content=[{"type": "text", "text": '{"course_code": "wrong"}'}],
        structured={"course_code": "5670201"},
    )
    assert mcp_payload(result) == {"course_code": "5670201"}


def test_single_text_block_is_parsed_as_the_payload():
    result = _Result(content=[{"type": "text", "text": '{"sections": [{"section": "1"}]}'}])
    assert mcp_payload(result) == {"sections": [{"section": "1"}]}


def test_single_key_envelope_is_unwrapped_on_the_structured_path():
    """FastMCP wraps a string-returning tool in {"result": "<document>"}.

    The envelope arrives on the typed metadata path too, and stopping at the
    structured payload is what made the SAIS student profile look empty.
    """
    profile = '{"Program Code / Name": "236/Mathematics", "Program Type": "MAJOR"}'
    result = _Result(structured={"result": profile})
    assert mcp_payload(result) == {"Program Code / Name": "236/Mathematics", "Program Type": "MAJOR"}


def test_nested_structured_envelopes_are_unwrapped():
    result = _Result(structured={"result": {"result": '{"semesters": []}'}})
    assert mcp_payload(result) == {"semesters": []}


def test_a_single_key_envelope_holding_plain_text_is_left_alone():
    result = _Result(structured={"result": "no courses found"})
    assert mcp_payload(result) == {"result": "no courses found"}


def test_several_text_blocks_are_left_alone():
    """Two blocks are a list of results, not one document rendered twice."""
    content = [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
    assert mcp_payload(_Result(content=content)) == content


@pytest.mark.parametrize("text", ["5670201", "01", "3", "null", "true", "NaN", "not json"])
def test_bare_scalars_stay_strings(text):
    """A course code is an identifier, not a number.

    ``json.loads`` on every string turned "5670201" into an int and "01" into
    1 — the section number lost its leading zero on the way to the browser.
    """
    assert parse_json_document(text) == text


def test_documents_are_still_parsed():
    assert parse_json_document('  {"a": 1}  ') == {"a": 1}
    assert parse_json_document("[1, 2]") == [1, 2]
    # Malformed is returned as-is rather than raising.
    assert parse_json_document("{oops") == "{oops"


def test_json_value_keeps_identifiers_intact_through_nesting():
    payload = {"courses": [{"course_code": "5670201", "section": "01", "credit": "3"}]}
    assert json_value(payload) == payload


# --- department identity ----------------------------------------------------


def test_reads_only_a_labelled_three_digit_code():
    payload = {"departments": [{"code": "567", "name": "Electrical and Electronics Engineering"}]}
    assert department_code(payload, "Electrical and Electronics Engineering") == "567"


def test_ignores_three_digit_values_that_are_not_codes():
    """A student id, a year or a row count is not a department.

    Scanning a payload for any ``\\b\\d{3}\\b`` is how the browser used to pick
    one, and a wrong department returns a completely plausible course list.
    """
    payload = {"total": 236, "year": 2026, "student_id": "2468101", "results": []}
    assert department_code(payload, "Mathematics") is None


def test_matches_the_named_department_among_several():
    payload = [
        {"code": "571", "name": "Computer Engineering"},
        {"code": "567", "name": "Electrical and Electronics Engineering"},
    ]
    assert department_code(payload, "Computer Engineering") == "571"


def test_an_abbreviation_prefixing_the_name_resolves():
    payload = [{"department_code": "236", "department_name": "MATH - Mathematics"}]
    assert department_code(payload, "MATH") == "236"


def test_ambiguity_is_reported_not_guessed():
    """Two plausible departments must not silently become one of them."""
    payload = [
        {"code": "236", "name": "Mathematics"},
        {"code": "345", "name": "Mathematics Education"},
    ]
    assert department_code(payload, "Mathe") is None


def test_a_lone_candidate_is_taken_even_without_a_name_match():
    """``search_departments`` already did the matching; one hit is the answer."""
    payload = {"departments": [{"code": "572", "name": "Aerospace Engineering"}]}
    assert department_code(payload, "AEE") == "572"


# --- cache ------------------------------------------------------------------


def test_expired_entries_are_reclaimed_without_being_read_again():
    """The leak: a plain dict only drops a key when that same key is re-read."""
    cache = TTLCache(ttl_seconds=-1, max_entries=100)
    cache.set(("a",), 1)
    cache.set(("b",), 2)
    assert cache.get(("b",)) is None
    # Writing anything prunes what has aged out, including keys nobody asks for.
    assert len(cache._entries) <= 1


def test_entries_are_bounded_by_count():
    cache = TTLCache(ttl_seconds=60, max_entries=3)
    for index in range(10):
        cache.set((str(index),), index)
    assert len(cache._entries) == 3
    assert cache.get(("9",)) == 9
    assert cache.get(("0",)) is None


def test_purge_forgets_one_users_entries_only():
    cache = TTLCache(ttl_seconds=60, max_entries=100)
    cache.set(("alice", "courses"), ["a"])
    cache.set(("bob", "courses"), ["b"])
    cache.purge(lambda key: key[0] == "alice")
    assert cache.get(("alice", "courses")) is None
    assert cache.get(("bob", "courses")) == ["b"]


def test_concurrent_misses_call_the_upstream_once():
    """Four page mounts must not spawn four SAIS sessions."""
    cache = TTLCache(ttl_seconds=60, max_entries=10)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "value"

    async def scenario():
        results = await asyncio.gather(*(cache.run(("k",), factory) for _ in range(4)))
        assert results == ["value"] * 4
        # A later caller reads the stored value rather than refilling.
        assert await cache.run(("k",), factory) == "value"

    asyncio.run(scenario())
    assert calls == 1


def test_a_failed_fill_is_not_cached_and_does_not_wedge_the_key():
    cache = TTLCache(ttl_seconds=60, max_entries=10)
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("upstream down")
        return "recovered"

    async def scenario():
        with pytest.raises(RuntimeError):
            await cache.run(("k",), flaky)
        assert await cache.run(("k",), flaky) == "recovered"

    asyncio.run(scenario())
    assert attempts == 2


def test_a_caller_giving_up_does_not_cancel_the_shared_fill():
    """A client that disconnects must not take the answer away from the others."""
    cache = TTLCache(ttl_seconds=60, max_entries=10)

    async def slow():
        await asyncio.sleep(0.05)
        return "value"

    async def scenario():
        first = asyncio.ensure_future(cache.run(("k",), slow))
        second = asyncio.ensure_future(cache.run(("k",), slow))
        await asyncio.sleep(0)
        first.cancel()
        assert await second == "value"

    asyncio.run(scenario())


# --- the department picker's options ----------------------------------------


def test_department_options_only_offer_explicitly_labelled_codes():
    """The picker must not be able to offer a student id as a department.

    It goes through the same extraction ``department_code`` uses, so a payload
    carrying a seven-digit student number and a row count yields neither.
    """
    payload = {
        "student_no": "2512345",
        "row_count": "412",
        "departments": [
            {"department_code": "567", "department_name": "Computer Engineering"},
            {"department_code": "571", "department_name": "Mathematics"},
        ],
    }
    assert department_options(payload) == [
        {"code": "567", "name": "Computer Engineering"},
        {"code": "571", "name": "Mathematics"},
    ]


def test_department_options_fall_back_to_the_code_as_its_own_label():
    """An unnamed department is still selectable — it just shows its code."""
    assert department_options({"code": "580"}) == [{"code": "580", "name": "580"}]


def test_department_options_are_empty_when_nothing_is_labelled():
    assert department_options({"results": ["Computer Engineering", "Mathematics"]}) == []

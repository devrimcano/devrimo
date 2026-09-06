"""The SAIS student card: unwrapping it, reading it, and storing it.

Every bug this file guards against shipped to production at least once. The
card arrives double-wrapped, labelled in Turkish, and with the degree level
written as prose, and each of those broke a different thing: an empty
department, a surname prefix taken from the given name, and a settings form
that returned 422 on save. The last test exists because a refactor once
deleted ``_apply_student_info`` and left two calls to it — nothing failed
until a student pressed the button.
"""

import asyncio
import json

import pytest

from app.planning.mcp_bridge import (
    _apply_student_info,
    _course_rows,
    _payload,
    _degree_level,
    _find_value,
    _program_code,
    _program_code_and_department,
    _schedule_matches_term,
    _schedule_term,
    _surname_prefix,
    _unwrap_result,
    _year_of_study,
)

async def _no_sleep(_seconds):
    """Retries are tested for their behaviour, not their pacing."""


CARD = {
    "Adı": "Erkin Emre",
    "Soyadı": "Taş",
    "Bölüm": "Bilgisayar Mühendisliği",
    "Öğrenim Düzeyi": "Lisans",
    "Kampüs": "Ankara",
}

# The English card, exactly as live SAIS returns it. It has no department
# field: the department is the right-hand half of "Program Code / Name", and
# "Program Type" sits beside it as a decoy for any loose "program" match.
ENGLISH_CARD = {
    "First Name": "Erkin Emre",
    "Last Name": "Taş",
    "Program Code / Name": "567/Electrical and Electronics Engineering",
    "Program Type": "MAJOR",
    "Education Level / Semester No": "Bachelor's (2nd year) / 4",
    "Standing": "SATISFACTORY",
}


def test_unwraps_a_json_document_held_as_a_string():
    """What SAIS actually returns: a document stringified under one key."""
    payload = _unwrap_result({"result": json.dumps(CARD, ensure_ascii=False)})
    assert _find_value(payload, {"department", "bölüm"}) == "Bilgisayar Mühendisliği"


def test_leaves_a_record_that_merely_contains_a_wrapper_key_alone():
    record = {"result": "PASSED", "course": "PHYS213"}
    assert _unwrap_result(record) == record


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Taş", "TA"),
        # The label is "Adı Soyadı" as often as "Soyadı". Reading the whole
        # field would store "ER" and answer every surname range wrong.
        ("Erkin Emre Taş", "TA"),
        ("işık", "İŞ"),  # Turkish uppercase: "i" is "İ", never "I".
        ("", None),
        (None, None),
    ],
)
def test_surname_prefix_is_two_letters_of_the_last_name(value, expected):
    assert _surname_prefix(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Lisans", "undergraduate"),
        # "yüksek lisans" contains "lisans", so order of interpretation matters.
        ("Yüksek Lisans", "masters"),
        ("Doktora", "doctoral"),
        ("Ön Lisans", "other"),
        ("Değişim Öğrencisi", "exchange"),
        ("Undergraduate", "undergraduate"),
        # Unrecognised is dropped, never stored: a value outside the API's
        # vocabulary displays as blank and breaks the student's next save.
        ("Özel Öğrenci", None),
        ("", None),
    ],
)
def test_degree_level_maps_onto_the_accepted_vocabulary(value, expected):
    assert _degree_level(value) == expected


def test_program_code_drops_a_name_too_long_to_be_a_code():
    assert _program_code("571") == "571"
    assert _program_code("Bilgisayar Mühendisliği Lisans Programı") is None


async def test_apply_student_info_stores_every_field_it_could_read(monkeypatch):
    """The regression test for the deleted helper: this call must resolve."""
    captured = {}

    async def fake_apply(db, user_id, **fields):
        captured.update(fields)

    monkeypatch.setattr("app.planning.mcp_bridge.apply_verified_context", fake_apply)
    await _apply_student_info(None, "user", _unwrap_result({"result": json.dumps(CARD, ensure_ascii=False)}))

    assert captured == {
        "department": "Bilgisayar Mühendisliği",
        "degree_level": "undergraduate",
        "program_code": None,
        "campus": "Ankara",
        "surname_prefix": "TA",
        # "Lisans" alone carries no year and no semester number, so there is
        # nothing to read and nothing is invented.
        "year_of_study": None,
    }


async def test_a_card_missing_fields_passes_none_rather_than_inventing_them(monkeypatch):
    captured = {}

    async def fake_apply(db, user_id, **fields):
        captured.update(fields)

    monkeypatch.setattr("app.planning.mcp_bridge.apply_verified_context", fake_apply)
    await _apply_student_info(None, "user", {"Öğrenci No": "e272479"})
    assert set(captured.values()) == {None}


class _Function:
    """A stand-in for a connected MCP function that fails a set number of times."""

    def __init__(self, failures: int, result: object = None):
        self.failures = failures
        self.result = result
        self.calls = 0

    async def entrypoint(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("The read operation timed out")
        return json.dumps(self.result, ensure_ascii=False)


async def test_a_slow_read_is_retried_rather_than_lost(monkeypatch):
    """METU answers in a second when warm and sometimes exceeds the campus
    server's thirty-second ceiling. One timeout must not cost the department."""
    monkeypatch.setattr("app.planning.mcp_bridge.asyncio.sleep", _no_sleep)
    function = _Function(failures=1, result=CARD)
    payload = await _payload(function, tool="sais_get_student_info", attempts=2)
    assert function.calls == 2
    assert _find_value(payload, {"department", "bölüm"}) == "Bilgisayar Mühendisliği"


async def test_retries_are_bounded(monkeypatch):
    monkeypatch.setattr("app.planning.mcp_bridge.asyncio.sleep", _no_sleep)
    function = _Function(failures=99)
    assert await _payload(function, tool="sais_get_student_info", attempts=2) is None
    assert function.calls == 2


async def test_a_single_attempt_is_not_retried(monkeypatch):
    monkeypatch.setattr("app.planning.mcp_bridge.asyncio.sleep", _no_sleep)
    function = _Function(failures=99)
    assert await _payload(function, tool="sais_get_transcript") is None
    assert function.calls == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("567/Electrical and Electronics Engineering", ("567", "Electrical and Electronics Engineering")),
        ("567 / Electrical and Electronics Engineering", ("567", "Electrical and Electronics Engineering")),
        ("5710101/Computer Engineering", ("5710101", "Computer Engineering")),
        # No code on the left, so the whole field is the name — never a code.
        ("Electrical and Electronics Engineering", (None, "Electrical and Electronics Engineering")),
        ("MAJOR", (None, "MAJOR")),
        # Nothing but a code, and no name to go with it. Read as a name it
        # would be stored as the student's department.
        ("571", ("571", None)),
        ("5710101", ("5710101", None)),
        ("", (None, None)),
    ],
)
def test_the_combined_programme_field_splits_into_code_and_name(value, expected):
    assert _program_code_and_department(value) == expected


async def test_the_live_english_card_yields_a_department_and_a_code(monkeypatch):
    """The regression test for the empty department: this exact card shape was
    stored with department NULL for every student on the site."""
    captured = {}

    async def fake_apply(db, user_id, **fields):
        captured.update(fields)

    monkeypatch.setattr("app.planning.mcp_bridge.apply_verified_context", fake_apply)
    await _apply_student_info(None, "user", ENGLISH_CARD)

    assert captured["department"] == "Electrical and Electronics Engineering"
    assert captured["program_code"] == "567"
    assert captured["degree_level"] == "undergraduate"
    assert captured["surname_prefix"] == "TA"


async def test_program_type_is_never_mistaken_for_the_programme(monkeypatch):
    """"Program Type" is listed beside the real field and its value is "MAJOR"."""
    captured = {}

    async def fake_apply(db, user_id, **fields):
        captured.update(fields)

    monkeypatch.setattr("app.planning.mcp_bridge.apply_verified_context", fake_apply)
    # Decoy first, so a loose match would bind to it.
    await _apply_student_info(None, "user", {"Program Type": "MAJOR", **ENGLISH_CARD})
    assert captured["department"] == "Electrical and Electronics Engineering"


async def test_a_plainly_labelled_programme_field_is_read(monkeypatch):
    """Some cards label it "program" and nothing more.

    That label is a prefix of the "Program Type" decoy, so it is matched
    exactly and never as a substring. The decoy is listed first here, where a
    loose match would reach it.
    """
    captured = {}

    async def fake_apply(db, user_id, **fields):
        captured.update(fields)

    monkeypatch.setattr("app.planning.mcp_bridge.apply_verified_context", fake_apply)
    await _apply_student_info(
        None,
        "user",
        {"Program Type": "MAJOR", "bolum": "Computer Engineering", "program": "571"},
    )

    assert captured["program_code"] == "571"
    assert captured["department"] == "Computer Engineering"


class _SlowFunction:
    """A connected function that never answers, to test the deadline."""

    def __init__(self):
        self.cancelled = False

    async def entrypoint(self):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "{}"


async def test_an_optional_read_is_abandoned_at_its_deadline():
    """METU takes longer to render the weekly schedule than it is worth
    waiting for, and the student is holding a spinner the whole time."""
    function = _SlowFunction()
    started = asyncio.get_running_loop().time()
    assert await _payload(function, tool="sais_get_schedule", timeout=0.05) is None
    assert asyncio.get_running_loop().time() - started < 5
    assert function.cancelled


async def test_a_read_with_no_deadline_is_never_abandoned(monkeypatch):
    """The agent shares one pooled campus session across a turn; cancelling a
    call on it would leave the next read talking to a busy server."""
    function = _Function(failures=0, result=CARD)
    assert await _payload(function, tool="sais_get_student_info") is not None
    assert function.calls == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Bachelor's (2nd year) / 4", 2),
        ("Doctorate (1st year) / 2", 1),
        ("Master's (3rd year) / 6", 3),
        # No year in the prose, so the semester decides: two to a year.
        ("Lisans / 7", 4),
        ("Lisans / 1", 1),
        # Turkish wording for the year itself.
        ("Lisans (3. sinif) / 5", 3),
        # Nothing recognisable: the comparator skips a dimension it has no
        # value for, which is cheaper than checking against a guess.
        ("Lisans", None),
        ("", None),
        (None, None),
    ],
)
def test_year_of_study_is_read_from_the_card(value, expected):
    assert _year_of_study(value) == expected


async def test_the_live_card_yields_a_year(monkeypatch):
    captured = {}

    async def fake_apply(db, user_id, **fields):
        captured.update(fields)

    monkeypatch.setattr("app.planning.mcp_bridge.apply_verified_context", fake_apply)
    await _apply_student_info(None, "user", ENGLISH_CARD)
    assert captured["year_of_study"] == 2


# One slot of the weekly schedule, exactly as live SAIS returns it. The code,
# section and room share a single cell and none of the transcript's keys appear.
SCHEDULE_PAYLOAD = {
    "semester": "2025-2026 Spring - MAJOR - Bachelor's (2nd year)",
    "semester_code": "20252|1",
    "total_slots": 2,
    "schedule": [
        {
            "day": "Monday",
            "hour": "8:40",
            "code_section_room": "2360130 - 1 - P1",
            "course_name": "MULTIVARIABLE AND VECTOR CALCULUS",
            "instructor": "(Prof.Dr.) MEHMET ZAFER NURLU",
            "classroom": "FİZİK BÖLÜMÜ BİNASI P1",
        },
        {
            "day": "Wednesday",
            "hour": "10:40",
            "code_section_room": "2300213 - 2 - FZ-25",
            "course_name": "PHYSICS III",
            "instructor": "STAFF",
            "classroom": "FZ-25",
        },
    ],
}


def test_the_weekly_schedule_is_read_at_all():
    """It was fetched successfully and then dropped: none of the transcript's
    key names appear on a schedule row, so every enrolled course was lost."""
    rows = _course_rows(SCHEDULE_PAYLOAD)
    assert {row["course_code"] for row in rows} == {"2360130", "2300213"}


def test_the_combined_cell_is_split_into_its_parts():
    rows = {row["course_code"]: row for row in _course_rows(SCHEDULE_PAYLOAD)}
    assert rows["2360130"]["section"] == "1"
    assert rows["2360130"]["name"] == "MULTIVARIABLE AND VECTOR CALCULUS"
    assert rows["2360130"]["meetings"] == [{"day": "Monday", "hour": "8:40", "room": "FİZİK BÖLÜMÜ BİNASI P1"}]
    # A room code may itself contain a hyphen, so the split stops after the
    # section rather than on every separator.
    assert rows["2300213"]["section"] == "2"
    assert rows["2300213"]["meetings"][0]["room"] == "FZ-25"


def test_every_weekly_meeting_of_one_course_is_kept():
    """A course meets several times a week and each is its own schedule row.
    Collapsing them to the last one misreported when the course actually is."""
    payload = {"schedule": [
        {"day": "Monday", "hour": "8:40", "code_section_room": "2360130 - 1 - P1", "course_name": "CALCULUS"},
        {"day": "Monday", "hour": "9:40", "code_section_room": "2360130 - 1 - P1", "course_name": "CALCULUS"},
        {"day": "Wednesday", "hour": "11:40", "code_section_room": "2360130 - 1 - P1", "course_name": "CALCULUS"},
    ]}
    rows = _course_rows(payload)
    assert len(rows) == 1
    assert [(m["day"], m["hour"]) for m in rows[0]["meetings"]] == [
        ("Monday", "8:40"), ("Monday", "9:40"), ("Wednesday", "11:40"),
    ]


def test_a_transcript_row_still_parses_the_old_way():
    """The transcript shape must not regress while teaching it a second one."""
    rows = _course_rows({"course_code": "PHYS106", "grade": "BB", "credits": 4})
    assert rows == [{"course_code": "PHYS106", "grade": "BB", "credits": 4}]


def test_a_schedule_reports_its_own_term():
    """The code field is read before the prose one, which carries no code."""
    assert _schedule_term(SCHEDULE_PAYLOAD) == "20252"
    assert _schedule_term({"semester": "2025-2026 Spring"}) is None
    assert _schedule_term({}) is None


def test_a_schedule_from_another_term_is_not_this_term_s_enrolment():
    """SAIS answers with whatever it last registered the student for rather
    than refusing a term it has nothing for. Storing that reported three
    enrolled courses for a term they had not registered for at all."""
    assert not _schedule_matches_term(SCHEDULE_PAYLOAD, "20261")
    assert _schedule_matches_term(SCHEDULE_PAYLOAD, "20252")
    # No opinion either way keeps the schedule: this guards a term mismatch,
    # not an unfamiliar payload.
    assert _schedule_matches_term({"schedule": []}, "20261")
    assert _schedule_matches_term(SCHEDULE_PAYLOAD, "2025-2026 Fall")

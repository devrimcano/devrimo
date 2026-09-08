"""The vendored SAIS client's held session, and the lock that makes it safe.

Reaching an app through the SAIS portal costs three requests before the real
one, and that preamble used to run on every single tool call — two thirds of all
traffic to METU was re-establishing a session the process already had. Holding
it is the largest single saving available on this path.

It is also the most dangerous, because the failure mode is silent: SAIS answers
a request carrying a stale token with the *previous* page rather than an error,
so a mistake here surfaces as a plausible-looking wrong answer — in the planner,
as one section's eligibility table shown for another section. Everything below
tests a way that could happen.

The module is loaded from ``vendor_patches`` by path: it ships inside the Course
Info image rather than as part of this application, so it has no import name
here. ``config`` is upstream's and is not vendored, so it is stubbed.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

VENDOR = Path(__file__).resolve().parents[1] / "vendor_patches" / "course_info"


@pytest.fixture(scope="module")
def sais():
    package = types.ModuleType("_vendored_course_info")
    package.__path__ = [str(VENDOR)]
    sys.modules["_vendored_course_info"] = package

    config = types.ModuleType("_vendored_course_info.config")
    config.settings = types.SimpleNamespace(sais_username="u", sais_password="p", locale="tr")
    sys.modules["_vendored_course_info.config"] = config

    for name in ("models", "sais_client"):
        spec = importlib.util.spec_from_file_location(
            f"_vendored_course_info.{name}", VENDOR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

    yield sys.modules["_vendored_course_info.sais_client"]

    for name in [key for key in sys.modules if key.startswith("_vendored_course_info")]:
        del sys.modules[name]


def _client(sais):
    client = sais.SAISClient(username="u", password="p", locale="tr")
    client._token = "token"  # never authenticate against METU from a test
    return client


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


ENTRY = (
    '<html><body><form action="main.php">'
    '<input name="hidden_creds" value="CREDS">'
    '<input name="hidden_redir" value="Login">'
    "</form></body></html>"
)
COURSE_LIST = "<html><body><table><tr><th>Code</th><th>Name</th></tr></table></body></html>"
AUTOLOGIN = '<html><body><form id="autologin"><input name="x" value="y"></form></body></html>'
EMPTY_PAGE = "<html><body><p>No records found.</p></body></html>"


class _Response:
    def __init__(self, html: str):
        self.content = html.encode("utf-8")
        self.url = "https://example.invalid/main.php"

    def raise_for_status(self):
        return None


def _track_opens(client, sais):
    opens: list[int] = []

    async def open_session(app_code):
        opens.append(app_code)
        return (f"https://example.invalid/app{app_code}/main.php", ENTRY, _soup(ENTRY))

    client._open_app_proxy_session = open_session
    return opens


# --- holding the session ------------------------------------------------------


async def test_the_preamble_runs_once_for_repeated_reads_of_one_app(sais):
    """The whole point: eight sections of one course share one session."""
    client = _client(sais)
    opens = _track_opens(client, sais)

    for _ in range(5):
        await client._get_app_proxy_session(64)

    assert opens == [64]


async def test_reaching_another_app_gives_up_the_held_one(sais):
    """The hazard this design exists to avoid.

    Course listings are app 64 and the student's own curriculum is app 178, and
    they share one cookie jar. The server keeps exactly one current session, so
    a cached app-64 page replayed after a category read would be posted into a
    session that has moved — and SAIS answers that with the previous page, not
    an error. Modelling it as a single slot makes replaying it impossible.
    """
    client = _client(sais)
    opens = _track_opens(client, sais)

    await client._get_app_proxy_session(64)
    await client._get_app_proxy_session(178)
    await client._get_app_proxy_session(64)

    assert opens == [64, 178, 64]


async def test_an_expired_session_is_reopened(sais):
    client = _client(sais)
    opens = _track_opens(client, sais)

    await client._get_app_proxy_session(64)
    app, opened_at, payload = client._session
    client._session = (app, opened_at - sais.SESSION_TTL_SECONDS - 1, payload)
    await client._get_app_proxy_session(64)

    assert opens == [64, 64]


async def test_the_cache_can_be_switched_off_without_a_rebuild(sais, monkeypatch):
    """A rollback has to be a broker config change, not an image build at 03:00."""
    monkeypatch.setattr(sais, "SESSION_CACHE_ENABLED", False)
    client = _client(sais)
    opens = _track_opens(client, sais)

    await client._get_app_proxy_session(64)
    await client._get_app_proxy_session(64)

    assert opens == [64, 64]


# --- noticing that the session went away --------------------------------------


def _post_returning(client, pages: list[str]) -> list[str]:
    """Answer each POST with the next page, recording what was asked."""
    sent: list[str] = []

    async def post(url, data=None, headers=None):
        sent.append(url)
        return _Response(pages[min(len(sent) - 1, len(pages) - 1)])

    client._client = types.SimpleNamespace(post=post)
    return sent


async def test_a_login_page_costs_exactly_one_retry(sais):
    """Not a loop: a second failure is a real failure and belongs to the caller."""
    client = _client(sais)
    opens = _track_opens(client, sais)
    _post_returning(client, [AUTOLOGIN, COURSE_LIST])

    _, html, _ = await client._submit_course_list_page("236", "20252")

    assert opens == [64, 64], "the held session is dropped and reopened once"
    assert "table" in html


async def test_a_session_that_is_gone_for_good_does_not_retry_for_ever(sais):
    client = _client(sais)
    opens = _track_opens(client, sais)
    _post_returning(client, [AUTOLOGIN])

    await client._submit_course_list_page("236", "20252")

    assert opens == [64, 64], "one retry, then the answer is handed back as it is"


async def test_an_empty_department_is_an_answer_not_a_lost_session(sais):
    """The trap in the other direction.

    A department with nothing scheduled parses to zero rows, and treating that
    as a lost session would retry every genuinely empty page for ever. Only
    positive evidence — the portal's own login form — counts.
    """
    client = _client(sais)
    opens = _track_opens(client, sais)
    _post_returning(client, [EMPTY_PAGE])

    await client._submit_course_list_page("236", "20252")

    assert opens == [64], "no retry for a page that simply had nothing in it"


# --- one call at a time -------------------------------------------------------


def test_every_public_call_is_serialised(sais):
    """Applied by default, so a method from a future upstream release is covered."""
    for name in (
        "list_program_courses",
        "get_course_info",
        "get_section_constraints",
        "get_departments_and_semesters",
        "get_student_categories_overview",
        "get_student_category_courses",
    ):
        assert hasattr(getattr(sais.SAISClient, name), "__wrapped__"), name


def test_authenticate_is_not_serialised(sais):
    """It is called from inside the locked path; wrapping it would deadlock."""
    assert not hasattr(sais.SAISClient.authenticate, "__wrapped__")
    assert not hasattr(sais.SAISClient.aclose, "__wrapped__")


async def test_two_calls_on_one_client_never_overlap(sais):
    """The silent-corruption case, driven directly."""
    client = _client(sais)
    depth = [0]
    peak = [0]

    async def open_session(app_code):
        depth[0] += 1
        peak[0] = max(peak[0], depth[0])
        try:
            await asyncio.sleep(0)
            return (f"https://example.invalid/app{app_code}", ENTRY, _soup(ENTRY))
        finally:
            depth[0] -= 1

    client._open_app_proxy_session = open_session
    client._client = types.SimpleNamespace(post=lambda *a, **k: _answer(EMPTY_PAGE))

    await asyncio.gather(
        client.get_departments_and_semesters(),
        client.get_departments_and_semesters(),
        client.get_departments_and_semesters(),
    )
    assert peak == [1]


async def _answer(html: str) -> _Response:
    return _Response(html)


# --- the request count, which is the whole claim ------------------------------


async def test_each_call_reports_how_many_requests_it_made(sais, capsys):
    """Holding the session is worth a request count and nothing else.

    So the count has to be observable from outside, or the saving is a claim
    rather than a measurement.
    """
    client = _client(sais)
    _track_opens(client, sais)
    _post_returning(client, [EMPTY_PAGE])

    await client.get_departments_and_semesters()

    line = capsys.readouterr().err
    assert "sais_call" in line
    assert "tool=get_departments_and_semesters" in line
    assert "requests=" in line and "ms=" in line


async def test_the_report_never_carries_a_url(sais, capsys):
    """`pkg` and `hidden_creds` are session tokens, and this line is shared.

    Asserted on what is emitted rather than on the source, because the source
    also contains the sentence explaining why there is no URL in it.
    """
    client = _client(sais)
    _track_opens(client, sais)
    _post_returning(client, [EMPTY_PAGE])

    await client.get_departments_and_semesters()

    line = capsys.readouterr().err
    assert "sais_call" in line
    for secret in ("http", "pkg=", "hidden_creds", "CREDS", "token"):
        assert secret not in line, f"{secret!r} must never reach a shared log"


def _curriculum_box(number, checked, code="2300213", grade=""):
    mark = '<img src="img/check.gif">' if checked else ''
    assigned = f'<div id="0000000|1|1|{code}|1|25|{grade}|PHYS 213|0">PHYS 213 MUST COURSE {grade}</div>' if grade else ''
    return f'''<div class="box-table-curriculum">
      <div class="box-table-head-curriculum"><div class="box-column-label-curriculum">{number}.SEMESTER</div>{mark}</div>
      <div class="box-row-curriculum"><div class="box-column-label-curriculum">PHYS 213</div>
      <div class="box-column-value-curriculum"><div id="0000000|1|1|{number}|{code}|1|PHYS 213">{assigned}</div></div></div>
    </div>'''

def test_real_curriculum_dom_checkmarks_and_grade_cells(sais):
    html = '<div id="curriculum">' + _curriculum_box(1, True, grade="BA") + _curriculum_box(3, False) + '</div>'
    result = sais.parse_student_curriculum(html)
    assert result["semesters"][0]["completed"] is True
    assert result["semesters"][0]["courses"][0]["grade"] == "BA"
    assert result["semesters"][1]["completed"] is False
    assert result["semesters"][1]["courses"][0]["grade"] == ""
    assert "0000000" not in str(result)


def test_curriculum_accepts_assigned_course_text_without_a_grade(sais):
    html = '''<div id="curriculum"><div class="box-table-curriculum">
      <div class="box-table-head-curriculum"><div class="box-column-label-curriculum">3.SEMESTER</div></div>
      <div class="box-row-curriculum"><div class="box-column-label-curriculum">PHYS 213</div>
      <div class="box-column-value-curriculum">
        <div id="0000000|1|1|3|2300213|1|PHYS 213">
          <div id="0000000|1|1|2300213|1|25||PHYS 213|0">PHYS 213 MUST COURSE</div>
        </div>
      </div></div>
    </div></div>'''

    result = sais.parse_student_curriculum(html)

    assert result["semesters"][0]["courses"] == [
        {"course_code": "2300213", "course_name": "PHYS 213", "grade": ""}
    ]


async def test_curriculum_retries_one_stalled_result_page_within_the_tool_budget(sais):
    client = _client(sais)
    landing = _soup('''<form action="curriculum.php">
      <select name="text_semester_programtype"><option selected value="20261|1">Main</option></select>
      <input type="hidden" name="token" value="opaque">
    </form>''')

    async def open_session(_app_code):
        return "https://example.invalid/main.php", str(landing), landing

    attempts = []

    async def post(url, data=None, timeout=None):
        attempts.append((url, timeout))
        if len(attempts) == 1:
            request = sais.httpx.Request("POST", url)
            raise sais.httpx.ReadTimeout("stalled", request=request)
        html = '<div id="curriculum">' + _curriculum_box(3, False) + '</div>'
        return _Response(html)

    client._open_app_proxy_session = open_session
    client._client = types.SimpleNamespace(post=post)

    result = await client.get_student_curriculum()

    assert len(attempts) == 2
    assert [timeout for _, timeout in attempts] == [12.0, 12.0]
    assert result["semesters"][0]["courses"][0]["course_code"] == "2300213"

def test_curriculum_parser_rejects_missing_board(sais):
    with pytest.raises(ValueError):
        sais.parse_student_curriculum('<form id="autologin"></form>')

def test_curriculum_parser_does_not_read_other_tabs(sais):
    html = '<div id="studentTranscript">' + _curriculum_box(1, False) + '</div>'
    html += '<div id="curriculum">' + _curriculum_box(3, False) + '</div>'
    assert [s["semester"] for s in sais.parse_student_curriculum(html)["semesters"]] == [3]

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
import time
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


def _course_identity(course="5670201", semester="20261", section=None):
    html = f'<input type="hidden" name="text_course_code" value="{course}">'
    html += f'<input type="hidden" name="select_semester" value="{semester}">'
    if section is not None:
        html += f'<input type="hidden" name="section" value="{section}">'
    return html


def _course_rule_identity(course="5670201", semester="20261"):
    return (
        f'<p>Semester: {semester}</p>'
        f'<input type="radio" name="text_course_code" value="{course}">'
    )


def test_response_term_label_uses_the_official_selector_label(sais):
    page = _soup("<p>Course Code: 5670201 Semester: Official Fall Term Course Name: Fixture</p>")
    sais._verify_course_response_identity(page, "5670201", "20261", semester_label="Official Fall Term")
    with pytest.raises(ValueError, match="identity"):
        sais._verify_course_response_identity(page, "5670201", "20261", semester_label="Official Spring Term")
    wrong_label = _soup(_course_identity() + "<p>Semester: Official Spring Term</p>")
    with pytest.raises(ValueError, match="identity"):
        sais._verify_course_response_identity(wrong_label, "5670201", "20261", semester_label="Official Fall Term")


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


# --- reusing the held position ------------------------------------------------


def _position_client(sais, nav_calls: list, pages: list[str]):
    """A client whose navigation is faked but still records the position.

    The real ``_submit_course_list_page`` records where the session stands; the
    fake has to do the same or the elision has nothing to reuse.
    """
    client = _client(sais)
    client._nav_elision = True

    async def course_list(department, semester):
        nav_calls.append((department, semester))
        client._note_position("https://example.invalid/main.php", semester)
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    sent = _post_returning(client, pages)
    return client, sent


def _constraint_table(start: str, end: str) -> str:
    return (
        "<table><tr><th>Dept</th><th>Char</th><th>Char</th><th>CGPA</th><th>CGPA</th>"
        "<th>Year</th><th>Year</th><th>Grade</th><th>Grade</th></tr>"
        f"<tr><td>CENG</td><td>{start}</td><td>{end}</td><td>0.00</td><td>4.00</td>"
        "<td>0</td><td>95</td><td>G</td><td>G</td></tr></table>"
    )


def _course_page_html(course: str = "5670201", semester: str = "20261") -> str:
    return _course_identity(course=course, semester=semester) + (
        '<form><input type="hidden" name="hidden_redir" value="Course_Info"></form>'
        "<table><tr><td>Course Name: FIXTURE COURSE Credit: 3</td></tr></table>"
        "<table><tr><th>Section</th><th>Instructor</th></tr></table>"
    )


async def test_course_info_reuses_the_held_position(sais):
    nav: list = []
    client, sent = _position_client(sais, nav, [
        _course_page_html(),
        _course_page_html(course="5670202"),
    ])

    first = await client.get_course_info("567", "20261", "5670201")
    second = await client.get_course_info("567", "20261", "5670202")

    assert (first.course_code, second.course_code) == ("5670201", "5670202")
    assert nav == [("567", "20261")], "the second read must reuse the held position"
    assert len(sent) == 2, "one action POST per course, no second department select"


async def test_sections_reuse_the_held_course_page(sais):
    nav: list = []
    client, sent = _position_client(sais, nav, [
        _course_page_html(),
        _course_identity(section="1") + _constraint_table("AA", "KA"),
        _course_identity(section="2") + _constraint_table("KB", "ZZ"),
    ])

    await client.get_course_info("567", "20261", "5670201")
    first = await client.get_section_constraints("567", "20261", "5670201", "1")
    second = await client.get_section_constraints("567", "20261", "5670201", "2")

    assert [row.start_char for row in first.constraints] == ["AA"]
    assert [row.start_char for row in second.constraints] == ["KB"]
    assert nav == [("567", "20261")]
    assert len(sent) == 3, "each section is one POST with the held page's fields"


async def test_elision_falls_back_when_the_direct_page_is_wrong(sais):
    nav: list = []
    client, sent = _position_client(sais, nav, [
        _course_page_html(),
        COURSE_LIST,
        _course_page_html(course="5670202"),
    ])

    await client.get_course_info("567", "20261", "5670201")
    second = await client.get_course_info("567", "20261", "5670202")

    assert second.course_code == "5670202"
    assert len(nav) == 2, "a rejected reuse takes the full navigation"
    assert len(sent) == 3


async def test_section_reuse_falls_back_to_the_long_path(sais):
    nav: list = []
    client, sent = _position_client(sais, nav, [
        _course_page_html(),
        _course_identity(section="2") + _constraint_table("KB", "ZZ"),
        _course_page_html(),
        _course_identity(section="1") + _constraint_table("AA", "KA"),
    ])

    await client.get_course_info("567", "20261", "5670201")
    result = await client.get_section_constraints("567", "20261", "5670201", "1")

    assert [row.start_char for row in result.constraints] == ["AA"]
    assert len(nav) == 2
    assert len(sent) == 4


async def test_an_expired_position_is_not_reused(sais):
    nav: list = []
    client, _sent = _position_client(sais, nav, [
        _course_page_html(),
        _course_page_html(course="5670202"),
    ])

    await client.get_course_info("567", "20261", "5670201")
    _, semester, _ = client._position
    client._position = (client._position[0], semester, time.monotonic() - sais.SESSION_TTL_SECONDS - 1)

    await client.get_course_info("567", "20261", "5670202")

    assert len(nav) == 2


async def test_a_different_term_is_not_reused_from_the_position(sais):
    nav: list = []
    client, _sent = _position_client(sais, nav, [
        _course_page_html(),
        _course_page_html(course="5670202", semester="20252"),
    ])

    await client.get_course_info("567", "20261", "5670201")
    await client.get_course_info("567", "20252", "5670202")

    assert nav == [("567", "20261"), ("567", "20252")]


async def test_navigation_elision_can_be_switched_off(sais):
    nav: list = []
    client, _sent = _position_client(sais, nav, [
        _course_page_html(),
        _course_page_html(course="5670202"),
    ])
    client._nav_elision = False

    await client.get_course_info("567", "20261", "5670201")
    await client.get_course_info("567", "20261", "5670202")

    assert len(nav) == 2, "with the switch off every read re-selects the department"


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


def test_one_malformed_curriculum_row_keeps_verified_rows_and_reports_partial(sais):
    malformed = '''<div class="box-row-curriculum">
      <div class="box-column-label-curriculum">EE 201</div>
      <div class="box-column-value-curriculum"><div id="changed-markup"></div></div>
    </div>'''
    box = _curriculum_box(3, False)
    before_close, after_close = box.rsplit('</div>', 1)
    html = '<div id="curriculum">' + before_close + malformed + '</div>' + after_close + '</div>'

    result = sais.parse_student_curriculum(html)

    assert result["semesters"][0]["courses"][0]["course_code"] == "2300213"
    assert result["warnings"] == ["EE 201: curriculum course identity could not be read."]


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


async def test_prerequisite_read_rejects_a_plausible_previous_page(sais):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    _post_returning(client, [COURSE_LIST])

    with pytest.raises(ValueError, match="prerequisite table"):
        await client.get_course_prerequisites("567", "20261", "5670201")


async def test_course_listing_rejects_an_unrelated_success_page(sais):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        page = "<html><body><p>Welcome to SAIS.</p></body></html>"
        return "https://example.invalid/main.php", page, _soup(page)

    client._submit_course_list_page = course_list

    with pytest.raises(ValueError, match="programme course table"):
        await client.list_program_courses("567", "20261")


async def test_explicit_empty_course_listing_remains_valid(sais):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        page = '<input name="select_dept" value="567"><input name="select_semester" value="20261"><p>No course records found.</p>'
        return "https://example.invalid/main.php", page, _soup(page)

    client._submit_course_list_page = course_list

    assert await client.list_program_courses("567", "20261") == []


@pytest.mark.parametrize("method", ["list_program_courses", "get_thesis_courses"])
@pytest.mark.parametrize("department,semester", [("240", "20261"), ("567", "20252"), ("", "")])
async def test_readable_listing_requires_requested_context(sais, method, department, semester):
    client = _client(sais)
    page = (f'<input name="select_dept" value="{department}">'
            f'<input name="select_semester" value="{semester}">' + COURSE_LIST)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", page, _soup(page)

    client._submit_course_list_page = course_list
    _post_returning(client, [page])
    with pytest.raises(ValueError, match="identity"):
        await getattr(client, method)("567", "20261")


async def test_verified_empty_thesis_table_is_valid(sais):
    client = _client(sais)
    page = '<input name="select_dept" value="567"><input name="select_semester" value="20261">' + COURSE_LIST

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", page, _soup(page)

    client._submit_course_list_page = course_list
    _post_returning(client, [page])
    assert await client.get_thesis_courses("567", "20261") == []


async def test_thesis_table_reads_the_code_from_its_radio(sais):
    """The live thesis table leads with an empty radio cell.

    Reading cells[0] as the code returned every row with an empty course_code
    and the code sitting in its name, which the importer records as malformed
    and the MCP tool served as misaligned fields.
    """
    client = _client(sais)
    page = (
        '<input name="select_dept" value="567"><input name="select_semester" value="20261">'
        "<table>"
        "<tr><th></th><th>Code</th><th>Name</th><th>ECTS Credit</th><th>Credit</th><th>Level</th><th>Type</th></tr>"
        '<tr><td><input type="radio" name="text_course_code" value="5670801"></td>'
        "<td>5670801</td><td>SPECIAL STUDIES</td><td>10.0</td><td>0.00 (4.00,2.00,)</td>"
        "<td>Graduate</td><td>Thesis</td></tr>"
        "</table>"
    )

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", page, _soup(page)

    client._submit_course_list_page = course_list
    _post_returning(client, [page])
    result = await client.get_thesis_courses("567", "20261")

    assert [(row.course_code, row.name) for row in result] == [("5670801", "SPECIAL STUDIES")]
    assert result[0].ects_credit == "10.0"
    assert result[0].credit == "0.00 (4.00,2.00,)"
    assert result[0].level == "Graduate"


async def test_thesis_row_without_the_radio_still_reads_its_code(sais):
    """A leading empty cell is a column shift, not a reason to drop the row."""
    client = _client(sais)
    page = (
        '<input name="select_dept" value="567"><input name="select_semester" value="20261">'
        "<table>"
        "<tr><th></th><th>Code</th><th>Name</th><th>ECTS Credit</th><th>Credit</th><th>Level</th><th>Type</th></tr>"
        "<tr><td></td><td>5670801</td><td>SPECIAL STUDIES</td><td>10.0</td><td>0.00</td>"
        "<td>Graduate</td><td>Thesis</td></tr>"
        "</table>"
    )

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", page, _soup(page)

    client._submit_course_list_page = course_list
    _post_returning(client, [page])
    result = await client.get_thesis_courses("567", "20261")

    assert [(row.course_code, row.name) for row in result] == [("5670801", "SPECIAL STUDIES")]
    assert result[0].ects_credit == "10.0"
    assert result[0].type == "Thesis"


async def test_thesis_row_without_the_type_column_keeps_columns_aligned(sais):
    client = _client(sais)
    page = (
        '<input name="select_dept" value="567"><input name="select_semester" value="20261">'
        "<table>"
        "<tr><th></th><th>Code</th><th>Name</th><th>ECTS Credit</th><th>Credit</th><th>Level</th></tr>"
        '<tr><td><input type="radio" name="text_course_code" value="5670801"></td>'
        "<td>5670801</td><td>SPECIAL STUDIES</td><td>10.0</td><td>0.00 (4.00,2.00,)</td>"
        "<td>Graduate</td></tr>"
        "</table>"
    )

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", page, _soup(page)

    client._submit_course_list_page = course_list
    _post_returning(client, [page])
    result = await client.get_thesis_courses("567", "20261")

    assert result[0].course_code == "5670801"
    assert result[0].name == "SPECIAL STUDIES"
    assert result[0].ects_credit == "10.0"
    assert result[0].credit == "0.00 (4.00,2.00,)"
    assert result[0].level == "Graduate"
    assert result[0].type == ""


async def test_unreadable_thesis_rows_fail_instead_of_reading_as_empty(sais):
    client = _client(sais)
    page = (
        '<input name="select_dept" value="567"><input name="select_semester" value="20261">'
        "<table>"
        "<tr><th></th><th>Code</th><th>Name</th><th>ECTS Credit</th><th>Credit</th><th>Level</th><th>Type</th></tr>"
        "<tr><td></td><td>NOT-A-CODE</td><td>SPECIAL STUDIES</td><td>10.0</td><td>0.00</td>"
        "<td>Graduate</td><td>Thesis</td></tr>"
        "</table>"
    )

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", page, _soup(page)

    client._submit_course_list_page = course_list
    _post_returning(client, [page])

    with pytest.raises(ValueError, match="thesis course table"):
        await client.get_thesis_courses("567", "20261")


async def test_explicit_no_prerequisite_message_remains_a_valid_empty_answer(sais):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    _post_returning(client, [_course_rule_identity() + "<p>This course does not have any prerequisites.</p>"])

    assert await client.get_course_prerequisites("567", "20261", "5670201") == []


async def test_course_info_read_rejects_a_plausible_previous_page(sais):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    _post_returning(client, [COURSE_LIST])

    with pytest.raises(ValueError, match="course information page"):
        await client.get_course_info("567", "20261", "5670201")


@pytest.mark.parametrize(
    "meeting_cells, expected_time, expected_room",
    [
        (["Monday", "09:00", "10:00", "B101"], "09:00-10:00", "B101"),
        (["Monday", "09:00", "10:00"], "09:00-10:00", ""),
        (["Monday", "09:00-10:00", "B101"], "09:00-10:00", "B101"),
    ],
)
async def test_course_info_preserves_meeting_layouts(sais, meeting_cells, expected_time, expected_room):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    cells = "".join(f"<td>{value}</td>" for value in meeting_cells)
    page = (
        "<table><tr><th>Section</th><th>Instructor</th><th>Instructor</th></tr>"
        '<tr><td><input name="submit_section" value="1"></td>'
        "<td>Jane Example</td><td></td><td><table><tr>"
        + cells + "</tr></table></td></tr></table>"
    )
    _post_returning(client, [_course_identity() + page])
    result = await client.get_course_info("567", "20261", "5670201")

    assert len(result.sections) == 1
    assert len(result.sections[0].schedule) == 1
    meeting = result.sections[0].schedule[0]
    assert (meeting.day, meeting.time, meeting.room) == ("Monday", expected_time, expected_room)


async def test_section_constraint_read_rejects_a_plausible_previous_page(sais):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    course_page = _course_identity() + '<form><input type="hidden" name="hidden_redir" value="Course_Info"></form>'
    _post_returning(client, [course_page, COURSE_LIST])

    with pytest.raises(ValueError, match="restriction table"):
        await client.get_section_constraints("567", "20261", "5670201", "1")


@pytest.mark.parametrize("identity", [
    _course_identity(course="5670202"),
    _course_identity(semester="20252"),
    "",
    _course_identity() + "<p>Course Code: 5670202</p>",
])
async def test_readable_stale_course_detail_is_rejected(sais, identity):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    _post_returning(client, [identity + "<table><tr><td>Course Name: Previous Course Credit: 3</td></tr></table>"])
    with pytest.raises(ValueError, match="identity"):
        await client.get_course_info("567", "20261", "5670201")


@pytest.mark.parametrize("course, semester, section", [
    ("5670202", "20261", "1"), ("5670201", "20252", "1"), ("5670201", "20261", "2"),
])
async def test_readable_stale_section_constraints_are_rejected(sais, course, semester, section):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    table = "<table><tr><th>Dept</th><th>Char</th></tr></table>"
    _post_returning(client, [_course_identity(), _course_identity(course, semester, section) + table])
    with pytest.raises(ValueError, match="identity"):
        await client.get_section_constraints("567", "20261", "5670201", "1")


async def test_verified_empty_section_constraints_remain_valid(sais):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    page = "<p>Course Code: 5670201 Semester: 20261 Section: 01</p><table><tr><th>Dept</th><th>Char</th></tr></table>"
    _post_returning(client, [_course_identity(), page])
    result = await client.get_section_constraints("567", "20261", "5670201", "1")
    assert result.constraints == []


async def test_explicit_no_section_criteria_is_an_empty_answer(sais):
    """SAIS states the absence of restrictions as a sentence, not a table.

    The live page repeats every section button instead of identifying the one
    asked for, so it is accepted only after course and semester identity and
    only when the requested section is among those the page lists.
    """
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    empty = (
        "<p>Course Code: 5670201 Semester : 20261</p>"
        + "<p>There is no section criteria to take the selected courses for this section.</p>"
        + '<input type="submit" name="submit_section" value="1">'
        + '<input type="submit" name="submit_section" value="2">'
    )
    _post_returning(client, [_course_identity(), empty])

    result = await client.get_section_constraints("567", "20261", "5670201", "1")

    assert result.constraints == []
    assert result.section == "1"


async def test_no_section_criteria_without_a_section_list_is_refused(sais):
    """A page with no section evidence is refused, not trusted."""
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    empty = (
        "<p>Course Code: 5670201 Semester : 20261</p>"
        + "<p>There is no section criteria to take the selected courses for this section.</p>"
    )
    _post_returning(client, [_course_identity(), empty])

    with pytest.raises(ValueError, match="section identity"):
        await client.get_section_constraints("567", "20261", "5670201", "1")


def test_explicit_empty_section_criteria_understands_turkish_caps(sais):
    assert sais._explicit_empty_section_criteria(_soup("<p>ŞUBE İÇİN KRİTER YOKTUR.</p>"))
    assert sais._explicit_empty_section_criteria(_soup("<p>Bu şube için kriter bulunmamaktadır.</p>"))
    assert not sais._explicit_empty_section_criteria(_soup("<p>Department : Computer Engineering</p>"))


async def test_no_section_criteria_still_requires_the_requested_course(sais):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    empty = (
        _course_identity(course="5670202")
        + "<p>There is no section criteria to take the selected courses for this section.</p>"
        + '<input type="submit" name="submit_section" value="1">'
    )
    _post_returning(client, [_course_identity(), empty])

    with pytest.raises(ValueError, match="identity"):
        await client.get_section_constraints("567", "20261", "5670201", "1")


async def test_no_section_criteria_requires_the_requested_section_to_be_listed(sais):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    empty = (
        _course_identity()
        + "<p>There is no section criteria to take the selected courses for this section.</p>"
        + '<input type="submit" name="submit_section" value="1">'
        + '<input type="submit" name="submit_section" value="2">'
    )
    _post_returning(client, [_course_identity(), empty])

    with pytest.raises(ValueError, match="section identity"):
        await client.get_section_constraints("567", "20261", "5670201", "3")


@pytest.mark.parametrize("method, body", [
    ("get_course_prerequisites", "<p>This course does not have any prerequisites.</p>"),
    ("get_course_replacements", "<p>This course does not have any replacements.</p>"),
])
async def test_empty_rules_must_belong_to_the_requested_course(sais, method, body):
    client = _client(sais)

    async def course_list(*_args, **_kwargs):
        return "https://example.invalid/main.php", COURSE_LIST, _soup(COURSE_LIST)

    client._submit_course_list_page = course_list
    _post_returning(client, [_course_rule_identity(course="5670202") + body])
    with pytest.raises(ValueError, match="identity"):
        await getattr(client, method)("567", "20261", "5670201")
    _post_returning(client, [_course_rule_identity() + body])
    assert await getattr(client, method)("567", "20261", "5670201") == []


def test_course_rule_identity_requires_exact_radio_and_semester(sais):
    sais._verify_course_rule_response_identity(_soup(_course_rule_identity()), "5670201", "20261")
    labelled = _soup("<p>Semester: 20261 Auto Replace Courses for 5670201</p>")
    sais._verify_course_rule_response_identity(labelled, "5670201", "20261")
    with pytest.raises(ValueError, match="identity"):
        sais._verify_course_rule_response_identity(
            _soup(_course_rule_identity(course="5670202")), "5670201", "20261"
        )
    with pytest.raises(ValueError, match="identity"):
        sais._verify_course_rule_response_identity(
            _soup(_course_rule_identity(semester="20252")), "5670201", "20261"
        )
    with pytest.raises(ValueError, match="identity"):
        sais._verify_course_rule_response_identity(
            _soup("<p>Semester: 20261 Auto Replace Courses for 5670202</p>"), "5670201", "20261"
        )

def test_curriculum_parser_rejects_missing_board(sais):
    with pytest.raises(ValueError):
        sais.parse_student_curriculum('<form id="autologin"></form>')

def test_curriculum_parser_does_not_read_other_tabs(sais):
    html = '<div id="studentTranscript">' + _curriculum_box(1, False) + '</div>'
    html += '<div id="curriculum">' + _curriculum_box(3, False) + '</div>'
    assert [s["semester"] for s in sais.parse_student_curriculum(html)["semesters"]] == [3]

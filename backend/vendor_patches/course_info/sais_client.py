"""METU SAIS (Student Affairs Information System) HTTP Client and HTML Parsers."""

from typing import List, Dict, Optional, Tuple, Any
import asyncio
import functools
import inspect
import os
import re
import sys
import time
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup

from .config import settings

# Reaching an app through the SAIS portal costs three HTTP requests before the
# real one: get_content for a pkg token, content.php for the autologin form, and
# the autologin POST itself. That was re-done from scratch on every single tool
# call, so two thirds of all traffic to METU was re-establishing a session this
# process already had. Holding it for ten minutes turns a cold eight-section
# course from 53 requests into 26.
#
# Switchable from the broker's environment so a rollback is a config change
# rather than an image build at three in the morning.
SESSION_CACHE_ENABLED = os.getenv("COURSE_INFO_SESSION_CACHE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
SESSION_TTL_SECONDS = 10 * 60


def _report(**fields) -> None:
    """One line on stderr, which the MCP stdio client forwards to the broker's log.

    Field names only ever carry a tool name, a request count and a duration.
    Never a URL: the portal's ``pkg`` parameter and ``hidden_creds`` are session
    tokens, and this line exists to be greppable in a shared journal.
    """
    try:
        body = " ".join(f"{key}={value}" for key, value in fields.items())
        print(f"sais_call {body}", file=sys.stderr, flush=True)
    except Exception:
        pass  # a measurement must never be the thing that fails a read
from .models import (
    Department,
    Semester,
    DepartmentAndSemesterList,
    CourseSummary,
    CourseSection,
    CourseDetails,
    SectionConstraint,
    SectionConstraints,
    ScheduleEntry,
    CoursePrerequisite,
    CourseReplacement,
    ThesisCourse,
    StudentProgramType,
    StudentCourseCategory,
    StudentCategoryOverview,
    StudentCategoryCourse,
    StudentCategoryResult,
)


def clean_text(text: Optional[str]) -> str:
    """Cleans up text, normalizes whitespace and repairs windows-1252 / latin-1 mojibake if present."""
    if not text:
        return ""
    text = text.strip()
    # Only attempt mojibake repair if common UTF-8 double-encoding artifacts are present
    if any(c in text for c in ["Ã", "Ä", "Å", "â", "\x9d", "\x92"]):
        for enc in ["windows-1252", "latin-1", "iso-8859-9"]:
            try:
                fixed = text.encode(enc).decode("utf-8")
                text = fixed
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
    return re.sub(r"\s+", " ", text).strip()


def _explicit_empty_result(soup: BeautifulSoup, subject: str) -> bool:
    """Whether SAIS explicitly says the requested result has no rows."""
    text = clean_text(soup.get_text(" ", strip=True)).casefold()
    subjects = {
        "course": ("course", "courses", "ders"),
        "prerequisite": ("prerequisite", "prerequisites", "ön koşul", "önkoşul"),
    }.get(subject, (subject,))
    empty_markers = (
        "no record",
        "no data",
        "not found",
        "does not have",
        "there is no",
        "bulunmamaktadır",
        "bulunamadı",
        "yoktur",
    )
    subject_present = any(term in text for term in subjects)
    no_rows_sentence = re.search(r"\bno\b.{0,40}\b(?:records?|data)\b", text) is not None
    return subject_present and (any(marker in text for marker in empty_markers) or no_rows_sentence)


def parse_student_curriculum(html: str) -> dict:
    """Read the actual Student Information curriculum board (SAIS App 61).

    Only course identities, grades and completion markers leave this parser;
    student identifiers embedded in DOM ids and the student card are discarded.
    """
    soup = BeautifulSoup(html, "html.parser")
    semesters = {}
    warnings: List[str] = []
    for box in soup.select("#curriculum .box-table-curriculum"):
        head = box.select_one(".box-table-head-curriculum")
        if head is None:
            continue
        label = head.select_one(".box-column-label-curriculum")
        match = re.fullmatch(r"\s*(\d+)\s*\.\s*(?:SEMESTER|YARIYIL|DÖNEM)\s*", label.get_text() if label else "", re.I)
        if not match:
            continue
        number = int(match.group(1))
        completed = any(urljoin("https://example.invalid/", img.get("src", "")).split("?")[0].endswith("/check.gif") for img in head.select("img"))
        courses = []
        for row_index, row in enumerate(box.select(".box-row-curriculum"), start=1):
            name = row.select_one(".box-column-label-curriculum")
            value = row.select_one(".box-column-value-curriculum")
            if name is None or value is None:
                warnings.append(
                    f"Semester {number}, row {row_index}: curriculum cells could not be read."
                )
                continue
            label_text = clean_text(name.get_text(" ", strip=True))
            # Elective slots have a category label, not a named required course.
            if not re.fullmatch(r"[A-Z]{2,6}\s*\d{3,4}", label_text, re.I):
                continue
            container = value.find(attrs={"id": True}, recursive=False)
            fields = str(container.get("id", "") if container else "").split("|")
            if len(fields) != 7 or not re.fullmatch(r"\d{7}", fields[4]):
                warnings.append(f"{label_text}: curriculum course identity could not be read.")
                continue
            grade = ""
            for assigned in container.find_all(attrs={"id": True}):
                parts = assigned["id"].split("|")
                if len(parts) == 9:
                    grade = clean_text(parts[6])
                    if grade:
                        break
            # SAIS also renders an assigned-course child for registrations that
            # do not have a grade yet. Its text is nonempty (for example,
            # ``PHYS 213 MUST COURSE``) while the grade field in the id is
            # empty. That is a valid pending requirement, not a parse failure.
            courses.append({"course_code": fields[4], "course_name": label_text, "grade": grade})
        semester = {"semester": number, "completed": completed, "courses": courses}
        if number in semesters and semesters[number] != semester:
            raise ValueError("SAIS returned conflicting curriculum boards")
        semesters[number] = semester
    if not semesters:
        raise ValueError("SAIS Student Information curriculum board could not be read")
    return {"semesters": [semesters[n] for n in sorted(semesters)], "warnings": warnings}


class SAISAuthError(Exception):
    """Raised when authentication against METU SAIS fails."""
    pass


class SAISClient:
    """Client for interacting with METU SAIS Student Portal services."""

    BASE_URL = "https://student.metu.edu.tr"
    SIGNIN_URL = f"{BASE_URL}/sso/backend/request/user/signin"
    GET_CONTENT_URL = f"{BASE_URL}/portal/backend/request/route/get_content"

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        locale: Optional[str] = None,
    ):
        self.username = username or settings.sais_username
        self.password = password or settings.sais_password
        self.locale = locale or settings.locale or "tr"
        self._token: Optional[str] = None
        # At most one app's proxy session is held at a time, because the server
        # keeps exactly one: the cookie jar is shared, and reaching app 178
        # moves the session away from app 64. Modelling that as a single slot
        # is what stops a cached app-64 page being replayed after a category
        # read has quietly moved the session elsewhere.
        self._session: Optional[Tuple[int, float, Tuple[str, str, BeautifulSoup]]] = None
        # Calls on one client must not overlap. See the note on the lock below.
        self._lock = asyncio.Lock()
        # Every HTTP request this client makes, counted once. The saving from
        # holding the proxy session is a request count and nothing else, so it
        # has to be observable from outside to be believed.
        self._requests = 0
        self._client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
            },
        )
        # Wrapped at ``send`` rather than at each call site, so a request added
        # anywhere in this file is counted without anyone remembering to.
        _send = self._client.send

        async def counting_send(*args, **kwargs):
            self._requests += 1
            return await _send(*args, **kwargs)

        self._client.send = counting_send

    async def aclose(self):
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def authenticate(self, force: bool = False) -> str:
        """Authenticate with SAIS SSO and obtain JWT token."""
        if self._token and not force:
            return self._token

        if not self.username or not self.password:
            raise SAISAuthError(
                "SAIS credentials are required. Set SAIS_USERNAME and SAIS_PASSWORD in .env or pass them explicitly."
            )

        signin_headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        signin_payload = {
            "username": self.username,
            "password": self.password,
        }

        resp = await self._client.post(
            self.SIGNIN_URL,
            json=signin_payload,
            headers=signin_headers,
        )

        if resp.status_code != 200:
            raise SAISAuthError(f"Signin HTTP status {resp.status_code}: {resp.text}")

        token = resp.headers.get("token") or resp.headers.get("Token")
        if not token:
            try:
                body = resp.json()
                if "error" in body and body["error"]:
                    raise SAISAuthError(f"Signin error from SAIS: {body['error']}")
            except Exception:
                pass
            raise SAISAuthError("Failed to obtain authentication token from SAIS response headers.")

        self._token = token
        return token

    async def _get_app_proxy_session(
        self, app_code: int, *, refresh: bool = False
    ) -> Tuple[str, str, BeautifulSoup]:
        """The app's entry page, reusing the session when we still hold it.

        ``refresh`` forces the three-request preamble. Callers pass it when the
        page they got back says the session moved — never on a merely empty
        result, which is a real answer for a department with no courses and
        would otherwise retry for ever.
        """
        if SESSION_CACHE_ENABLED and not refresh and self._session is not None:
            held_app, opened_at, payload = self._session
            if held_app == app_code and time.monotonic() - opened_at < SESSION_TTL_SECONDS:
                return payload
        payload = await self._open_app_proxy_session(app_code)
        # Replaces rather than adds: one slot, one live session.
        self._session = (app_code, time.monotonic(), payload)
        return payload

    def _session_lost(self, soup: BeautifulSoup) -> bool:
        """Whether a response is the portal asking us to log in again.

        Positive evidence only. "Nothing parsed" is not evidence: an empty
        department is a real answer, and treating it as a lost session would
        turn every genuinely empty page into a retry.
        """
        return soup.find("form", id="autologin") is not None

    async def _open_app_proxy_session(self, app_code: int) -> Tuple[str, str, BeautifulSoup]:
        """Navigate through SAIS portal get_content, autologin form, and return initial proxy HTML."""
        token = await self.authenticate()

        # Step 1: get_content route
        content_headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Token": token,
            "Locale": self.locale,
        }
        content_payload = {"app": app_code, "additionalInfo": False}

        resp = await self._client.post(
            self.GET_CONTENT_URL,
            json=content_payload,
            headers=content_headers,
        )

        if resp.status_code != 200:
            # Try re-authenticating once if unauthorized
            token = await self.authenticate(force=True)
            content_headers["Token"] = token
            resp = await self._client.post(
                self.GET_CONTENT_URL,
                json=content_payload,
                headers=content_headers,
            )
            if resp.status_code != 200:
                raise SAISAuthError(f"get_content for app {app_code} failed with status {resp.status_code}")

        res_json = resp.json()
        pkg = res_json.get("pkg")
        if not pkg:
            raise ValueError(f"No pkg token returned for app {app_code}")

        # Step 2: GET content.php?pkg=...
        page_url = f"{self.BASE_URL}/portal/content.php?pkg={pkg}"
        page_resp = await self._client.get(
            page_url,
            headers={"Referer": f"{self.BASE_URL}/portal/"},
        )

        autologin_html = self._decode_html(page_resp)
        soup = BeautifulSoup(autologin_html, "html.parser")
        form = soup.find("form", id="autologin") or soup.find("form")
        if not form:
            raise ValueError(f"Autologin form not found in content.php for app {app_code}")

        action = form.get("action", "")
        if not action.startswith("http"):
            action = f"{self.BASE_URL}/{action.lstrip('/')}"

        form_data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            val = inp.get("value", "")
            if name:
                form_data[name] = val

        # Step 3: POST autologin form to proxy gateway
        proxy_resp = await self._client.post(
            action,
            data=form_data,
            headers={"Referer": page_url},
        )

        proxy_html = self._decode_html(proxy_resp)
        proxy_soup = BeautifulSoup(proxy_html, "html.parser")
        return str(proxy_resp.url), proxy_html, proxy_soup

    def _decode_html(self, response: httpx.Response) -> str:
        """Safely decodes HTML responses handling iso-8859-9 / windows-1254 and utf-8."""
        content = response.content
        for encoding in ["utf-8", "windows-1254", "iso-8859-9", "latin-1"]:
            try:
                return content.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return content.decode("utf-8", errors="replace")

    # =========================================================================
    # Service 64: View Program Course Details
    # =========================================================================

    async def get_student_curriculum(self) -> dict:
        """Fetch the Student Information page and parse its Curriculum tab."""
        # This form advances the page; never reuse a saved pre-submit form.
        self._session = None
        url, _, landing = await self._open_app_proxy_session(61)
        selector = landing.find("select", attrs={"name": "text_semester_programtype"})
        form = selector.find_parent("form") if selector else None
        if form is None:
            raise ValueError("SAIS Student Information semester form was not found")
        options = [option for option in selector.find_all("option") if option.get("value", "").endswith("|1")]
        if not options:
            raise ValueError("SAIS main programme could not be selected")
        selected = next((option for option in options if option.has_attr("selected")), options[0])
        values = {item["name"]: item.get("value", "") for item in form.select('input[type="hidden"][name]')}
        values["text_semester_programtype"] = selected["value"]
        values["submit_studentInformation"] = "Submit"
        endpoint = urljoin(url, form.get("action") or url)
        response = None
        for attempt in range(2):
            try:
                # The MCP caller gives the complete tool call 30 seconds. A
                # single stalled SAIS response must leave enough time for one
                # bounded retry instead of consuming that whole budget.
                response = await self._client.post(endpoint, data=values, timeout=12.0)
                break
            except httpx.TimeoutException:
                if attempt:
                    raise
        if response is None:  # pragma: no cover - the loop returns or raises
            raise RuntimeError("SAIS curriculum request produced no response")
        response.raise_for_status()
        return parse_student_curriculum(self._decode_html(response))

    async def get_departments_and_semesters(self) -> DepartmentAndSemesterList:
        """Fetch all available METU departments and semesters from Program Course Details (64)."""
        _, _, soup = await self._get_app_proxy_session(64)

        departments: List[Department] = []
        dept_select = soup.find("select", {"name": re.compile(r"select_dept", re.I)})
        if dept_select:
            for opt in dept_select.find_all("option"):
                code = opt.get("value", "").strip()
                name = clean_text(opt.get_text(strip=True))
                if code and code != "0":
                    departments.append(Department(code=code, name=name))

        semesters: List[Semester] = []
        sem_select = soup.find("select", {"name": re.compile(r"select_semester", re.I)})
        if sem_select:
            for opt in sem_select.find_all("option"):
                code = opt.get("value", "").strip()
                name = clean_text(opt.get_text(strip=True))
                if code:
                    semesters.append(Semester(code=code, name=name))

        return DepartmentAndSemesterList(departments=departments, semesters=semesters)

    async def _submit_course_list_page(
        self, department_code: str, semester_code: str, *, _retrying: bool = False
    ) -> Tuple[str, str, BeautifulSoup]:
        """Submit the department and semester selection in App 64 to get the course list page."""
        target_url, _, soup = await self._get_app_proxy_session(64, refresh=_retrying)

        form = soup.find("form")
        if not form:
            raise ValueError("Course query form not found in App 64 initial page.")

        action = form.get("action", "main.php")
        if not action.startswith("http"):
            base_dir = target_url.rsplit("/", 1)[0]
            action = f"{base_dir}/{action.lstrip('/')}"

        hidden_creds = ""
        creds_elem = soup.find("input", {"name": "hidden_creds"})
        if creds_elem:
            hidden_creds = creds_elem.get("value", "")

        hidden_redir = "Login"
        redir_elem = soup.find("input", {"name": "hidden_redir"})
        if redir_elem:
            hidden_redir = redir_elem.get("value", "Login")

        post_data = {
            "select_dept": str(department_code).strip(),
            "select_semester": str(semester_code).strip(),
            "textWithoutThesis": "1",
            "submit_CourseList": "Submit",
            "hidden_redir": hidden_redir,
            "hidden_creds": hidden_creds,
        }

        resp = await self._client.post(
            action,
            data=post_data,
            headers={"Referer": target_url},
        )
        html = self._decode_html(resp)
        res_soup = BeautifulSoup(html, "html.parser")
        if self._session_lost(res_soup) and not _retrying:
            # The held session is no longer the one the server has. Drop it and
            # take the long way round exactly once; a second failure is a real
            # failure and belongs to the caller.
            self._session = None
            return await self._submit_course_list_page(
                department_code, semester_code, _retrying=True
            )
        return action, html, res_soup

    async def list_program_courses(
        self, department_code: str, semester_code: str
    ) -> List[CourseSummary]:
        """List all courses offered for a specific department and semester."""
        _, _, soup = await self._submit_course_list_page(department_code, semester_code)

        courses: List[CourseSummary] = []
        course_table_found = False
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            header_cells = [td.get_text(strip=True).lower() for td in rows[0].find_all(["td", "th"])]
            if any("code" in h for h in header_cells) and any("name" in h for h in header_cells):
                course_table_found = True
                for tr in rows[1:]:
                    cells = tr.find_all(["td", "th"])
                    if len(cells) >= 6:
                        radio = tr.find("input", {"type": "radio", "name": "text_course_code"})
                        code_from_radio = radio.get("value", "").strip() if radio else ""

                        cell_texts = [clean_text(c.get_text(" ", strip=True)) for c in cells]
                        if radio and len(cell_texts) >= 7:
                            code = code_from_radio or cell_texts[1]
                            name = cell_texts[2]
                            ects = cell_texts[3]
                            credit = cell_texts[4]
                            level = cell_texts[5]
                            ctype = cell_texts[6] if len(cell_texts) > 6 else ""
                        else:
                            code = code_from_radio or cell_texts[0]
                            name = cell_texts[1]
                            ects = cell_texts[2] if len(cell_texts) > 2 else ""
                            credit = cell_texts[3] if len(cell_texts) > 3 else ""
                            level = cell_texts[4] if len(cell_texts) > 4 else ""
                            ctype = cell_texts[5] if len(cell_texts) > 5 else ""

                        if code:
                            courses.append(
                                CourseSummary(
                                    course_code=code,
                                    name=name,
                                    ects_credit=ects,
                                    credit=credit,
                                    level=level,
                                    type=ctype,
                                )
                            )

        if not course_table_found and not _explicit_empty_result(soup, "course"):
            raise ValueError("SAIS programme course table could not be read")

        return courses

    async def get_section_constraints(
        self, department_code: str, semester_code: str, course_code: str, section: str
    ) -> SectionConstraints:
        """Who may register for one section: the eligibility table behind its number.

        The section number on the course page is a submit button, not a link,
        so this page is one POST deeper than everything else here and is the
        only place METU states the restriction in a structured form. The
        free-text "critical info" box beside it is almost always empty; the
        real rules — admitted departments, surname ranges, CGPA and year
        bounds — live here.

        The form's hidden fields are read back from the course page and resent
        rather than hard-coded: this old interface identifies where a POST came
        from with a ``hidden_redir`` token that differs per page, and guessing
        it returns the previous page instead of an error.
        """
        action_url, _, _ = await self._submit_course_list_page(department_code, semester_code)

        resp = await self._client.post(
            action_url,
            data={
                "text_course_code": str(course_code).strip(),
                "SubmitCourseInfo": "Submit",
                "hidden_redir": "Course_List",
            },
            headers={"Referer": action_url},
        )
        course_soup = BeautifulSoup(self._decode_html(resp), "html.parser")

        post_data: Dict[str, str] = {}
        for hidden in course_soup.find_all("input", {"type": "hidden"}):
            name = hidden.get("name")
            if name:
                post_data[name] = hidden.get("value", "")
        post_data["submit_section"] = str(section).strip()

        resp = await self._client.post(
            action_url,
            data=post_data,
            headers={"Referer": action_url},
        )
        soup = BeautifulSoup(self._decode_html(resp), "html.parser")

        rows: List[SectionConstraint] = []
        constraint_table_found = False
        for table in soup.find_all("table"):
            trs = table.find_all("tr")
            if not trs:
                continue
            headers = [clean_text(td.get_text(" ", strip=True)).lower() for td in trs[0].find_all(["td", "th"])]
            # Identified by its columns, not its position: this page carries
            # several unlabelled layout tables and the eligibility one is the
            # only table naming a department alongside a character range.
            if not (any("dept" in h for h in headers) and any("char" in h for h in headers)):
                continue
            constraint_table_found = True
            for tr in trs[1:]:
                cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
                if len(cells) < 9 or not cells[0]:
                    continue
                rows.append(
                    SectionConstraint(
                        given_dept=cells[0],
                        start_char=cells[1],
                        end_char=cells[2],
                        min_cgpa=cells[3],
                        max_cgpa=cells[4],
                        min_year=cells[5],
                        max_year=cells[6],
                        start_grade=cells[7],
                        end_grade=cells[8],
                    )
                )
            break

        if not constraint_table_found:
            raise ValueError("SAIS section restriction table could not be read")

        return SectionConstraints(
            department=str(department_code),
            semester=str(semester_code),
            course_code=str(course_code).strip(),
            section=str(section).strip(),
            constraints=rows,
        )

    async def get_course_info(
        self, department_code: str, semester_code: str, course_code: str
    ) -> CourseDetails:
        """Get section details, instructors, and critical course info for a course."""
        action_url, _, soup = await self._submit_course_list_page(department_code, semester_code)

        post_data = {
            "text_course_code": str(course_code).strip(),
            "SubmitCourseInfo": "Submit",
            "hidden_redir": "Course_List",
        }

        resp = await self._client.post(
            action_url,
            data=post_data,
            headers={"Referer": action_url},
        )
        html = self._decode_html(resp)
        res_soup = BeautifulSoup(html, "html.parser")

        dept_name = ""
        sem_name = str(semester_code)
        c_name = ""
        credit_info = ""

        header_table = res_soup.find("table")
        if header_table:
            text = header_table.get_text(" ", strip=True)
            dept_m = re.search(r"Department\s*:\s*([^:\n\r]+?)(?=Semester|$)", text, re.I)
            if dept_m:
                dept_name = clean_text(dept_m.group(1))
            name_m = re.search(r"Course Name\s*:\s*([^:\n\r]+?)(?=Credit|$)", text, re.I)
            if name_m:
                c_name = clean_text(name_m.group(1))
            credit_m = re.search(r"Credit\s*:\s*([^\n\r]+)", text, re.I)
            if credit_m:
                credit_info = clean_text(credit_m.group(1))

        sections: List[CourseSection] = []
        section_table_found = False
        for table in res_soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["td", "th"])]
            if any("section" in h for h in headers) and any("instructor" in h for h in headers):
                section_table_found = True
                current_section: Optional[CourseSection] = None
                for tr in rows[1:]:
                    cells = tr.find_all(["td", "th"])
                    if not cells:
                        continue
                    sec_input = tr.find("input", {"name": "submit_section"})
                    if sec_input:
                        sec_num = clean_text(sec_input.get("value", ""))
                        instructors = []
                        if len(cells) > 1:
                            inst1 = clean_text(cells[1].get_text(strip=True))
                            if inst1:
                                instructors.append(inst1)
                        if len(cells) > 2:
                            inst2 = clean_text(cells[2].get_text(strip=True))
                            if inst2 and inst2 not in instructors:
                                instructors.append(inst2)

                        syllabus_avail = bool(tr.find("button", {"name": "submit_syllabus"}))
                        crit_area = tr.find("textarea", {"name": "text_eklenti"})
                        crit_info = clean_text(crit_area.get_text(strip=True)) if crit_area else ""

                        current_section = CourseSection(
                            section=sec_num,
                            instructors=instructors,
                            syllabus_available=syllabus_avail,
                            critical_info=crit_info,
                            schedule=[],
                        )
                        sections.append(current_section)
                        # The live page nests a four-column meeting table in
                        # the section row: day, start, end, room. Parse it
                        # here so the later recursive rows cannot duplicate it.
                        nested_table = tr.find("table")
                        if nested_table:
                            for sch_tr in nested_table.find_all("tr"):
                                sch_cells = [clean_text(td.get_text(strip=True)) for td in sch_tr.find_all("td")]
                                if any(sch_cells) and len(sch_cells) >= 3:
                                    start = sch_cells[1] if len(sch_cells) > 1 else ""
                                    end = sch_cells[2] if len(sch_cells) > 2 else ""
                                    current_section.schedule.append(
                                        ScheduleEntry(
                                            day=sch_cells[0] if len(sch_cells) > 0 else "",
                                            time=f"{start}-{end}" if len(sch_cells) >= 4 else start,
                                            room=sch_cells[3] if len(sch_cells) >= 4 else end,
                                        )
                                    )

        if not c_name and not section_table_found:
            raise ValueError("SAIS course information page could not be read")

        return CourseDetails(
            department=dept_name or department_code,
            semester=sem_name,
            course_code=course_code,
            course_name=c_name,
            credit_info=credit_info,
            sections=sections,
        )

    async def get_course_prerequisites(
        self, department_code: str, semester_code: str, course_code: str
    ) -> List[CoursePrerequisite]:
        """Fetch prerequisite courses and rules for a course."""
        action_url, _, _ = await self._submit_course_list_page(department_code, semester_code)

        post_data = {
            "text_course_code": str(course_code).strip(),
            "SubmitPrerequisite": "Submit",
            "hidden_redir": "Course_List",
        }

        resp = await self._client.post(
            action_url,
            data=post_data,
            headers={"Referer": action_url},
        )
        html = self._decode_html(resp)
        soup = BeautifulSoup(html, "html.parser")

        prereqs: List[CoursePrerequisite] = []
        prerequisite_table_found = False
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["td", "th"])]
            if any("course code" in h or "prerequisite" in h for h in headers) and any("set no" in h or "min grade" in h for h in headers):
                prerequisite_table_found = True
                for tr in rows[1:]:
                    cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
                    if len(cells) >= 7:
                        prereqs.append(
                            CoursePrerequisite(
                                program_code=cells[0],
                                dept_version=cells[1],
                                prerequisite_course_code=cells[2],
                                name=cells[3],
                                credit=cells[4],
                                set_no=cells[5],
                                min_grade=cells[6] if len(cells) > 6 else "DD",
                                level_type=cells[7] if len(cells) > 7 else "",
                                position=cells[8] if len(cells) > 8 else "",
                            )
                        )

        if not prerequisite_table_found and not _explicit_empty_result(soup, "prerequisite"):
            raise ValueError("SAIS prerequisite table could not be read")

        return prereqs

    async def get_course_replacements(
        self, department_code: str, semester_code: str, course_code: str
    ) -> List[CourseReplacement]:
        """Fetch equivalent / auto-replacement (Denk Dersler) courses for a course."""
        action_url, _, _ = await self._submit_course_list_page(department_code, semester_code)

        post_data = {
            "text_course_code": str(course_code).strip(),
            "SubmitReplacement": "Submit",
            "hidden_redir": "Course_List",
        }

        resp = await self._client.post(
            action_url,
            data=post_data,
            headers={"Referer": action_url},
        )
        html = self._decode_html(resp)
        soup = BeautifulSoup(html, "html.parser")

        replacements: List[CourseReplacement] = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["td", "th"])]
            if any("course code" in h or "replacement" in h for h in headers) and any("name" in h for h in headers):
                for tr in rows[1:]:
                    cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
                    if len(cells) >= 6:
                        replacements.append(
                            CourseReplacement(
                                program_code=cells[0],
                                dept_version=cells[1],
                                replaced_course_code=cells[2],
                                name=cells[3],
                                credit=cells[4],
                                level=cells[5] if len(cells) > 5 else "",
                                status=cells[6] if len(cells) > 6 else "",
                            )
                        )

        return replacements

    async def get_thesis_courses(
        self, department_code: str, semester_code: str
    ) -> List[ThesisCourse]:
        """Fetch thesis work courses for a department."""
        action_url, _, _ = await self._submit_course_list_page(department_code, semester_code)

        post_data = {
            "SubmitThesisWork": "Thesis Work Courses",
            "hidden_redir": "Course_List",
        }

        resp = await self._client.post(
            action_url,
            data=post_data,
            headers={"Referer": action_url},
        )
        html = self._decode_html(resp)
        soup = BeautifulSoup(html, "html.parser")

        courses: List[ThesisCourse] = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["td", "th"])]
            if any("code" in h for h in headers) and any("name" in h for h in headers):
                for tr in rows[1:]:
                    cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
                    if len(cells) >= 6:
                        courses.append(
                            ThesisCourse(
                                course_code=cells[0],
                                name=cells[1],
                                ects_credit=cells[2] if len(cells) > 2 else "",
                                credit=cells[3] if len(cells) > 3 else "",
                                level=cells[4] if len(cells) > 4 else "",
                                type=cells[5] if len(cells) > 5 else "",
                            )
                        )

        return courses

    # =========================================================================
    # Service 178: View Student Course Categories
    # =========================================================================

    async def get_student_categories_overview(self) -> StudentCategoryOverview:
        """Fetch the logged-in student's program types and course category options."""
        _, _, soup = await self._get_app_proxy_session(178)

        program_types: List[StudentProgramType] = []
        prog_select = soup.find("select", {"name": "text_program_type"})
        if prog_select:
            for opt in prog_select.find_all("option"):
                val = opt.get("value", "").strip()
                text = clean_text(opt.get_text(strip=True))
                if val and val != "0":
                    program_types.append(StudentProgramType(id=val, name=text))

        categories: List[StudentCourseCategory] = []
        cat_select = soup.find("select", {"name": "text_program_category"})
        if cat_select:
            for opt in cat_select.find_all("option"):
                val = opt.get("value", "").strip()
                text = clean_text(opt.get_text(strip=True))
                if val and val != "0":
                    categories.append(StudentCourseCategory(id=val, name=text))

        return StudentCategoryOverview(
            program_types=program_types,
            course_categories=categories,
        )

    async def get_student_category_courses(
        self,
        program_type: str = "1",
        category_id: str = "1-236",
    ) -> StudentCategoryResult:
        """Fetch courses belonging to a student's chosen category (e.g. MUST COURSE, DEPARTMENTAL ELECTIVE)."""
        target_url, _, soup = await self._get_app_proxy_session(178)

        form = soup.find("form")
        if not form:
            raise ValueError("Form not found in App 178 initial page.")

        action = form.get("action", "main.php")
        if not action.startswith("http"):
            base_dir = target_url.rsplit("/", 1)[0]
            action = f"{base_dir}/{action.lstrip('/')}"

        cat_name = category_id
        cat_select = soup.find("select", {"name": "text_program_category"})
        if cat_select:
            matched_opt = cat_select.find("option", {"value": category_id})
            if matched_opt:
                cat_name = clean_text(matched_opt.get_text(strip=True))

        post_data = {
            "text_program_type": str(program_type).strip(),
            "text_program_category": str(category_id).strip(),
            "submitDevam": "",
            "hidden_redir": "StudentElectiveCourses",
        }

        resp = await self._client.post(
            action,
            data=post_data,
            headers={"Referer": target_url},
        )
        html = self._decode_html(resp)
        res_soup = BeautifulSoup(html, "html.parser")

        alert_elem = res_soup.find("div", class_=re.compile(r"alert", re.I))
        msg = None
        if alert_elem:
            msg = clean_text(alert_elem.get_text(" ", strip=True))

        courses: List[StudentCategoryCourse] = []
        table = res_soup.find("table")
        if table:
            rows = table.find_all("tr")
            if len(rows) > 1:
                for tr in rows[1:]:
                    cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
                    if len(cells) >= 6:
                        courses.append(
                            StudentCategoryCourse(
                                course_code=cells[0],
                                course_name=cells[1],
                                category=cells[2],
                                program_type=cells[3],
                                credit=cells[4],
                                year_or_ects=cells[5] if len(cells) > 5 else "",
                            )
                        )

        return StudentCategoryResult(
            category_id=category_id,
            category_name=cat_name,
            message=msg,
            courses=courses,
        )


_client_cache: Dict[str, Tuple[Optional[asyncio.AbstractEventLoop], SAISClient]] = {}


def get_cached_client(
    username: Optional[str] = None,
    password: Optional[str] = None,
    locale: Optional[str] = None,
) -> SAISClient:
    """Retrieve or create a cached SAISClient instance bound to the current running event loop."""
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    u = username or settings.sais_username
    p = password or settings.sais_password
    loc = locale or settings.locale or "tr"
    key = f"{u}:{loc}"

    cached = _client_cache.get(key)
    if (
        cached is None
        or cached[0] != current_loop
        or cached[1]._client.is_closed
    ):
        client = SAISClient(username=u, password=p, locale=loc)
        if current_loop is not None:
            _client_cache[key] = (current_loop, client)
        return client
    return cached[1]


# One call at a time, per client.
#
# This client holds a single cookie jar and SAIS's course-listing flow is
# stateful on the server: the tokens for the next request are read out of the
# previous response, and which page the session is "on" lives in the PHP
# session. Two calls in flight therefore land on each other's pages, and the
# failure is silent -- a wrong token returns the *previous* page rather than an
# error. Holding a cached proxy session makes that worse, not better, so the two
# belong in the same change.
#
# The broker serialises its own catalog reads, but the agent reaches these tools
# through the MCP entrypoint directly and never passes through that code. The
# lock lives here because here is the level of the thing that is actually shared.
#
# Applied to every public coroutine rather than a named list, so a method added
# by a future upstream release is serialised by default rather than by whoever
# remembers. Two exclusions, both deliberate:
#   * ``authenticate`` is called from inside the locked path and would deadlock;
#   * ``aclose`` is teardown and must not queue behind a call in flight.
def _serialised(method):
    @functools.wraps(method)
    async def guarded(self, *args, **kwargs):
        async with self._lock:
            before = self._requests
            started = time.monotonic()
            try:
                return await method(self, *args, **kwargs)
            finally:
                _report(
                    tool=method.__name__,
                    requests=self._requests - before,
                    ms=round((time.monotonic() - started) * 1000),
                )

    return guarded


for _name, _member in list(vars(SAISClient).items()):
    if _name.startswith("_") or _name in {"authenticate", "aclose"}:
        continue
    if inspect.iscoroutinefunction(_member):
        setattr(SAISClient, _name, _serialised(_member))

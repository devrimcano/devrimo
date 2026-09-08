"""Read-only bridge from legacy per-user MCPs into typed planning snapshots.

The agent never supplies transcript values to the planner. This bridge calls
the connected SAIS functions directly, normalizes their structured results,
and persists only the typed private snapshot used by deterministic code.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from agno.tools.mcp import MCPTools
from sqlalchemy.ext.asyncio import AsyncSession

from app.campus.eligibility import grade_token, tr_upper
from app.campus.mcp_results import mcp_payload, parse_json_document
from app.config import get_settings
from app.db.session import SessionLocal
from app.logging import get_logger
from app.planning.service import upsert_academic_snapshot
from app.student.service import apply_verified_context

logger = get_logger(__name__)


def _function(toolkits: Iterable[MCPTools], name: str):
    for toolkit in toolkits:
        function = (toolkit.functions or {}).get(name)
        if function is not None:
            return function
    return None


# Keys a server uses to wrap its real answer.
_RESULT_WRAPPERS = ("result", "data", "content", "output", "payload", "response")


def _unwrap_result(payload: Any, depth: int = 0) -> Any:
    """Peel a single-key wrapper, decoding a JSON document held as a string.

    ``{"result": "{\"Bölüm\": ...}"}`` is what SAIS actually returns, and the
    fields are unreachable until both layers come off. Only a lone wrapper key
    is unwrapped, so a genuine record that happens to contain a "data" column
    alongside others is left alone.
    """
    if depth > 3:
        return payload
    if isinstance(payload, str):
        parsed = parse_json_document(payload)
        return payload if isinstance(parsed, str) else _unwrap_result(parsed, depth + 1)
    if isinstance(payload, dict) and len(payload) == 1:
        key, value = next(iter(payload.items()))
        if str(key).strip().lower() in _RESULT_WRAPPERS:
            return _unwrap_result(value, depth + 1)
    return payload


async def _payload(function, *, tool: str = "", attempts: int = 1, timeout: float | None = None) -> Any:
    """Call one SAIS tool and return its document, or None with a logged reason.

    ``attempts`` retries a failed call. METU answers the student card in about
    a second when its session is warm and occasionally takes longer than the
    thirty seconds the campus server allows it, which surfaces here as a
    ReadTimeout and cost the student their department for the whole refresh.
    These reads are page fetches with no side effects, so retrying one is safe,
    and the retry reuses the established login rather than signing in again.

    ``timeout`` abandons a call that is taking too long for what it is worth.
    Only pass it for a read whose connection is torn down straight afterwards:
    cancelling leaves the campus server still working on a request nobody is
    reading, which is fine for a session about to close and not fine for the
    pooled one an agent turn is sharing.
    """
    if function is None or function.entrypoint is None:
        logger.warning("sais_tool_missing", tool=tool)
        return None
    payload = None
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            call = function.entrypoint()
            payload = mcp_payload(await (asyncio.wait_for(call, timeout) if timeout else call))
            break
        except Exception as exc:
            # Previously this propagated as an unhandled 500 with the campus
            # server's error buried in a framework stack.
            logger.warning("sais_tool_failed", tool=tool, attempt=attempt, error=str(exc))
            if attempt >= max(attempts, 1):
                return None
            # Long enough for a stalled request to have been given up on,
            # short enough to stay inside the sync's own budget.
            await asyncio.sleep(2)
    # The SAIS server wraps its answer: the useful document arrives as a JSON
    # string under a single "result" key, so the outer value is a dict with
    # nothing readable in it. Every field lookup then finds no keys and the
    # department comes back empty while nothing reports a failure.
    payload = _unwrap_result(payload)
    # A payload still in string form means the server answered with prose
    # rather than a document, which this bridge has no way to normalize.
    if isinstance(payload, str):
        message = payload.strip()
        # That prose is SAIS explaining itself — an expired session, a record
        # it cannot find — and throwing it away is why a failed refresh could
        # only ever say "SAIS did not answer". Short answers are messages and
        # are logged; anything longer is transcript content and is reported by
        # length alone so no coursework ends up in the journal.
        logger.warning(
            "sais_payload_not_structured",
            tool=tool,
            length=len(message),
            detail=message[:200] if len(message) <= 300 else None,
        )
        return None
    return payload


# SAIS labels its student card in the student's own locale, so the key is
# "Bölüm" or "Soyadı" as often as "Department" or "Surname". Matching those
# by lowercasing alone fails on every Turkish character: "bölüm" is not
# "bolum", and the field is silently read as absent.
_KEY_FOLD = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                           "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})


def _fold_key(text: str) -> str:
    """A label reduced to letters and digits, Turkish characters folded."""
    return re.sub(r"[^a-z0-9]", "", str(text).translate(_KEY_FOLD).casefold())


def _surname_prefix(value: Any) -> str | None:
    """The two letters a section's eligibility range compares, and nothing else.

    METU writes those bounds as two characters, so this is the whole of what the
    check reads. Trimming here rather than at the comparison means the rest of
    the surname is never stored, never logged and never leaves SAIS.

    The *last* whitespace-separated token is taken. The label this arrives
    under is "Adı Soyadı" as often as "Soyadı", and a Turkish surname is
    written last, so reading the whole field would store the first two letters
    of the student's *given* name and answer every eligibility range wrong.
    """
    parts = str(value or "").split()
    return tr_upper(parts[-1])[:2] if parts else None


def _all_labels(value: Any, depth: int = 0) -> list[str]:
    """Every key in a payload, at any depth. Labels only, never values."""
    if depth > 6:
        return []
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.append(str(key))
            found.extend(_all_labels(child, depth + 1))
    elif isinstance(value, list):
        for child in value[:5]:
            found.extend(_all_labels(child, depth + 1))
    return found


def _match_value(value: Any, wanted: set[str], exact: bool) -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            folded = _fold_key(key)
            hit = folded in wanted if exact else any(candidate in folded for candidate in wanted)
            if hit and child not in (None, "") and not isinstance(child, (dict, list)):
                return child
        for child in value.values():
            found = _match_value(child, wanted, exact)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _match_value(child, wanted, exact)
            if found not in (None, ""):
                return found
    return None


def _find_value(value: Any, keys: set[str], exact_only: set[str] = frozenset()) -> Any:
    """The first value whose label names one of ``keys``.

    Two passes. An exact folded match first, so a payload carrying both
    "Department" and "Department Code" binds to the one that means it. Then a
    substring pass, because SAIS labels its student card in prose — "Bölüm
    Adı", "Program/Bölüm" — and an exact match reads those as absent, which is
    how the department came back empty for every student while the surname
    beside it was found.

    ``exact_only`` joins the first pass and sits out the second. It is for a
    label that is right on its own but is a prefix of a decoy: "Program" means
    the programme, while "Program Type" — whose value is "MAJOR" — would answer
    the substring pass first and store the word MAJOR as the student's
    department.
    """
    wanted = {_fold_key(key) for key in keys if _fold_key(key)}
    strict = wanted | {_fold_key(key) for key in exact_only if _fold_key(key)}
    if not strict:
        return None
    found = _match_value(value, strict, exact=True)
    if found not in (None, ""):
        return found
    return _match_value(value, wanted, exact=False) if wanted else None


# The weekly schedule packs the course code, the section and the room into a
# single cell — "2360130 - 1 - P1" — and none of the keys the transcript uses
# appear on those rows. Every enrolled course was therefore fetched from SAIS
# and then dropped on the floor: the snapshot stored zero of them while
# ``sais_get_schedule`` was answering perfectly well.
_CODE_SECTION_ROOM = re.compile(r"^\s*(\d{6,7})\s*-\s*([^\s-]+)\s*(?:-\s*(.*?))?\s*$")


def _course_rows(value: Any) -> list[dict]:
    rows: list[dict] = []
    if isinstance(value, dict):
        normalized = {str(key).lower(): child for key, child in value.items()}
        code = next(
            (
                normalized[key]
                for key in ("course_code", "code", "ders_kodu", "course")
                if key in normalized and normalized[key]
            ),
            None,
        )
        combined_section = combined_room = None
        if not code:
            for key in ("code_section_room", "code_section", "ders_sube"):
                match = _CODE_SECTION_ROOM.match(str(normalized.get(key) or ""))
                if match:
                    code, combined_section, combined_room = match.group(1), match.group(2), match.group(3)
                    break
        if code:
            row = {"course_code": "".join(str(code).upper().split())}
            grade = next(
                (normalized[key] for key in ("grade", "letter_grade", "not") if normalized.get(key)), None
            )
            section = next(
                (normalized[key] for key in ("section", "sube") if normalized.get(key)), combined_section
            )
            credits = next(
                (normalized[key] for key in ("credits", "credit", "local_credit") if normalized.get(key) is not None),
                None,
            )
            if grade:
                # The scrape brings the rest of the cell with it: a real
                # value carries the grade, then whitespace, a newline and a
                # trailing note such as "(NTE)". Only the letter grade is
                # kept, or every later comparison is against noise.
                row["grade"] = grade_token(grade)
            if section is not None:
                row["section"] = str(section)
            if credits is not None:
                row["credits"] = credits
            for field, keys in (
                ("name", ("course_name", "name", "ders_adi")),
                ("day", ("day", "gun")),
                ("hour", ("hour", "time", "saat")),
                ("room", ("classroom", "room", "derslik")),
                ("instructor", ("instructor", "instructors", "ogretim_uyesi")),
            ):
                found = next((normalized[key] for key in keys if normalized.get(key)), None)
                if found is not None:
                    row[field] = " ".join(str(found).split())
            if combined_room and "room" not in row:
                row["room"] = combined_room
            rows.append(row)
        else:
            for child in value.values():
                rows.extend(_course_rows(child))
    elif isinstance(value, list):
        for child in value:
            rows.extend(_course_rows(child))
    # One row per course-and-section, carrying every meeting it has. A course
    # meets three or four times a week and each meeting arrives as its own
    # schedule row; keeping the last one and discarding the rest left a record
    # saying the calculus lecture is on Wednesday when it is also on Monday.
    unique: dict[tuple[str, str | None, str | None], dict] = {}
    for row in rows:
        key = (row["course_code"], row.get("section"), row.get("grade"))
        # This runs at every level of the recursion, so a row arriving from a
        # nested call has already had its own meeting folded into "meetings".
        # Reading only the loose fields therefore merged nothing at all and
        # kept whichever meeting happened to come first.
        meeting = {field: row.pop(field) for field in ("day", "hour", "room") if field in row}
        incoming = row.pop("meetings", [])
        if meeting:
            incoming = [meeting, *incoming]
        existing = unique.get(key)
        if existing is None:
            if incoming:
                row["meetings"] = incoming
            unique[key] = row
            continue
        merged = existing.setdefault("meetings", [])
        for item in incoming:
            if item not in merged:
                merged.append(item)
    return list(unique.values())


def _number(value: Any, default: float = 0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


# Labels SAIS is known to use for each field of the student card. Folded before
# comparison, so "Bölüm" and "bolum" are the same key here.
# The English card has no department field at all — the department is the
# right-hand half of "Program Code / Name". These are for the Turkish card and
# for whatever else SAIS may return; the program field is the real source.
_DEPARTMENT_KEYS = {"department", "bölüm", "bolum"}
_DEGREE_KEYS = {"degree_level", "level", "program_level", "derece", "seviye", "ogrenim"}
# Deliberately precise. SAIS's English card carries "Program Code / Name"
# *and* "Program Type" (whose value is "MAJOR"), and a loose "program"
# substring would bind to whichever the page happened to list first — storing
# "MAJOR" as the student's department roughly half the time. Every key here
# folds to something no decoy label contains. If SAIS renames the field, the
# sais_student_info_labels line says so on the next refresh.
_PROGRAM_KEYS = {"program code / name", "program_code", "program kodu", "program kodu / adı"}
# Matched exactly and never as a substring — see ``_find_value``. A card whose
# only programme field is labelled plainly is read, without "Program Type"
# being read along with it.
_PROGRAM_EXACT_KEYS = {"program", "programme", "programı"}
_CAMPUS_KEYS = {"campus", "yerleşke", "yerleske", "kampüs", "kampus"}
# "Adı Soyadı" is as common a label as "Soyadı", and folds to a superstring of
# it, so both land here and _surname_prefix takes the last token of whichever
# one matched.
_SURNAME_KEYS = {"surname", "soyadı", "soyadi", "soyad", "lastname", "last_name", "soyisim"}

# The five values the API and the settings form accept. SAIS writes this field
# in Turkish prose, and storing that prose verbatim is not merely untidy: the
# settings form echoes whatever it was given straight back into the PUT that
# validates against this vocabulary, so a stored "Lisans" makes every later
# save fail with 422 and the student cannot edit their own profile.
# Ordered longest-intent-first: "yüksek lisans" must be read as a master's
# before the "lisans" inside it is read as a bachelor's.
_DEGREE_WORDS: tuple[tuple[str, str], ...] = (
    ("yukseklisans", "masters"),
    ("master", "masters"),
    ("msc", "masters"),
    ("doktora", "doctoral"),
    ("doctoral", "doctoral"),
    ("phd", "doctoral"),
    ("degisim", "exchange"),
    ("exchange", "exchange"),
    ("erasmus", "exchange"),
    ("onlisans", "other"),
    ("lisans", "undergraduate"),
    ("undergraduate", "undergraduate"),
    ("bachelor", "undergraduate"),
)


def _text(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


def _degree_level(value: Any) -> str | None:
    """Map SAIS's wording onto the vocabulary the rest of the app accepts.

    Anything unrecognised is dropped rather than stored, because a value
    outside the vocabulary is worse than no value at all: it displays as blank
    and it breaks the student's next save.
    """
    folded = _fold_key(_text(value) or "")
    if not folded:
        return None
    for word, level in _DEGREE_WORDS:
        if word in folded:
            return level
    return None


# A programme code: "567", "5710101", occasionally with a trailing letter.
_PROGRAM_CODE = re.compile(r"[0-9]{2,7}[A-Za-z]?")


def _program_code_and_department(value: Any) -> tuple[str | None, str | None]:
    """Split SAIS's combined programme field into its code and its name.

    The card returns one string — "567/Electrical and Electronics Engineering"
    — carrying both. Read whole it is neither: too long for the 32-character
    code column, and unresolvable as a department name. That is the whole
    reason the department came back empty while every other field arrived.

    Split on the first separator only, and only when the left side actually
    looks like a code; a programme whose name contains a slash is otherwise
    read as a code plus a truncated name.
    """
    text = _text(value)
    if not text:
        return None, None
    code, separator, name = text.partition("/")
    code, name = code.strip(), name.strip()
    if not separator:
        # No separator and nothing but digits: a bare programme code. Falling
        # through would have returned it as the department name, and a student
        # whose card carries only the code would be stored as studying "571".
        return (text, None) if _PROGRAM_CODE.fullmatch(text) else (None, text)
    if not _PROGRAM_CODE.fullmatch(code):
        return None, text
    return (code or None), (name or None)


def _program_code(value: Any) -> str | None:
    """A programme code the column can actually hold, or nothing.

    A name cut off at 32 characters is not a code — it is a truncated sentence
    nobody can match on — so an over-long value is dropped rather than stored.
    """
    text = _text(value)
    return text if text and len(text) <= 32 else None


# "Bachelor's (2nd year) / 4" — the year in prose, the semester number after
# the slash. Both are read: the prose is what the registrar states, and the
# semester is a check when the wording is one this does not recognise.
_YEAR_IN_TEXT = re.compile(r"(\d+)\s*(?:st|nd|rd|th)?\s*(?:year|yil|sinif)", re.IGNORECASE)
_SEMESTER_TAIL = re.compile(r"/\s*(\d+)\s*$")


def _year_of_study(value: Any) -> int | None:
    """Which year of their programme the student is in, 1-9.

    A section's eligibility row compares its year band against this. Returning
    None when the wording is unfamiliar is deliberate: the comparator skips a
    dimension it has no value for, and not checking the year is much cheaper
    than checking it against a number we guessed.
    """
    text = _text(value) or ""
    if not text:
        return None
    match = _YEAR_IN_TEXT.search(text)
    if match:
        year = int(match.group(1))
        return year if 1 <= year <= 9 else None
    # Two semesters to a year, so semester 4 is a second-year student.
    tail = _SEMESTER_TAIL.search(text)
    if tail:
        semester = int(tail.group(1))
        if 1 <= semester <= 20:
            return (semester + 1) // 2
    return None


async def _apply_student_info(db: AsyncSession, user_id: UUID, student_info: Any) -> None:
    """Store the student card SAIS returned as their verified academic context.

    The labels it arrived under are logged — key names only, never their values
    — because when a field comes back empty the one question worth answering is
    what SAIS actually called it, and after the payload is discarded that is
    unrecoverable. Guessing it cost four rounds of deploys once already.
    """
    program_code, program_department = _program_code_and_department(
        _find_value(student_info, _PROGRAM_KEYS, _PROGRAM_EXACT_KEYS)
    )
    # A department field of its own wins; otherwise the name half of the
    # programme field, which is where the English card keeps it.
    department = _text(_find_value(student_info, _DEPARTMENT_KEYS)) or program_department
    degree_level = _degree_level(_find_value(student_info, _DEGREE_KEYS))
    program_code = _program_code(program_code)
    campus = _text(_find_value(student_info, _CAMPUS_KEYS))
    surname_prefix = _surname_prefix(_find_value(student_info, _SURNAME_KEYS))
    # Same field the degree level comes from: "Bachelor's (2nd year) / 4".
    year_of_study = _year_of_study(_find_value(student_info, _DEGREE_KEYS))
    logger.info(
        "sais_student_info_labels",
        user_id=str(user_id),
        labels=sorted(set(_all_labels(student_info)))[:40],
        found=[
            name
            for name, value in (
                ("department", department),
                ("degree_level", degree_level),
                ("program_code", program_code),
                ("campus", campus),
                ("surname_prefix", surname_prefix),
                ("year_of_study", year_of_study),
            )
            if value
        ],
    )
    await apply_verified_context(
        db,
        user_id,
        department=department,
        degree_level=degree_level,
        program_code=program_code,
        campus=campus,
        surname_prefix=surname_prefix,
        year_of_study=year_of_study,
    )


# METU codes a term as five digits: the starting year and the part number,
# so 20252 is the spring of 2025-2026 and 20261 the following autumn.
_TERM_CODE = re.compile(r"(?<![0-9])(20[0-9]{2}[1-3])(?![0-9])")


def _term_code(value: Any) -> str | None:
    """The five-digit term code inside a value, if there is exactly one."""
    match = _TERM_CODE.search(str(value or ""))
    return match.group(1) if match else None


def _schedule_term(schedule: Any) -> str | None:
    """The term a weekly schedule says it is for.

    The explicit code fields are read before the prose ones. The payload lists
    ``"semester": "2025-2026 Spring - MAJOR"`` *before* ``"semester_code":
    "20252|1"``, and only the second carries a code — so a single lookup over
    both finds the prose first, extracts nothing, and concludes it has no
    opinion about a term it was told outright.
    """
    for keys in ({"semester_code", "term_code"}, {"semester", "term", "donem"}):
        code = _term_code(_find_value(schedule, keys))
        if code:
            return code
    return None


def _schedule_matches_term(schedule: Any, term: str) -> bool:
    """Whether a weekly schedule actually belongs to the term we asked about.

    SAIS answers ``get_schedule`` with whatever it last registered the student
    for and says so — ``"semester_code": "20252|1"`` — rather than refusing a
    term it has nothing for. Storing that against the requested term reported a
    student as enrolled in three courses for a term they have not registered
    for at all, which is the difference between "you have three" and "you have
    none" on the screen they check before registration.

    Unknown on either side means no opinion, and the schedule is kept: this
    guards against a term mismatch, not against an unfamiliar payload.
    """
    wanted, got = _term_code(term), _schedule_term(schedule)
    return not (wanted and got) or wanted == got


async def refresh_from_sais(
    user_id: UUID, term: str, connected: list[MCPTools], *, optional_timeout: float | None = None
) -> bool:
    """Store what SAIS will give us. ``False`` only when it gave nothing at all.

    The student card is read *first* and stored on its own. It is the cheapest
    call and the one the planner and chat depend on most — department, campus,
    surname prefix — and it used to be read last, behind the transcript and the
    weekly schedule. Either of those failing therefore cost the student their
    department, which is exactly what happened: ``get_schedule`` raises inside
    the SAIS server for some accounts, and the department step never ran.

    Each call is independent now. An *empty* transcript is still an answer, not
    a failure — a first-semester student has no completed courses — so only
    getting nothing from any call at all returns False.
    """
    # Retried: this is the one call whose result the planner, the chat tools
    # and the schedule page all depend on, and it is the cheapest to repeat.
    student_info = await _payload(
        _function(connected, "sais_get_student_info"), tool="sais_get_student_info", attempts=2
    )
    if student_info is not None:
        async with SessionLocal() as db:
            await _apply_student_info(db, user_id, student_info)

    transcript_fn = _function(connected, "sais_get_transcript")
    transcript = await _payload(transcript_fn, tool="sais_get_transcript") if transcript_fn else None
    if transcript is None:
        # No transcript is survivable when the card came through: the student
        # keeps a usable department instead of an error and nothing stored.
        return student_info is not None

    # The weekly schedule is the least valuable thing here and reliably the
    # slowest: METU regularly takes longer to render it than the campus server
    # will wait, and those thirty seconds used to be spent on every refresh
    # before the student got an answer they already had after four. Bounded
    # where the caller says it is safe to abandon.
    schedule = await _payload(
        _function(connected, "sais_get_schedule"), tool="sais_get_schedule", timeout=optional_timeout
    )
    completed = [row for row in _course_rows(transcript) if row.get("grade")]
    # None where a read did not happen, so the store keeps what it already has.
    # A schedule we could not read is not the same as a term with no classes in
    # it, and only one of those should clear the stored week.
    enrolled: list[dict] | None
    if schedule is None:
        enrolled = None
    else:
        enrolled = _course_rows(schedule)
        if enrolled and not _schedule_matches_term(schedule, term):
            logger.info(
                "sais_schedule_term_mismatch",
                requested=term,
                returned=_schedule_term(schedule),
                dropped=len(enrolled),
            )
            enrolled = []
    credits = _number(
        _find_value(transcript, {"total_credits", "completed_credits", "credits_completed", "toplam_kredi"})
    )
    cgpa = _number(_find_value(transcript, {"cgpa", "cumulative_gpa", "gpa", "genel_not_ortalamasi"}))
    async with SessionLocal() as db:
        await upsert_academic_snapshot(
            db,
            user_id,
            term,
            # A transcript that parsed to nothing is a failed parse, not a
            # student with no history, so it is reported as "not read".
            completed_courses=completed or None,
            enrolled_courses=enrolled,
            # Both default to zero when the figure cannot be found, and a zero
            # CGPA written over a real one is the same silent loss.
            current_credits=credits or None,
            current_grade_points=(credits * cgpa) or None,
            source="sais",
        )
    logger.info(
        "sais_refresh_complete",
        has_student_info=student_info is not None,
        completed_courses=len(completed),
        enrolled_courses=len(enrolled),
    )
    return True


@asynccontextmanager
async def _campus_session(user_id: UUID) -> AsyncIterator[list[MCPTools]]:
    """Lease only SAIS, checking current consent before each refresh."""
    from app.campus.sessions import integration_session
    async with integration_session(user_id, "sais") as connected:
        yield connected


async def sync_student_context_from_sais(user_id: UUID) -> bool:
    """Fill the student's verified academic context right after they connect.

    :func:`refresh_from_sais` only runs inside a turn, when the model calls a
    planning tool, so a student who connected SAIS and opened their profile saw
    an empty academic context until they happened to ask a planning question.

    Reads ``sais_get_student_info`` alone — department, degree level, program
    code, campus, which is exactly what the profile shows. The transcript is
    deliberately not read here: it needs a term this path has no way to choose,
    and it is private data the profile never displays. The stored context is
    marked verified but unconfirmed, so the student still confirms it before
    anything uses it.
    """
    async with _campus_session(user_id) as connected:
        student_info = await _payload(
            _function(connected, "sais_get_student_info"), tool="sais_get_student_info", attempts=2
        )
        if student_info is None:
            return False
        async with SessionLocal() as db:
            await _apply_student_info(db, user_id, student_info)
    logger.info("student_context_synced", user_id=str(user_id))
    return True


async def sync_planning_snapshot_from_sais(user_id: UUID, term: str) -> bool:
    """Refresh the existing deterministic planner without starting an agent run.

    This session exists only for this call and is closed on the way out, so the
    optional reads may be abandoned rather than allowed to run out the clock on
    a student waiting for an answer.
    """
    async with _campus_session(user_id) as connected:
        return await refresh_from_sais(
            user_id, term, connected, optional_timeout=get_settings().sais_optional_read_seconds
        )

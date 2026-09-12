"""Deterministic intent classification, context selection and answer guidance.

A turn gets the whole tool surface and the student's context either way; what it
does not get is a stable shape for the answer, so two questions of the same kind
come back in two different formats - one prerequisite answer was a table and the
next was prose with a typo in it.

The classifier is deliberately keyword-based. A model call to classify the
intent would cost the latency and tokens this exists to save, and the intents
below are the ones the tools already answer deterministically; anything the
patterns do not recognise stays "other" and keeps the full context.
"""

import re

# Turkish letters folded the way a student types on an English keyboard, so
# "ön koşul" and "on kosul" reach the same rule.
_FOLD = str.maketrans(
    {
        "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
        "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
    }
)


def _fold(text: str) -> str:
    return str(text or "").translate(_FOLD).casefold()


# Ordered: the earlier a rule matches, the more specific it is. "EE 201'in ön
# koşulu nedir" is a prerequisite question, not a knowledge question, and
# "planımdaki dersler" is a schedule question, not a credits one. Needles are
# written folded, and the schedule ones are narrow on purpose: "program" alone
# would read "Python programlama nedir" as a scheduling question.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("memory", ("hatirla", "unutma", "aklinda tut", "remember")),
    ("mail", ("mail", "e-posta", "eposta", "email", "gelen kutusu", "inbox")),
    ("announcements", ("duyuru", "announcement", "ilan")),
    ("prerequisites", ("on kosul", "prerequisite", "prereq", "kosulu")),
    ("eligibility", ("uygun", "alabilir", "kisit", "eligible", "kayit olabil")),
    ("sections", ("sube", "section", "hoca", "ogretim uyesi")),
    ("credits", ("kredi", "akts", "ects", "credit")),
    ("schedule", ("ders program", "programi", "planim", "planla", "takvim", "cakis", "hafta", "schedule", "timetable")),
    ("knowledge", ("yonetmelik", "nedir", "nasil", "neden", "kural")),
    ("greeting", ("merhaba", "selam", "hello")),
)

_GUIDANCE = {
    "prerequisites": (
        "Answer shape for this question: one line per alternative group, each requirement written as its "
        "`course_label` with the minimum grade, then one short sentence that any single group is enough. "
        "Two groups read well as a two-column table."
    ),
    "credits": (
        "Answer shape for this question: the course label with its local credits and ECTS on one line; add "
        "prerequisites or sections only if asked."
    ),
    "sections": (
        "Answer shape for this question: a table of section, instructor and day/time, or one sentence that "
        "the times are not published yet; keep restrictions to a single line."
    ),
    "eligibility": (
        "Answer shape for this question: the verdict first (eligible, not eligible, or unknown), then the "
        "exact rule that decides it; never infer from grades stated in chat."
    ),
    "schedule": (
        "Answer shape for this question: the student's planned week as one short line per course; do not "
        "substitute the registered SAIS schedule."
    ),
    "mail": (
        "Answer shape for this question: what was found, one line each (sender, subject, date); anything "
        "that sends waits for explicit confirmation."
    ),
    "announcements": (
        "Answer shape for this question: newest first, one line each with its date; say when the list may "
        "be incomplete."
    ),
    "knowledge": (
        "Answer shape for this question: the answer in one short paragraph, then the source and when it was "
        "read."
    ),
    "memory": (
        "Answer shape for this question: persist it first (read my.memory, then update the whole list), and "
        "only then confirm in one sentence exactly what will be remembered. Never say it is remembered "
        "without the successful update; do not narrate the storing itself."
    ),
    "greeting": (
        "Answer shape for this question: two or three lines at most; name one thing you can help with that "
        "matches the student's own plan or department."
    ),
}

# Fields every turn gets: cheap, and they personalise the answer.
_ALWAYS = frozenset(
    {
        "display_name",
        "department",
        "academic_identity",
        "explicit_memories",
        "benign_preferences",
        "locale",
        "current_focus",
    }
)
# The planner week is half a kilobyte, and it is what makes "what is on my week"
# answerable without a tool call - so it stays for questions that touch the week.
_TIMETABLE_INTENTS = frozenset({"schedule", "prerequisites", "credits", "sections", "eligibility", "greeting"})
# A one-line list of connected campus servers, only where a campus tool may be reached.
_CAMPUS_INTENTS = frozenset({"sections", "eligibility", "schedule", "announcements", "mail", "knowledge"})
# Questions that name a term or a moment. `academic_term_hint` is what says
# 20261 is Fall; without it one run labelled the term "2026-2026 Bahar".
_TIME_INTENTS = frozenset({"schedule", "prerequisites", "credits", "sections", "eligibility", "announcements"})


def classify(message: str) -> str:
    """The turn's intent, or "other" when no rule matches."""
    folded = _fold(message)
    if not folded.strip():
        return "other"
    for intent, needles in _RULES:
        if any(needle in folded for needle in needles):
            return intent
    return "other"


def context_fields(intent: str) -> frozenset[str] | None:
    """The dependency fields this intent justifies, or None for all of them.

    A field earns its place only when removing it would make the model call a
    tool for something it could have been given - every field is re-sent on
    every later model step of the turn.
    """
    if intent == "other":
        return None
    fields = set(_ALWAYS)
    if intent in _TIMETABLE_INTENTS:
        fields.add("planned_timetable")
    if intent in _CAMPUS_INTENTS:
        fields.add("enabled_tools")
    if intent in _TIME_INTENTS:
        fields.add("local_datetime")
        fields.add("academic_term_hint")
    return frozenset(fields)


def guidance(intent: str) -> str | None:
    return _GUIDANCE.get(intent)


_COURSE_CODE = re.compile(r"\b([A-Za-zÇĞİÖŞÜçğıöşü]{2,6})\s?-?\s?(\d{3,4})\b")
_FULL_CODE = re.compile(r"\b(\d{7})\b")


def current_focus(message: str | None) -> dict | None:
    """The courses the student just named, so "onun/peki" has an antecedent.

    Deterministic on purpose: it is one small field, and a model call to find it
    would cost more than the reference it resolves.
    """
    if not message:
        return None
    codes: list[str] = []
    for match in _COURSE_CODE.finditer(message):
        codes.append(f"{match.group(1).upper()} {match.group(2)}")
    for match in _FULL_CODE.finditer(message):
        codes.append(match.group(1))
    if not codes:
        return None
    seen: list[str] = []
    for code in codes:
        if code not in seen:
            seen.append(code)
    return {"courses": seen[-3:]}

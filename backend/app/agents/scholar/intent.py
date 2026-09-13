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
# Each needle is matched at a word start, so "ilan" does not fire on "açılan"
# (inside a sections question) and "sube" still fires on "şubeleri". Turkish
# suffixes attach to the end of a word, which is why the boundary is only at
# the start.
_RULES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (intent, tuple(re.compile(r"\b" + re.escape(needle)) for needle in needles))
    for intent, needles in (
        ("memory", ("hatirla", "unutma", "aklinda tut", "remember")),
        ("mail", ("mail", "e-posta", "eposta", "email", "gelen kutusu", "inbox")),
        ("announcements", ("duyuru", "announcement", "ilan", "guncelleme", "etkinlik", "event")),
        ("prerequisites", ("on kosul", "prerequisite", "prereq", "kosulu")),
        ("eligibility", ("uygun", "alabilir", "kisit", "eligible", "kayit olabil")),
        ("sections", ("sube", "section", "hoca", "ogretim uyesi")),
        ("credits", ("kredi", "akts", "ects", "credit")),
        (
            "schedule",
            ("ders program", "programi", "planim", "planla", "takvim", "cakis", "hafta", "schedule", "timetable"),
        ),
        ("knowledge", ("yonetmelik", "nedir", "nasil", "neden", "kural")),
        ("greeting", ("merhaba", "selam", "hello")),
    )
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
        "the times are not published yet; keep restrictions to a single line. When the result carries "
        "sections_omitted, say how many sections the course has in total, show the ones you have, and ask "
        "whether the student wants all of them or one that fits a specific need (instructor, day, surname "
        "range). `resource.expand` is only honored when the student asked for everything in this message, "
        "or you offered it in the previous reply and they took the offer."
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
        "Answer shape for this question: read my.updates for the personalized feed - it carries campus "
        "announcements and events; use student.announcements only when the student means their own SAIS "
        "board. Show at most five newest items, one line each with its date, and how many more there are. "
        "When the student names a category or topic (events are type event), filter the list by type, "
        "title and summary and show only the matches. `resource.expand` is only honored when the student "
        "asked for everything in this message, or you offered it in the previous reply."
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
    for intent, patterns in _RULES:
        if any(pattern.search(folded) for pattern in patterns):
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

# A five-digit METU term code (20261 = 2026-2027 Fall). A term named only in
# words - "2026 güz", "gelecek dönem" - cannot be mapped to a code here, and
# reading the active term for a question about another one is worse than not
# reading at all, so it is reported unresolved and the caller skips.
# Term codes are commonly written with Turkish case suffixes (``20252'de`` or
# ``20252de``). Numeric guards keep a five-digit code from being pulled out of
# a longer identifier while allowing those attached suffixes.
_TERM_CODE = re.compile(r"(?<!\d)20\d{3}(?!\d)")
_TERM_WORDS = re.compile(
    r"(?:"
    # A season by itself is enough for the unresolvable guard, except for
    # ``yaz``: in Turkish it is also the imperative of "to write" ("ön koşulu
    # yaz").  The summer forms below require a noun/season suffix or a
    # relative qualifier, so that imperative is not mistaken for a term.
    r"\b(?:guz|bahar|fall|spring|summer)(?:da|de|daki|deki|in|ın|un|ün|a|e|i|ı|u|ü)?\b|"
    r"\b(?:gelecek|gecen|onceki|sonraki|onumuzdeki|bir sonraki)\s+"
    r"(?:donem|yariyil|yil)(?:de|da|daki|deki|ki|in|e|den|inde|indeki)?\b|"
    r"\b(?:bu|gelecek|gecen|onceki|sonraki|onumuzdeki|bir sonraki)\s+"
    r"(?:summer|yaz)\b|"
    r"\b(?:yaz)(?:da|de|daki|deki)\b|"
    r"\b(?:yaz)\s+(?:donem|yariyil|okulu|semester|term)"
    r"(?:de|da|daki|deki|ki|in|e|den|inde|indeki)?\b|"
    r"\b(?:next|last|previous|following|coming)\s+"
    r"(?:term|semester|academic\s+year)\b|"
    r"\b(?:gelecek|gecen|onceki|sonraki|onumuzdeki)\s+donem"
    r"(?:de|da|daki|deki|ki|in|e|den|inde|indeki)?\b"
    r")"
)
_YEAR = re.compile(r"\b20\d{2}\b")

# "şube 2", "2. şube", "section-3". Before-the-word matches require the
# punctuation, so the course number in "EE 201 şubesi" is not read as a
# section; "şubeleri" (plural, no number) resolves to no section.
_SECTION_AFTER = re.compile(r"(?:sube|section)\s*[.:#/-]?\s*(\d{1,3})")
_SECTION_BEFORE = re.compile(r"(?<![a-z0-9])(\d{1,3})\s*[.:#/-]\s*(?:sube|section)")
_SECTION_WORD = re.compile(r"\b(?:sube|section)")


# "hepsini göster", "tamamını çıkar", "tümünü listele", "show all". Both the
# student's request and the assistant's offer use these, which is what lets the
# broker tell "they asked for everything" from "the model decided to fetch
# everything on its own".
_EVERYTHING = re.compile(
    r"\b(hepsi|hepsini|tamami|tamamini|tumu|tumunu|butun|butununu|"
    r"all of them|all of it|everything|show all|the rest)"
)

_NEGATED_EVERYTHING = re.compile(
    r"(?:"
    r"\b(?:hepsi|hepsini|tamami|tamamini|tumu|tumunu|butun|butununu|"
    r"all(?:\s+of\s+(?:them|it))?|everything|the\s+rest)\b"
    r"(?:[^.!?\n]{0,32})?\b(?:istemiyorum|istemeyin|gosterm(?:e|eyin|eyelim)|"
    r"cikarma|listeleme|gerek\s+yok|no|not|don't|dont|do\s+not)\b|"
    r"\b(?:hayir|hayır|no|not|don't|dont|do\s+not)\b"
    r"(?:[^.!?\n]{0,32})?\b(?:hepsi|hepsini|tamami|tamamini|tumu|tumunu|butun|butununu|"
    r"all(?:\s+of\s+(?:them|it))?|everything|the\s+rest)\b"
    r")",
    re.IGNORECASE,
)
_CONDITIONAL_OFFER = re.compile(
    r"(?:\b(?:istersen|dilersen|ister\s+misin|if\s+you\s+want|would\s+you\s+like|"
    r"i\s+can|can\s+show|i\s+could|show\s+you)\b|"
    r"\b(?:gosterebilirim|gostereyim|listeleyebilirim|listeleyeyim|"
    r"cikarabilirim|cikarayim)\b)",
    re.IGNORECASE,
)
_AFFIRMATIVE = re.compile(
    r"^(?:"
    r"(?:evet|tamam|olur|peki|lutfen|yes|okay|ok|sure|please|go\s+ahead|do\s+it|devam(?:\s+et)?)"
    r"(?:[\s,;:!?-]+(?:goster|listele|cikar|devam\s+et|lutfen|please|go\s+ahead|do\s+it))?|"
    r"(?:goster|listele|cikar|devam\s+et)"
    r")[\s.!?]*$",
    re.IGNORECASE,
)
_LIMITED_REQUEST = re.compile(
    r"\b(?:sadece|yalnizca|yalnızca|ilk|son|birkaç|birkac|some|only|just|"
    r"one|few|top\s+\d+)\b",
    re.IGNORECASE,
)


def wants_everything(message: str | None) -> bool:
    """Whether a message asks for (or offers) every item of a list."""
    if not message:
        return False
    folded = _fold(message)
    return bool(_EVERYTHING.search(folded)) and not bool(_NEGATED_EVERYTHING.search(folded))


def _explicit_everything_request(message: str | None) -> bool:
    """Whether the latest user message positively asks for the full list.

    ``wants_everything`` is intentionally broad because it is also useful when
    recognizing the wording of an assistant offer. This stricter predicate is
    the API authorization boundary: conditional offers, negations and bounded
    requests do not grant ``resource.expand``.
    """
    if not message:
        return False
    folded = _fold(message)
    if _NEGATED_EVERYTHING.search(folded) or _LIMITED_REQUEST.search(folded):
        return False
    if _CONDITIONAL_OFFER.search(folded):
        return False
    return bool(_EVERYTHING.search(folded))


def _offers_everything(message: str | None) -> bool:
    """Whether an assistant message explicitly offers the complete list."""
    if not message:
        return False
    folded = _fold(message)
    return bool(_EVERYTHING.search(folded) and _CONDITIONAL_OFFER.search(folded)) and not bool(
        _NEGATED_EVERYTHING.search(folded)
    )


def _accepts_everything_offer(message: str | None) -> bool:
    """Whether a user affirmatively accepts a previously stated offer."""
    if not message:
        return False
    folded = _fold(message).strip()
    if _NEGATED_EVERYTHING.search(folded) or _LIMITED_REQUEST.search(folded):
        return False
    # An explicit full-list request is stronger than a short acceptance token.
    return _explicit_everything_request(folded) or bool(_AFFIRMATIVE.fullmatch(folded))


def expand_allowed_for_turn(latest_user: str | None, previous_assistant: str | None = None) -> bool:
    """Return the user-granted expansion capability for one chat turn.

    The assistant can describe or offer a full list, but its text is never
    itself consent. A new turn needs either an affirmative full-list request or
    a positive latest-user acceptance of an explicit prior offer. In particular,
    a negated latest message always wins over text quoted from the assistant.
    """
    if _explicit_everything_request(latest_user):
        return True
    return _offers_everything(previous_assistant) and _accepts_everything_offer(latest_user)


def requested_scope(message: str | None) -> dict:
    """The term and section a message names, and whether either is unresolvable.

    The turn forwards these to a prefetch so "20252'de EE 201" reads 20252 and
    not silently the active term, and "EE 201 şube 2 uygun mu" reads section 2
    instead of the whole course. When a term or section is named in a form this
    cannot resolve, the caller skips the prefetch rather than guessing.
    """
    if not message:
        return {"term": None, "section": None, "term_unresolved": False, "section_unresolved": False}
    folded = _fold(message)
    term_match = _TERM_CODE.search(folded)
    term = term_match.group(0) if term_match else None
    section_match = _SECTION_AFTER.search(folded) or _SECTION_BEFORE.search(folded)
    section = section_match.group(1) if section_match else None
    term_unresolved = term is None and bool(_TERM_WORDS.search(folded) or _YEAR.search(folded))
    return {
        "term": term,
        "section": section,
        "term_unresolved": term_unresolved,
        "section_unresolved": section is None and bool(_SECTION_WORD.search(folded)),
    }


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

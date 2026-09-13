"""What an answer claims, checked against what the run actually did.

The instructions forbid claiming a save that never happened and narrating
internal machinery, and the model still does both occasionally. These
deterministic checks make violations countable, and let the broker hold only an
incomplete or unsupported claim sentence long enough to verify it. The rest of
the answer keeps streaming normally.
"""

import json
import re
from collections.abc import Mapping

_CLAIM_OPERATIONS = {
    "update": frozenset(
        {
            "kaydettim",
            "kaydetti",
            "kaydedildi",
            "kayıt ettim",
            "kayit ettim",
            "kayıt edildi",
            "kayit edildi",
            "ekledim",
            "ekledi",
            "eklendi",
            "olusturdum",
            "oluşturdum",
            "olusturdu",
            "oluşturdu",
            "olusturuldu",
            "oluşturuldu",
            "guncelledim",
            "güncelledim",
            "guncelledi",
            "güncelledi",
            "guncellendi",
            "güncellendi",
            "saved",
            "updated",
            "created",
        }
    ),
    "undo": frozenset(
        {"sildim", "sildi", "silindi", "deleted", "reverted", "undid", "geri aldım", "geri aldim", "geri aldı"}
    ),
    "send_email": frozenset(
        {
            "gonderdim",
            "gönderdim",
            "gonderdi",
            "gönderdi",
            "gonderildi",
            "gönderildi",
            "sent",
            "delivered",
        }
    ),
}
_ENGLISH_CLAIMS = frozenset(
    {"saved", "updated", "created", "deleted", "reverted", "undid", "sent", "delivered"}
)

_CLAIMS = re.compile(
    r"\b(kaydettim|kaydetti|kaydedildi|kayıt ettim|kayit ettim|kayıt edildi|kayit edildi|"
    r"ekledim|ekledi|eklendi|guncelledim|güncelledim|guncelledi|güncelledi|"
    r"guncellendi|güncellendi|sildim|sildi|silindi|gonderdim|gönderdim|gonderdi|gönderdi|"
    r"gonderildi|gönderildi|olusturdum|oluşturdum|olusturdu|oluşturdu|"
    r"olusturuldu|oluşturuldu|geri aldım|geri aldim|geri aldı|"
    r"reverted|undid|saved|updated|sent|delivered|created|deleted)\b",
    re.IGNORECASE,
)
_GENERIC_SUCCESS = re.compile(
    r"\b(?:yapıldı|yapildi|tamamlandı|tamamlandi|gerçekleştirildi|gerceklestirildi)\b",
    re.IGNORECASE,
)

_CLAIM_PHRASES = frozenset(
    phrase.casefold()
    for phrases in _CLAIM_OPERATIONS.values()
    for phrase in phrases
) | frozenset(
    {
        "yapıldı",
        "yapildi",
        "tamamlandı",
        "tamamlandi",
        "gerçekleştirildi",
        "gerceklestirildi",
    }
)

# Prefixes that can become an affirmative English assertion once a claim verb
# arrives in a later model delta. Turkish finite claim verbs are detected from
# their own partial word; the nouns below cover the common object-first form
# ("Tercihini kaydettim") before that verb starts arriving.
_ASSERTION_SUBJECTS = (
    "i",
    "we",
    "email",
    "message",
    "draft",
    "preference",
    "change",
    "plan",
    "schedule",
    "timetable",
    "the email",
    "the message",
    "the draft",
    "the preference",
    "the change",
    "the plan",
    "the schedule",
    "the timetable",
    "tercih",
    "tercihin",
    "tercihini",
    "değişiklik",
    "degisiklik",
    "plan",
    "program",
    "takvim",
    "kayıt",
    "kayit",
    "e-posta",
    "eposta",
    "mail",
    "mesaj",
)
_ASSERTION_PREFIXES = frozenset(
    prefix
    for subject in _ASSERTION_SUBJECTS
    for prefix in (
        subject,
        f"{subject} have",
        f"{subject} has",
        f"{subject} have been",
        f"{subject} has been",
        f"{subject} was",
        f"{subject} were",
        f"{subject} is",
        f"{subject} are",
        f"{subject} just",
        f"{subject} already",
        f"{subject} successfully",
        f"{subject} was just",
        f"{subject} was already",
        f"{subject} was successfully",
        f"{subject} has just",
        f"{subject} has already",
        f"{subject} has successfully",
        f"{subject} has been successfully",
    )
)
_NEGATED_ASSERTION = re.compile(
    r"\b(?:not|never|no|didn['’]?t|did\s+not|don['’]?t|do\s+not|cannot|can['’]?t|"
    r"couldn['’]?t|could\s+not|degil|değil|hayir|hayır|demedim|söylemedim|soylemedim|"
    r"kaydedilmedi|güncellenmedi|guncellenmedi|silinmedi|gönderilmedi|gonderilmedi)\b",
    re.IGNORECASE,
)
_HISTORICAL_ASSERTION = re.compile(
    r"\b(?:yesterday|previously|earlier|ago|last\s+(?:week|term|semester|time)|"
    r"dün|dun|daha\s+önce|daha\s+once|önceden|onceden|geçen\s+(?:hafta|dönem)|"
    r"gecen\s+(?:hafta|donem))\b",
    re.IGNORECASE,
)
_QUOTED_TEXT = re.compile(r"`[^`]*`|\"[^\"]*\"|“[^”]*”|‘[^’]*’")
_CLAUSE_BOUNDARY = re.compile(
    r"\s*(?:;|,?\s*\b(?:and|then|but|however|ve|sonra|ardından|ardindan|ama|ancak|fakat)\b)\s*",
    re.IGNORECASE,
)
# Word-bounded, so "sorgulama" (ordinary Turkish for querying) is not jargon.
_JARGON = re.compile(
    r"\b(published release|veritaban\w*|şema|schema|system prompt|course_count)\b",
    re.IGNORECASE,
)


def _send_succeeded(result) -> bool:
    """Whether an email result proves delivery rather than approval/pending."""
    if isinstance(result, str):
        text = result.casefold().strip()
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError):
            return bool(re.fullmatch(r"(?:sent|success|succeeded|completed|delivered)(?: successfully)?", text))
        return _send_succeeded(parsed)
    if not isinstance(result, Mapping):
        return False
    if result.get("success") is False:
        return False
    status = str(result.get("status") or "").casefold().strip()
    if status in {"approval_required", "pending", "failed", "error", "unknown", "executing"}:
        return False
    if result.get("success") is True:
        return True
    if status in {"sent", "success", "succeeded", "completed", "delivered"}:
        return True
    return bool(result.get("message_id"))


def _completion(item) -> tuple[str | None, object, bool]:
    """Read a completion record while accepting the old string test shape."""
    if isinstance(item, str):
        return item, None, False
    if isinstance(item, Mapping):
        return (
            item.get("tool") or item.get("name") or item.get("tool_name"),
            item.get("result"),
            bool(item.get("tool_call_error")),
        )
    return (
        getattr(item, "tool", None) or getattr(item, "name", None) or getattr(item, "tool_name", None),
        getattr(item, "result", None),
        bool(getattr(item, "tool_call_error", False)),
    )


def completed_operations(completed_tools) -> set[str]:
    """Normalize successful tool completions to the mutation capabilities they prove."""
    operations: set[str] = set()
    for item in completed_tools or []:
        tool, result, failed = _completion(item)
        if failed:
            continue
        if tool == "update":
            operations.add("update")
        elif tool == "undo":
            operations.add("undo")
        elif tool in {"send_email", "webmail_send_email", "webmail_reply_email"} and _send_succeeded(result):
            operations.add("send_email")
    return operations


def completed_tool_names(completed_tools) -> list[str]:
    """Return names for privacy-safe audit fields without logging tool results."""
    return [tool for item in completed_tools or [] if (tool := _completion(item)[0])]


def _claim_operation(claim: str) -> str | None:
    folded = claim.casefold()
    for operation, claims in _CLAIM_OPERATIONS.items():
        if folded in {item.casefold() for item in claims}:
            return operation
    return None


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    sentence_start = max(text.rfind(mark, 0, start) for mark in ".!?\n") + 1
    candidates = [text.find(mark, end) for mark in ".!?\n" if text.find(mark, end) >= 0]
    sentence_end = min(candidates, default=len(text) - 1)
    if sentence_end < len(text):
        sentence_end += 1
    return sentence_start, sentence_end


def _inside_quote(sentence: str, start: int, end: int) -> bool:
    return any(match.start() <= start and end <= match.end() for match in _QUOTED_TEXT.finditer(sentence))


def _claim_clause(sentence: str, start: int, end: int) -> tuple[str, int, int]:
    boundaries = list(_CLAUSE_BOUNDARY.finditer(sentence))
    clause_start = max((match.end() for match in boundaries if match.end() <= start), default=0)
    clause_end = min((match.start() for match in boundaries if match.start() >= end), default=len(sentence))
    return sentence[clause_start:clause_end], start - clause_start, end - clause_start


def _is_affirmative_assertion(sentence: str, start: int, end: int, claim: str) -> bool:
    """Exclude questions, negations, quotations and adjectival English uses."""
    if sentence.rstrip().endswith("?"):
        return False
    if _inside_quote(sentence, start, end):
        return False
    sentence, start, end = _claim_clause(sentence, start, end)
    if _NEGATED_ASSERTION.search(sentence) or _HISTORICAL_ASSERTION.search(sentence):
        return False
    if claim.casefold() not in _ENGLISH_CLAIMS:
        # Turkish finite verb forms encode an asserted person/passive tense;
        # quoted, negated, historical and interrogative uses were excluded.
        return True

    before = sentence[:start].casefold()
    after = sentence[end:].casefold()
    if re.search(r"\b(?:i|we)(?:['’]ve|\s+have)?(?:\s+(?:just|already|successfully))?\s*$", before):
        return True
    if re.search(
        r"\b(?:was|were|is|are|has\s+been|have\s+been)(?:\s+(?:now|just|already|successfully))?\s*$",
        before,
    ):
        return True
    if re.search(
        r"\b(?:email|message|draft|preference|change|plan|schedule|timetable)"
        r"(?:\s+(?:just|already|successfully))?\s*$",
        before,
    ):
        return True
    leading = re.match(r"[\s\-*•#>:]*", sentence).end()
    if start != leading and before.strip() != "successfully":
        return False
    return bool(
        re.match(
            r"\s*(?:successfully\s*)?(?:[.!:;,-]|$|(?:your|it|this|that|the\s+(?:change|preference|plan|"
            r"schedule|timetable|email|message|draft))\b)",
            after,
        )
    )


def _generic_operation(sentence: str) -> str | None:
    folded = sentence.casefold()
    if re.search(r"\b(?:e-?posta|mail|mesaj)\b", folded):
        return "send_email"
    if re.search(r"\bgeri\s+alma\b", folded):
        return "undo"
    if re.search(r"\b(?:değişiklik|degisiklik|tercih|plan|program|takvim|kayıt|kayit)\b", folded):
        return "update"
    return None


def claim_matches(answer: str) -> list[tuple[str, str, int, int]]:
    """Return ``(text, operation, start, end)`` for each success claim."""
    matches = []
    for match in _CLAIMS.finditer(answer or ""):
        if _inside_quote(answer, match.start(), match.end()):
            continue
        operation = _claim_operation(match.group(0))
        sentence_start, sentence_end = _sentence_bounds(answer, match.start(), match.end())
        sentence = answer[sentence_start:sentence_end]
        if operation is not None and _is_affirmative_assertion(
            sentence,
            match.start() - sentence_start,
            match.end() - sentence_start,
            match.group(0),
        ):
            matches.append((match.group(0), operation, match.start(), match.end()))
    for match in _GENERIC_SUCCESS.finditer(answer or ""):
        if _inside_quote(answer, match.start(), match.end()):
            continue
        sentence_start, sentence_end = _sentence_bounds(answer, match.start(), match.end())
        sentence = answer[sentence_start:sentence_end]
        operation = _generic_operation(sentence)
        if operation is not None and _is_affirmative_assertion(
            sentence,
            match.start() - sentence_start,
            match.end() - sentence_start,
            match.group(0),
        ):
            matches.append((match.group(0), operation, match.start(), match.end()))
    matches.sort(key=lambda item: item[2])
    return matches


def unsupported_claims(answer: str, completed_tools) -> list[str]:
    """Success claims whose matching mutation operation did not complete.

    A completed ``update`` proves only save/update language; it does not prove
    that an email was sent or that a previous revision was undone.
    """
    if not answer:
        return []
    completed = completed_operations(completed_tools)
    return sorted(
        {text.casefold() for text, operation, _start, _end in claim_matches(answer) if operation not in completed}
    )


def may_become_claim(fragment: str) -> bool:
    """Whether an unfinished fragment must wait for a later model delta.

    This is deliberately narrower than buffering every incomplete sentence.
    It recognizes partial claim words and the small set of grammatical leads
    accepted by :func:`claim_matches`, so ordinary model deltas keep their
    original streaming behavior.
    """
    if not fragment:
        return False
    # A sentence boundary inside a still-open quote is not a real answer
    # boundary. Retain the quote until its closing delimiter arrives so claim
    # examples remain eligible for the quoted-text exclusion as a whole.
    if (
        fragment.count("`") % 2
        or fragment.count('"') % 2
        or fragment.count("“") > fragment.count("”")
        or fragment.count("‘") > fragment.count("’")
    ):
        return True
    clause = re.split(r"[.!?\n]", fragment)[-1].casefold().strip()
    if not clause:
        return False

    # A claim word or phrase may itself be split at any character boundary.
    # Check each word-boundary suffix so "Email del" retains only until the
    # following delta can prove whether it is "delivered".
    for match in re.finditer(r"\b", clause):
        suffix = clause[match.start():].strip()
        if suffix and any(phrase.startswith(suffix) for phrase in _CLAIM_PHRASES):
            return True

    # Keep an assertion lead only while it still exactly describes a possible
    # lead. Once it becomes ordinary prose ("I think"), it streams normally.
    return clause in _ASSERTION_PREFIXES or any(
        prefix.startswith(clause) for prefix in _ASSERTION_PREFIXES
    )


def remove_unsupported_claims(answer: str, completed_tools) -> str:
    """Replace an unproven success sentence with a neutral correction."""
    if not answer:
        return answer
    completed = completed_operations(completed_tools)
    unsupported = [
        (text, operation, start, end)
        for text, operation, start, end in claim_matches(answer)
        if operation not in completed
    ]
    if not unsupported:
        return answer
    filtered = answer
    replacements: list[tuple[int, int, str]] = []
    for text, operation, start, end in unsupported:
        sentence_start, sentence_end = _sentence_bounds(filtered, start, end)
        sentence = filtered[sentence_start:sentence_end]
        clause, relative_start, _relative_end = _claim_clause(
            sentence, start - sentence_start, end - sentence_start
        )
        clause_start = start - relative_start
        clause_end = clause_start + len(clause)
        # Keep one correction for a sentence with several unsupported claims.
        if any(
            existing_start == clause_start and existing_end == clause_end
            for existing_start, existing_end, _ in replacements
        ):
            continue
        turkish = text.casefold() not in _ENGLISH_CLAIMS
        corrections = {
            "update": (
                "Değişiklik doğrulanamadı." if turkish else "I could not verify the change."
            ),
            "undo": (
                "Geri alma işlemi doğrulanamadı." if turkish else "I could not verify the reversal."
            ),
            "send_email": (
                "E-posta teslimi doğrulanamadı." if turkish else "I could not verify email delivery."
            ),
        }
        replacements.append((clause_start, clause_end, corrections[operation]))
    for start, end, replacement in sorted(replacements, reverse=True):
        filtered = filtered[:start] + replacement + filtered[end:]
    return filtered.strip()


def jargon(answer: str) -> list[str]:
    return sorted({match.group(0).casefold() for match in _JARGON.finditer(answer or "")})

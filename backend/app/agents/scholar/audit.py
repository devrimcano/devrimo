"""What an answer claims, checked against what the run actually did.

The instructions forbid claiming a save that never happened and narrating
internal machinery, and the model still does both occasionally. Enforcement by
rewriting is impossible without buffering the whole answer - which would trade
the streaming away for it - so these are the deterministic checks that make the
violations countable instead of invisible. Nothing here changes the answer; the
broker reports what it saw.
"""

import re

# Each group pairs the tools that can make a claim true with the verbs that
# assert it. A completed `update` must not excuse "mail gönderdim": the audit
# answers "was this operation done", not "was anything done".
_CLAIM_GROUPS = (
    (
        frozenset({"send_email", "webmail_send_email", "webmail_reply_email"}),
        re.compile(r"\b(gonderdim|gönderdim|gonderildi|gönderildi|sent)\b", re.IGNORECASE),
    ),
    (
        frozenset({"update"}),
        re.compile(
            r"\b(kaydettim|kaydedildi|kayıt ettim|kayit ettim|ekledim|guncelledim|güncelledim|"
            r"olusturdum|oluşturdum|saved|updated|created)\b",
            re.IGNORECASE,
        ),
    ),
    (
        frozenset({"undo", "update"}),
        re.compile(r"\b(sildim|sildi|deleted|removed|geri aldim|geri aldım)\b", re.IGNORECASE),
    ),
)
MUTATIONS = frozenset().union(*(tools for tools, _ in _CLAIM_GROUPS))

# Word-bounded, so "sorgulama" (ordinary Turkish for querying) is not jargon.
_JARGON = re.compile(
    r"\b(published release|veritaban\w*|şema|schema|system prompt|course_count)\b",
    re.IGNORECASE,
)


def unsupported_claims(answer: str, completed_tools) -> list[str]:
    """Success verbs in the answer whose own operation never completed."""
    if not answer:
        return []
    done = set(completed_tools or [])
    found: set[str] = set()
    for tools, pattern in _CLAIM_GROUPS:
        if tools & done:
            continue
        found.update(match.group(0).casefold() for match in pattern.finditer(answer))
    return sorted(found)


def jargon(answer: str) -> list[str]:
    return sorted({match.group(0).casefold() for match in _JARGON.finditer(answer or "")})

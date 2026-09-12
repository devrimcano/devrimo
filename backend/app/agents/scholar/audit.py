"""What an answer claims, checked against what the run actually did.

The instructions forbid claiming a save that never happened and narrating
internal machinery, and the model still does both occasionally. Enforcement by
rewriting is impossible without buffering the whole answer - which would trade
the streaming away for it - so these are the deterministic checks that make the
violations countable instead of invisible. Nothing here changes the answer; the
broker reports what it saw.
"""

import re

MUTATIONS = {"update", "undo", "send_email", "webmail_send_email", "webmail_reply_email"}

_CLAIMS = re.compile(
    r"\b(kaydettim|kaydedildi|kayıt ettim|kayit ettim|ekledim|guncelledim|güncelledim|"
    r"sildim|gonderdim|gönderdim|gonderildi|gönderildi|olusturdum|oluşturdum|"
    r"saved|updated|sent|created|deleted)\b",
    re.IGNORECASE,
)
# Word-bounded, so "sorgulama" (ordinary Turkish for querying) is not jargon.
_JARGON = re.compile(
    r"\b(published release|veritaban\w*|şema|schema|system prompt|course_count)\b",
    re.IGNORECASE,
)


def unsupported_claims(answer: str, completed_tools) -> list[str]:
    """Success verbs in the answer with no completed mutation behind them."""
    if not answer or MUTATIONS & set(completed_tools or []):
        return []
    return sorted({match.group(0).casefold() for match in _CLAIMS.finditer(answer)})


def jargon(answer: str) -> list[str]:
    return sorted({match.group(0).casefold() for match in _JARGON.finditer(answer or "")})

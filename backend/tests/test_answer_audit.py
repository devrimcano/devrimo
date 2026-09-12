"""The two prompt-only guarantees, made countable instead of invisible.

The instructions forbid claiming a save that never happened and narrating
internal machinery; the model still does both occasionally. These checks do not
change the answer - they only report what it claimed.
"""

from app.agents.scholar.audit import jargon, unsupported_claims


def test_a_success_claim_with_no_completed_write_is_flagged():
    assert unsupported_claims("Tercihini kaydettim.", ["read", "search"]) == ["kaydettim"]


def test_a_completed_write_explains_the_claim():
    assert unsupported_claims("Kaydettim: planını güncelledim.", ["update"]) == []
    assert unsupported_claims("I sent the mail.", ["read", "send_email"]) == []


def test_an_answer_with_no_claim_is_clean():
    assert unsupported_claims("EE 201 dört kredi.", ["read"]) == []
    assert unsupported_claims("", []) == []


def test_internal_jargon_is_counted_and_ordinary_turkish_is_not():
    assert "published release" in jargon("This is not in the published release.")
    assert jargon("Listeleri sorgulama sonucu") == []
    assert jargon("Kayıt yok") == []
    assert jargon("veritabanından okundu") == ["veritabanından"]

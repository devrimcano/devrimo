"""Mutation claims and internal jargon are checked at the stream boundary.

The instructions forbid claiming a save that never happened and narrating
internal machinery; the model still does both occasionally. Claim checks drive
the student-facing guard, while both checks remain available for audit metrics.
"""

from app.agents.scholar.audit import jargon, remove_unsupported_claims, unsupported_claims


def test_a_success_claim_with_no_completed_write_is_flagged():
    assert unsupported_claims("Tercihini kaydettim.", ["read", "search"]) == ["kaydettim"]


def test_a_completed_write_explains_the_claim():
    assert unsupported_claims("Kaydettim: planını güncelledim.", ["update"]) == []
    assert (
        unsupported_claims(
            "I sent the mail.",
            [{"tool": "send_email", "result": {"status": "sent", "message_id": "m-1"}}],
        )
        == []
    )
    assert unsupported_claims("I sent the mail.", [{"tool": "send_email", "result": "sent"}]) == []
    assert unsupported_claims("Sent successfully.", [{"tool_name": "send_email", "result": '"sent"'}]) == []


def test_a_write_does_not_explain_a_different_mutation_claim():
    assert unsupported_claims("I sent the mail.", ["update"]) == ["sent"]
    assert unsupported_claims("Planı geri aldım.", ["update"]) == ["geri aldım"]
    assert unsupported_claims("Planı güncelledim.", ["send_email"]) == ["güncelledim"]
    assert (
        unsupported_claims(
            "I sent the mail.", [{"tool": "send_email", "result": {"status": "approval_required"}}]
        )
        == ["sent"]
    )
    assert unsupported_claims("Tercih güncellendi.", []) == ["güncellendi"]
    assert unsupported_claims("I sent the mail and saved the preference.", []) == ["saved", "sent"]
    assert unsupported_claims("Sent successfully.", []) == ["sent"]
    assert unsupported_claims("I saved it and sent the email.", ["update"]) == ["sent"]
    assert unsupported_claims("I saved it, then sent the email.", ["update"]) == ["sent"]
    assert unsupported_claims("Updated the plan and delivered the email.", ["update"]) == ["delivered"]


def test_turkish_person_passive_and_generic_success_forms_need_proof():
    assert unsupported_claims("Tercihini kaydetti.", []) == ["kaydetti"]
    assert unsupported_claims("Planı sildi.", []) == ["sildi"]
    assert unsupported_claims("Değişiklik başarıyla yapıldı.", []) == ["yapıldı"]
    assert unsupported_claims("E-posta gönderildi.", []) == ["gönderildi"]
    assert unsupported_claims("Email sent.", []) == ["sent"]
    assert unsupported_claims("Plan updated.", []) == ["updated"]
    assert unsupported_claims("Preference saved.", []) == ["saved"]
    assert unsupported_claims("Plan created.", []) == ["created"]
    assert unsupported_claims("Email delivered.", []) == ["delivered"]
    assert unsupported_claims("Successfully saved.", []) == ["saved"]
    assert unsupported_claims("Plan successfully updated.", []) == ["updated"]
    assert unsupported_claims("Email successfully delivered.", []) == ["delivered"]
    assert unsupported_claims("Schedule already saved.", []) == ["saved"]
    assert unsupported_claims("The plan is now updated.", []) == ["updated"]
    assert unsupported_claims("The plan was just updated.", []) == ["updated"]
    assert unsupported_claims("The plan is already saved.", []) == ["saved"]


def test_descriptive_negated_quoted_and_historical_text_is_not_a_mutation_claim():
    safe = [
        "The saved preference is visible.",
        "I did not say I saved it.",
        'He wrote "I saved it." as an example.',
        "Düğme `Saved` görünür.",
        "The preference was saved yesterday.",
        "Email not delivered.",
        "Dün tercih kaydedildi.",
        "Tercih kaydedildi mi?",
    ]
    for sentence in safe:
        assert unsupported_claims(sentence, []) == []
        assert remove_unsupported_claims(sentence, []) == sentence

    compound = "I have not saved it, but I sent the email."
    assert unsupported_claims(compound, []) == ["sent"]
    assert remove_unsupported_claims(compound, []) == (
        "I have not saved it, but I could not verify email delivery."
    )


def test_an_unsupported_claim_is_removed_before_streaming_to_the_student():
    assert remove_unsupported_claims("Tercihini kaydettim.", []) == "Değişiklik doğrulanamadı."
    assert remove_unsupported_claims("I sent the email.", []) == "I could not verify email delivery."
    assert remove_unsupported_claims("Kaydettim.", ["update"]) == "Kaydettim."


def test_an_answer_with_no_claim_is_clean():
    assert unsupported_claims("EE 201 dört kredi.", ["read"]) == []
    assert unsupported_claims("", []) == []


def test_internal_jargon_is_counted_and_ordinary_turkish_is_not():
    assert "published release" in jargon("This is not in the published release.")
    assert jargon("Listeleri sorgulama sonucu") == []
    assert jargon("Kayıt yok") == []
    assert jargon("veritabanından okundu") == ["veritabanından"]
